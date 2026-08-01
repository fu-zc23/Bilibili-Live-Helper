import hashlib
import hmac
import io
import json
import re
import time
from typing import Any
from urllib.parse import quote, urlencode

import requests
import qrcode

from .constants import (
    DEFAULT_HEADERS,
    LOGIN_MAX_POLLS,
    LOGIN_POLL_INTERVAL,
    REQUEST_TIMEOUT,
    WATCH_LOG_ID_PREFIX,
    WATCH_PAGE_URL_PREFIX,
    WATCH_SCREEN_SIZE,
    WATCH_WEB_LOCATION,
)
from .exceptions import BiliLiveError
from .models import MedalTaskInfo, TargetRoom
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

    @staticmethod
    def compact_json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

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

    def get_activated_medal_info(self, target_id: int) -> dict[str, Any]:
        """获取指定主播的已佩戴粉丝勋章信息及亲密度任务列表"""
        payload = self.request(
            "GET",
            "https://api.live.bilibili.com/xlive/app-ucenter/v1/fansMedal/GetActivatedMedalInfo",
            params={
                "platform": "pc",
                "target_id": target_id,
                "web_location": "444.260",
            },
            headers=self.live_headers(),
        )
        return self.expect_code_zero(payload, "获取勋章任务信息失败")

    @staticmethod
    def parse_medal_tasks(medal_data: dict[str, Any]) -> tuple[bool, list[MedalTaskInfo]]:
        """从 API 返回数据中解析任务列表"""
        is_lighted = bool(medal_data.get("is_lighted", False))
        raw_tasks = medal_data.get("task_info") or []
        if not isinstance(raw_tasks, list):
            return is_lighted, []

        tasks: list[MedalTaskInfo] = []
        for item in raw_tasks:
            sub_title = str(item.get("sub_title", ""))
            daily_limit, daily_current = BiliLiveClient._parse_task_progress(sub_title)
            tasks.append(MedalTaskInfo(
                jump_type=str(item.get("jump_type", "")),
                title=str(item.get("title", "")),
                sub_title=sub_title,
                add_text=str(item.get("add_text", "")),
                is_done=bool(item.get("is_done", False)),
                daily_limit=daily_limit,
                daily_current=daily_current,
            ))
        return is_lighted, tasks

    @staticmethod
    def _parse_task_progress(sub_title: str) -> tuple[int, int]:
        """从 "每日上限 7/10" 中解析出 (10, 7)"""
        import re
        m = re.search(r"(\d+)\s*/\s*(\d+)", sub_title)
        if m:
            return int(m.group(2)), int(m.group(1))
        return 0, 0

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

    def get_info_by_room(self, room_id: int) -> dict[str, Any]:
        payload = self.request(
            "GET",
            "https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom",
            params=self.sign_wbi_params(
                {
                    "room_id": room_id,
                    "web_location": WATCH_WEB_LOCATION,
                }
            ),
            headers=self.live_headers(room_id),
        )
        return self.expect_code_zero(payload, "获取直播间聚合信息失败")

    def get_room_play_info(self, room_id: int) -> dict[str, Any]:
        payload = self.request(
            "GET",
            "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo",
            params={
                "room_id": room_id,
                "protocol": "0,1",
                "format": "0,1,2",
                "codec": "0,1,2",
                "qn": 400,
                "platform": "web",
                "ptype": 8,
                "dolby": 5,
                "panoramic": 1,
            },
            headers=self.live_headers(room_id),
        )
        return self.expect_code_zero(payload, "获取直播流信息失败")

    @staticmethod
    def select_play_url(play_info: dict[str, Any]) -> str:
        playurl = (play_info.get("playurl_info") or {}).get("playurl") or {}
        streams = playurl.get("stream") or []
        codec_priority = {"av1": 0, "hevc": 1, "avc": 2}

        candidates: list[tuple[int, str]] = []
        for stream in streams:
            for fmt in stream.get("format") or []:
                for codec in fmt.get("codec") or []:
                    url_info = (codec.get("url_info") or [{}])[0]
                    host = str(url_info.get("host", "")).strip()
                    base_url = str(codec.get("base_url", "")).strip()
                    extra = str(url_info.get("extra", "")).strip()
                    if not host or not base_url:
                        continue
                    joiner = "" if base_url.endswith("?") or not extra else "&"
                    if base_url.endswith("?"):
                        full_url = f"{host}{base_url}{extra}"
                    else:
                        full_url = f"{host}{base_url}{joiner}{extra}"
                    priority = codec_priority.get(str(codec.get("codec_name", "")).lower(), 99)
                    candidates.append((priority, full_url))

        if not candidates:
            return ""
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

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

                aggregate_info = self.get_info_by_room(room_id_from_space)
                room_info = aggregate_info.get("room_info") or self.get_room_info(room_id_from_space)
                anchor_info = aggregate_info.get("anchor_info") or {}
                live_info = anchor_info.get("live_info") or {}
            except (requests.RequestException, BiliLiveError, KeyError, ValueError) as exc:
                if first_error is None:
                    first_error = exc
                print(f"[WARN] 跳过粉丝牌目标 {target_id}: {exc}")
                continue

            real_room_id = int(room_info.get("room_id") or room_id_from_space)
            if real_room_id in seen_room_ids:
                continue

            is_living = int(room_info.get("live_status", 0) or 0) == 1
            play_url = ""
            if is_living:
                try:
                    play_info = self.get_room_play_info(real_room_id)
                    play_url = self.select_play_url(play_info)
                except (requests.RequestException, BiliLiveError, KeyError, ValueError) as exc:
                    print(f"[WARN] 获取房间 {real_room_id} 播放流失败: {exc}")

            seen_room_ids.add(real_room_id)
            targets.append(
                TargetRoom(
                    room_id=real_room_id,
                    anchor_id=int(room_info["uid"]),
                    anchor_level=int(live_info.get("level", 0) or 0),
                    parent_area_id=int(room_info.get("parent_area_id", 0) or 0),
                    area_id=int(room_info.get("area_id", 0) or 0),
                    target_name=str(medal.get("target_name", "")).strip() or f"room_{real_room_id}",
                    is_living=is_living,
                    live_key=str(room_info.get("up_session") or room_info.get("live_id_str") or "").strip(),
                    sub_session_key=str(room_info.get("sub_session_key", "") or "").strip(),
                    play_url=play_url,
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

    def get_live_buvid(self) -> str:
        live_buvid = str(self.session.cookies.get("LIVE_BUVID", "")).strip()
        if not live_buvid:
            raise BiliLiveError("缺少 LIVE_BUVID，请先初始化直播 Cookie")
        return live_buvid

    @staticmethod
    def build_watch_log_query(
        *,
        room_id: int,
        anchor_id: int,
        anchor_level: int,
        area_id: int,
        parent_area_id: int,
        player_guid: str,
        play_url: str,
        watch_seconds: int,
        timestamp_ms: int,
        live_key: str,
        player_session_id: str,
        sub_session_key: str,
        track_id: int,
    ) -> str:
        context = json.dumps(
            [
                {"statistic": {"appId": 100, "platform": 5, "pc_client": "web"}, "device": "web"},
                {"room_category": 0},
                {"official_channel": {}},
                {"is_pk": 0, "pk_id": track_id},
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        sessions = json.dumps(
            [{"live_key": live_key, "sub_session_key": sub_session_key}],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        progress_value = 180 + max(0, watch_seconds)
        parts = [
            f"{WATCH_LOG_ID_PREFIX}{timestamp_ms}{quote(f'{WATCH_PAGE_URL_PREFIX}/{room_id}', safe='')}",
            "",
            "444.8.0.0",
            str(timestamp_ms),
            "",
            WATCH_SCREEN_SIZE,
            "1",
            str(room_id),
            str(anchor_id),
            str(anchor_level),
            str(area_id),
            str(parent_area_id),
            "0",
            player_guid,
            "3",
            "5",
            quote(play_url, safe=""),
            str(progress_value),
            str(timestamp_ms),
            str(timestamp_ms),
            "",
            "",
            "",
            str(live_key),
            player_session_id,
            quote(context, safe=""),
            "0",
            quote(sessions, safe=""),
            str(track_id),
            "0",
            "",
        ]
        return "|".join(parts)

    def report_watch_log(
        self,
        target: TargetRoom,
        *,
        player_guid: str,
        player_session_id: str,
        watch_seconds: int,
    ) -> None:
        if not target.live_key or not target.sub_session_key or not target.play_url:
            raise BiliLiveError(f"房间 {target.room_id} 缺少观看日志所需的直播参数")

        timestamp_ms = int(time.time() * 1000)
        query = self.build_watch_log_query(
            room_id=target.room_id,
            anchor_id=target.anchor_id,
            anchor_level=target.anchor_level,
            area_id=target.area_id,
            parent_area_id=target.parent_area_id,
            player_guid=player_guid,
            play_url=target.play_url,
            watch_seconds=watch_seconds,
            timestamp_ms=timestamp_ms,
            live_key=target.live_key,
            player_session_id=player_session_id,
            sub_session_key=target.sub_session_key,
            track_id=target.track_id,
        )
        response = self.session.get(
            f"https://data.bilibili.com/log/web?{query}",
            headers=self.live_headers(target.room_id),
            timeout=self.timeout,
        )
        response.raise_for_status()

    @staticmethod
    def parse_watch_heartbeat_response(data: dict[str, Any], error_prefix: str) -> dict[str, Any]:
        timestamp = int(data.get("timestamp", 0) or 0)
        secret_key = str(data.get("secret_key", "") or "").strip()
        secret_rule = data.get("secret_rule") or []
        if timestamp <= 0:
            raise BiliLiveError(f"{error_prefix}: 缺少 timestamp")
        if not secret_key:
            raise BiliLiveError(f"{error_prefix}: 缺少 secret_key")
        if not isinstance(secret_rule, list) or not secret_rule:
            raise BiliLiveError(f"{error_prefix}: 缺少 secret_rule")
        return {
            "timestamp": timestamp,
            "secret_key": secret_key,
            "secret_rule": [int(rule) for rule in secret_rule],
            "heartbeat_interval": int(data.get("heartbeat_interval", 60) or 60),
        }

    @staticmethod
    def sign_watch_heartbeat(payload: str, rules: list[int], secret_key: str) -> str:
        algorithms = {
            0: hashlib.md5,
            1: hashlib.sha1,
            2: hashlib.sha256,
            3: hashlib.sha224,
            4: hashlib.sha512,
            5: hashlib.sha384,
        }
        result = payload
        for rule in rules:
            algorithm = algorithms.get(int(rule))
            if algorithm is None:
                continue
            result = hmac.new(
                secret_key.encode("utf-8"),
                result.encode("utf-8"),
                algorithm,
            ).hexdigest()
        return result

    def enter_room_heartbeat(
        self,
        target: TargetRoom,
        *,
        device_id: str,
        seq_id: int,
        session_uuid: str,
    ) -> dict[str, Any]:
        timestamp = int(time.time() * 1000)
        payload = self.request(
            "POST",
            "https://live-trace.bilibili.com/xlive/data-interface/v1/x25Kn/E",
            data={
                "id": self.compact_json(
                    [target.parent_area_id, target.area_id, seq_id, target.room_id]
                ),
                "device": self.compact_json([device_id, session_uuid]),
                "ts": timestamp,
                "is_patch": 0,
                "heart_beat": "[]",
                "ua": self.session.headers.get("User-Agent", ""),
                "csrf_token": self.csrf,
                "csrf": self.csrf,
                "visit_id": "",
                "ruid": target.anchor_id,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                **self.live_headers(target.room_id),
            },
        )
        data = self.expect_code_zero(payload, "直播间进入上报失败")
        return self.parse_watch_heartbeat_response(data, "直播间进入上报失败")

    def send_watch_heartbeat(
        self,
        target: TargetRoom,
        *,
        device_id: str,
        seq_id: int,
        session_uuid: str,
        timestamp: int,
        secret_key: str,
        secret_rule: list[int],
    ) -> dict[str, Any]:
        if timestamp <= 0 or not secret_key or not secret_rule:
            raise BiliLiveError("观看心跳缺少 secret_key、secret_rule 或 timestamp")

        current_ts = int(time.time() * 1000)
        sign_payload = self.compact_json(
            {
                "platform": "web",
                "parent_id": target.parent_area_id,
                "area_id": target.area_id,
                "seq_id": seq_id,
                "room_id": target.room_id,
                "buvid": device_id,
                "uuid": session_uuid,
                "ets": timestamp,
                "time": 60,
                "ts": current_ts,
            }
        )
        params = self.sign_wbi_params(
            {
                "s": self.sign_watch_heartbeat(sign_payload, secret_rule, secret_key),
                "id": self.compact_json(
                    [target.parent_area_id, target.area_id, seq_id, target.room_id]
                ),
                "device": self.compact_json([device_id, session_uuid]),
                "ruid": target.anchor_id,
                "ets": timestamp,
                "benchmark": secret_key,
                "time": 60,
                "ts": current_ts,
                "ua": self.session.headers.get("User-Agent", ""),
                "trackid": target.track_id,
                "web_location": WATCH_WEB_LOCATION,
                "csrf": self.csrf,
            }
        )
        payload = self.request(
            "POST",
            "https://live-trace.bilibili.com/xlive/data-interface/v1/x25Kn/X",
            params=params,
            headers=self.live_headers(target.room_id),
        )
        data = self.expect_code_zero(payload, "直播间观看心跳失败")
        return self.parse_watch_heartbeat_response(data, "直播间观看心跳失败")
