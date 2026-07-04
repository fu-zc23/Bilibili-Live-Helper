import hashlib
import io
import re
import time
from typing import Any
from urllib.parse import urlencode

import qrcode
import requests

from .constants import DEFAULT_HEADERS, LOGIN_MAX_POLLS, LOGIN_POLL_INTERVAL, REQUEST_TIMEOUT
from .exceptions import BiliLiveError
from .models import TargetRoom
from .utils import parse_cookie_string


class BiliLiveClient:
    def __init__(self, cookie_string: str, timeout: int = REQUEST_TIMEOUT) -> None:
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = timeout
        self._wbi_keys: tuple[str, str] | None = None

        cookie_dict = parse_cookie_string(cookie_string)
        for key, value in cookie_dict.items():
            self.session.cookies.set(key, value, domain=".bilibili.com")

        self.csrf = self.session.cookies.get("bili_jct", "")
        self.uid = self.session.cookies.get("DedeUserID", "")
        if not self.session.cookies.get("SESSDATA"):
            raise BiliLiveError("Cookie 缺少 SESSDATA")
        if not self.csrf:
            raise BiliLiveError("Cookie 缺少 bili_jct")
        if not self.uid:
            raise BiliLiveError("Cookie 缺少 DedeUserID")

    @staticmethod
    def main_headers() -> dict[str, str]:
        return {
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
        }

    @staticmethod
    def live_headers(room_id: int | None = None) -> dict[str, str]:
        referer = "https://live.bilibili.com/" if room_id is None else f"https://live.bilibili.com/{room_id}"
        return {
            "Referer": referer,
            "Origin": "https://live.bilibili.com",
        }

    @staticmethod
    def expect_code_zero(payload: dict[str, Any], error_prefix: str) -> dict[str, Any]:
        if payload.get("code") != 0:
            raise BiliLiveError(f"{error_prefix}: {payload.get('message', '未知错误')}")
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise BiliLiveError(f"{error_prefix}: 接口返回 data 格式异常")
        return data

    def cookie_string(self) -> str:
        cookie_map: dict[str, str] = {}
        for cookie in self.session.cookies:
            if cookie.value:
                cookie_map[cookie.name] = cookie.value
        return "; ".join(f"{name}={value}" for name, value in cookie_map.items())

    def refresh_auth_fields(self) -> None:
        self.csrf = self.session.cookies.get("bili_jct", "")
        self.uid = self.session.cookies.get("DedeUserID", "")
        if not self.session.cookies.get("SESSDATA"):
            raise BiliLiveError("登录后仍未拿到 SESSDATA")
        if not self.csrf:
            raise BiliLiveError("登录后仍未拿到 bili_jct")
        if not self.uid:
            raise BiliLiveError("登录后仍未拿到 DedeUserID")

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self.session.request(
            method=method,
            url=url,
            params=params,
            data=data,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise BiliLiveError(f"接口返回格式异常: {url}")
        return payload

    def ensure_main_site_cookie(self) -> None:
        response = self.session.get(
            "https://www.bilibili.com/",
            headers={"Referer": "https://www.bilibili.com/"},
            timeout=self.timeout,
        )
        response.raise_for_status()

    def ensure_live_cookie(self) -> None:
        payload = self.request(
            "GET",
            "https://api.live.bilibili.com/news/v1/notice/recom",
            params={"product": "live"},
            headers=self.live_headers(),
        )
        self.expect_code_zero(payload, "初始化直播 Cookie 失败")
        if not self.session.cookies.get("LIVE_BUVID"):
            raise BiliLiveError("初始化直播 Cookie 后仍未获取到 LIVE_BUVID")

    def generate_login_qrcode(self) -> tuple[str, str]:
        payload = self.request(
            "GET",
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
        )
        data = self.expect_code_zero(payload, "获取二维码失败")
        login_url = str(data.get("url", "")).strip()
        qrcode_key = str(data.get("qrcode_key", "")).strip()
        if not login_url or not qrcode_key:
            raise BiliLiveError("二维码接口返回缺少 url 或 qrcode_key")
        return login_url, qrcode_key

    def poll_login_status(self, qrcode_key: str) -> tuple[int, str]:
        response = self.session.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": qrcode_key, "source": "main_mini"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        data = self.expect_code_zero(payload, "登录轮询失败")
        return int(data.get("code", -1)), str(data.get("message", "")).strip()

    @staticmethod
    def render_terminal_qr(content: str) -> str:
        qr = qrcode.QRCode(border=4)
        qr.add_data(content)
        qr.make(fit=True)
        out = io.StringIO()
        qr.print_ascii(out=out, invert=True)
        return out.getvalue().rstrip()

    def qr_login(self) -> str:
        login_url, qrcode_key = self.generate_login_qrcode()

        print("[INFO] 已生成 B 站扫码登录二维码")
        print(self.render_terminal_qr(login_url))
        print(f"[INFO] 登录链接: {login_url}")
        print("[INFO] 请使用 B 站 App 扫码并确认登录")
        for poll_index in range(LOGIN_MAX_POLLS):
            status_code, message = self.poll_login_status(qrcode_key)
            if status_code == 0:
                self.refresh_auth_fields()
                cookie_string = self.cookie_string()
                if not cookie_string:
                    raise BiliLiveError("扫码成功，但未提取到 Cookie")
                print("[OK] 扫码登录成功")
                return cookie_string

            if status_code == 86038:
                raise BiliLiveError("二维码已失效，请重新运行登录")

            readable = message or "等待扫码中"
            print(f"[INFO] 登录状态 {poll_index + 1}/{LOGIN_MAX_POLLS}: {readable}")
            time.sleep(LOGIN_POLL_INTERVAL)

        raise BiliLiveError("登录超时，请重新运行后扫码")

    def get_room_info(self, room_id: int) -> dict[str, Any]:
        payload = self.request(
            "GET",
            "https://api.live.bilibili.com/room/v1/Room/get_info",
            params={"room_id": room_id, "from": "room"},
            headers={"Referer": f"https://live.bilibili.com/{room_id}"},
        )
        data = self.expect_code_zero(payload, "获取直播间信息失败")
        if not data.get("uid"):
            raise BiliLiveError("直播间信息中缺少主播 uid")
        return data

    def get_medal_wall(self) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            "https://api.live.bilibili.com/xlive/web-ucenter/user/MedalWall",
            params={"target_id": self.uid},
            headers=self.live_headers(),
        )
        data = self.expect_code_zero(payload, "获取粉丝牌列表失败")
        medals = data.get("list") or []
        if not isinstance(medals, list):
            raise BiliLiveError("粉丝牌列表格式异常")
        return medals

    def get_wbi_keys(self) -> tuple[str, str]:
        if self._wbi_keys is not None:
            return self._wbi_keys

        payload = self.request(
            "GET",
            "https://api.bilibili.com/x/web-interface/nav",
            headers=self.main_headers(),
        )
        data = self.expect_code_zero(payload, "获取 WBI Key 失败")
        wbi_img = data.get("wbi_img") or {}
        img_url = str(wbi_img.get("img_url", "")).strip()
        sub_url = str(wbi_img.get("sub_url", "")).strip()
        if not img_url or not sub_url:
            raise BiliLiveError("WBI 图片信息缺失")

        img_key = img_url.split("/")[-1].split(".")[0]
        sub_key = sub_url.split("/")[-1].split(".")[0]
        self._wbi_keys = (img_key, sub_key)
        return self._wbi_keys

    def sign_wbi_params(self, params: dict[str, Any]) -> dict[str, Any]:
        img_key, sub_key = self.get_wbi_keys()
        mixin_key_tab = [
            46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
            27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
            37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
            22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
        ]
        orig = img_key + sub_key
        mixin_key = "".join(orig[index] for index in mixin_key_tab)[:32]

        signed_params: dict[str, Any] = {**params, "wts": int(time.time())}
        chr_filter = re.compile(r"[!'()*]")
        encoded: dict[str, str] = {}
        for key, value in signed_params.items():
            encoded[key] = chr_filter.sub("", str(value))

        query = urlencode(sorted(encoded.items()))
        signed_params["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
        return signed_params

    def get_space_info(self, mid: int) -> dict[str, Any]:
        payload = self.request(
            "GET",
            "https://api.bilibili.com/x/space/wbi/acc/info",
            params=self.sign_wbi_params({"mid": mid}),
            headers=self.main_headers(),
        )
        return self.expect_code_zero(payload, "获取空间信息失败")

    def resolve_target_rooms(self) -> list[TargetRoom]:
        targets: list[TargetRoom] = []
        seen_room_ids: set[int] = set()
        first_error: Exception | None = None
        for medal in self.get_medal_wall():
            medal_info = medal.get("medal_info") or {}
            target_id = int(medal_info.get("target_id", 0) or 0)
            if target_id <= 0:
                continue

            try:
                space_info = self.get_space_info(target_id)
                live_room = space_info.get("live_room") or {}
                room_id_from_space = int(live_room.get("roomid", 0) or 0)
                if room_id_from_space <= 0:
                    continue

                room_info = self.get_room_info(room_id_from_space)
            except (requests.RequestException, BiliLiveError, KeyError, ValueError) as exc:
                if first_error is None:
                    first_error = exc
                print(f"[WARN] 跳过粉丝牌目标 {target_id}: {exc}")
                continue

            real_room_id = int(room_info.get("room_id") or room_id_from_space)
            if real_room_id in seen_room_ids:
                continue

            seen_room_ids.add(real_room_id)
            targets.append(
                TargetRoom(
                    room_id=real_room_id,
                    anchor_id=int(room_info["uid"]),
                    target_name=str(medal.get("target_name", "")).strip() or f"room_{real_room_id}",
                    is_living=int(room_info.get("live_status", 0) or 0) == 1,
                )
            )
        if not targets and first_error is not None:
            if isinstance(first_error, requests.RequestException):
                raise first_error
            raise BiliLiveError(f"解析粉丝牌直播间失败: {first_error}")
        return targets

    def like_room(self, room_id: int, anchor_id: int, click_time: int) -> None:
        click_time = max(1, min(click_time, 10))
        payload = self.request(
            "POST",
            "https://api.live.bilibili.com/xlive/app-ucenter/v1/like_info_v3/like/likeReportV3",
            data={
                "click_time": click_time,
                "room_id": room_id,
                "uid": self.uid,
                "anchor_id": anchor_id,
                "csrf_token": self.csrf,
                "csrf": self.csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", **self.live_headers(room_id)},
        )
        if payload.get("code") != 0:
            raise BiliLiveError(f"点赞失败: {payload.get('message', '未知错误')} (code={payload.get('code')})")

    def send_danmaku(self, room_id: int, message: str) -> None:
        payload = self.request(
            "POST",
            "https://api.live.bilibili.com/msg/send",
            data={
                "bubble": "0",
                "msg": message,
                "color": "16777215",
                "mode": "1",
                "fontsize": "25",
                "rnd": str(int(time.time())),
                "roomid": room_id,
                "csrf_token": self.csrf,
                "csrf": self.csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded", **self.live_headers(room_id)},
        )
        if payload.get("code") != 0:
            raise BiliLiveError(
                f"弹幕发送失败: {payload.get('message', '未知错误')} (code={payload.get('code')})"
            )
