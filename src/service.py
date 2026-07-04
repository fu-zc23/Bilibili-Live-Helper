from pathlib import Path
import random
import sys
import time

import requests

from .client import BiliLiveClient
from .config import build_parser, load_config_from_args, parse_task_config, validate_task_config
from .constants import DUMMY_COOKIE
from .exceptions import BiliLiveError
from .models import TaskConfig
from .utils import choose_message, save_json


def login_and_update_config(
    *,
    client: BiliLiveClient,
    config: dict,
    config_path: Path,
) -> str:
    cookie_string = client.qr_login()
    config["cookie"] = cookie_string
    save_json(config_path, config)
    print(f"[OK] 新 Cookie 已保存到 {config_path}")
    return cookie_string


def ensure_login_cookie(
    *,
    task_config: TaskConfig,
    config: dict,
    config_path: Path,
    force_login: bool,
    run_after_login: bool,
) -> str | None:
    if force_login:
        login_client = BiliLiveClient(cookie_string=DUMMY_COOKIE)
        cookie = login_and_update_config(
            client=login_client,
            config=config,
            config_path=config_path,
        )
        return cookie if run_after_login else None

    if task_config.cookie:
        return task_config.cookie

    print("[INFO] 未检测到本地 Cookie，开始自动扫码登录")
    login_client = BiliLiveClient(cookie_string=DUMMY_COOKIE)
    return login_and_update_config(
        client=login_client,
        config=config,
        config_path=config_path,
    )


class BiliLiveService:
    def __init__(self, client: BiliLiveClient) -> None:
        self.client = client

    def like_room_multiple(
        self,
        room_id: int,
        anchor_id: int,
        like_count: int,
        like_batch_size: int,
        like_interval_min: float,
        like_interval_max: float,
    ) -> None:
        batch_size = max(1, min(like_batch_size, 10))
        total_requests = (like_count + batch_size - 1) // batch_size
        completed_likes = 0
        for request_index in range(total_requests):
            current_batch = min(batch_size, like_count - completed_likes)
            self.client.like_room(room_id, anchor_id, current_batch)
            completed_likes += current_batch
            print(
                f"[OK] 点赞请求 {request_index + 1}/{total_requests}: "
                f"本次 {current_batch} 赞，累计 {completed_likes}/{like_count}"
            )
            wait_seconds = random.uniform(like_interval_min, like_interval_max)
            print(f"[INFO] 等待 {wait_seconds:.1f} 秒后继续点赞")
            time.sleep(wait_seconds)

    def run(self, task_config: TaskConfig) -> None:
        self.client.ensure_main_site_cookie()
        self.client.ensure_live_cookie()

        targets = self.client.resolve_target_rooms()
        if not targets:
            print("[WARN] 未找到可执行的粉丝牌直播间")
            return

        print(f"[INFO] 已获取 LIVE_BUVID: {'LIVE_BUVID' in self.client.session.cookies}")
        print(f"[INFO] 本次共处理 {len(targets)} 个直播间")

        total_like_success = 0
        total_danmaku_success = 0
        for target_index, target in enumerate(targets, start=1):
            room_like_success = 0
            room_status = "开播中" if target.is_living else "未开播"
            print(
                f"[INFO] 开始处理 {target_index}/{len(targets)}: "
                f"{target.target_name} 房间 {target.room_id} ({room_status})"
            )

            if task_config.like_count > 0:
                if not target.is_living:
                    print(f"[INFO] 房间 {target.room_id} 当前未开播，跳过点赞")
                else:
                    self.like_room_multiple(
                        target.room_id,
                        target.anchor_id,
                        task_config.like_count,
                        task_config.like_batch_size,
                        task_config.like_interval_min,
                        task_config.like_interval_max,
                    )
                    room_like_success += task_config.like_count
                    total_like_success += task_config.like_count

            room_danmaku_success = 0
            for danmaku_index in range(task_config.danmaku_count):
                message = choose_message(task_config.danmaku_messages, danmaku_index)
                self.client.send_danmaku(target.room_id, message)
                room_danmaku_success += 1
                total_danmaku_success += 1
                print(f"[OK] 弹幕 {danmaku_index + 1}/{task_config.danmaku_count}: {message}")
                wait_seconds = random.uniform(
                    task_config.danmaku_interval_min,
                    task_config.danmaku_interval_max,
                )
                print(f"[INFO] 等待 {wait_seconds:.1f} 秒后继续发送")
                time.sleep(wait_seconds)

            print(
                f"[INFO] 房间完成: {target.target_name}，"
                f"点赞 {room_like_success} 次，弹幕 {room_danmaku_success} 条"
            )

        if task_config.danmaku_count == 0 and task_config.like_count <= 0:
            print("[WARN] 当前配置未执行任何操作")
            return

        print(
            f"[DONE] 全部任务完成，处理直播间 {len(targets)} 个，"
            f"成功点赞 {total_like_success} 次，成功发送弹幕 {total_danmaku_success} 条"
        )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config_path = Path(args.config)
        config = load_config_from_args(args)
        task_config = parse_task_config(config)

        cookie = ensure_login_cookie(
            task_config=task_config,
            config=config,
            config_path=config_path,
            force_login=args.login,
            run_after_login=args.run_after_login,
        )
        if cookie is None:
            return 0

        task_config = TaskConfig(**{**task_config.__dict__, "cookie": cookie})
        validate_task_config(task_config)

        client = BiliLiveClient(cookie_string=task_config.cookie)
        service = BiliLiveService(client)
        service.run(task_config)
        return 0
    except requests.HTTPError as exc:
        print(f"[ERROR] HTTP 请求失败: {exc}", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"[ERROR] 网络请求异常: {exc}", file=sys.stderr)
    except BiliLiveError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
    except ValueError as exc:
        print(f"[ERROR] 配置解析失败: {exc}", file=sys.stderr)
    return 1
