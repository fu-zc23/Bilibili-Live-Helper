from pathlib import Path
import random
import sys
import time
import uuid

import requests

from .client import BiliLiveClient
from .config import build_parser, load_config_from_args, parse_task_config, validate_task_config
from .constants import (
    DUMMY_COOKIE,
    WATCH_FAILURE_THRESHOLD,
    WATCH_HEARTBEAT_INTERVAL,
    WATCH_LOG_INTERVAL,
    WATCH_REQUEST_GAP,
)
from .exceptions import BiliLiveError
from .models import TaskConfig, TargetRoom, WatchState
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

    def watch_live_rooms(self, targets: list[TargetRoom], watch_minutes: int) -> tuple[int, int]:
        live_targets = [
            target
            for target in targets
            if target.is_living and target.live_key and target.sub_session_key and target.play_url
        ]
        if watch_minutes <= 0:
            return 0, 0
        skipped_targets = [
            target
            for target in targets
            if target.is_living and target not in live_targets
        ]
        for target in skipped_targets:
            print(
                f"[WARN] 房间 {target.room_id} 缺少观看链路参数，跳过挂机 "
                f"(live_key={bool(target.live_key)}, sub_session_key={bool(target.sub_session_key)}, "
                f"play_url={bool(target.play_url)})"
            )
        if not live_targets:
            print("[WARN] 当前没有开播房间，跳过观看任务")
            return 0, 0

        device_id = self.client.get_live_buvid()
        states = [
            WatchState(
                target=target,
                device_id=device_id,
                session_uuid=str(uuid.uuid4()),
                player_guid=str(uuid.uuid4()),
                player_session_id=uuid.uuid4().hex[:11],
            )
            for target in live_targets
        ]
        print(
            f"[INFO] 开始直播间观看任务，共 {len(states)} 个开播房间，"
            f"每个房间挂机 {watch_minutes} 分钟"
        )

        for state in states:
            try:
                response = self.client.enter_room_heartbeat(
                    state.target,
                    device_id=state.device_id,
                    seq_id=0,
                    session_uuid=state.session_uuid,
                )
                state.timestamp = int(response.get("timestamp", 0) or 0)
                state.secret_key = str(response.get("secret_key", "") or "")
                state.secret_rule = [int(rule) for rule in (response.get("secret_rule") or [])]
                print(f"[OK] 初始化观看会话: {state.target.target_name} 房间 {state.target.room_id}")
            except (requests.RequestException, BiliLiveError, TypeError, ValueError) as exc:
                state.failed_times += 1
                print(f"[WARN] 初始化观看会话失败: {state.target.target_name} 房间 {state.target.room_id} - {exc}")

        total_rounds = watch_minutes * int(WATCH_HEARTBEAT_INTERVAL / WATCH_LOG_INTERVAL)
        for round_index in range(total_rounds):
            active_states = [
                state
                for state in states
                if state.failed_times < WATCH_FAILURE_THRESHOLD
                and state.timestamp > 0
                and state.secret_key
                and state.secret_rule
            ]
            if not active_states:
                print("[WARN] 所有房间的观看链路都已连续失败，提前结束观看任务")
                break

            round_start = time.monotonic()
            for state_index, state in enumerate(active_states, start=1):
                try:
                    next_watch_seconds = state.watch_seconds + int(WATCH_LOG_INTERVAL)
                    self.client.report_watch_log(
                        state.target,
                        player_guid=state.player_guid,
                        player_session_id=state.player_session_id,
                        watch_seconds=next_watch_seconds,
                    )
                    state.watch_seconds = next_watch_seconds
                    if state.watch_seconds % int(WATCH_HEARTBEAT_INTERVAL) == 0:
                        response = self.client.send_watch_heartbeat(
                            state.target,
                            device_id=state.device_id,
                            seq_id=state.heartbeat_count + 1,
                            session_uuid=state.session_uuid,
                            timestamp=state.timestamp,
                            secret_key=state.secret_key,
                            secret_rule=state.secret_rule,
                        )
                        state.timestamp = int(response.get("timestamp", 0) or 0)
                        state.secret_key = str(response.get("secret_key", "") or "")
                        state.secret_rule = [
                            int(rule)
                            for rule in (response.get("secret_rule") or [])
                        ]
                        state.heartbeat_count += 1
                        print(
                            f"[OK] 观看心跳 {state.heartbeat_count}/{watch_minutes}: "
                            f"{state.target.target_name} 房间 {state.target.room_id} "
                            f"(累计 {state.watch_seconds // 60} 分钟)"
                        )

                    state.failed_times = 0
                except (requests.RequestException, BiliLiveError, TypeError, ValueError) as exc:
                    state.failed_times += 1
                    print(
                        f"[WARN] 观看上报失败: {state.target.target_name} 房间 {state.target.room_id} "
                        f"({state.failed_times}/{WATCH_FAILURE_THRESHOLD}) - {exc}"
                    )

                if state_index < len(active_states):
                    time.sleep(WATCH_REQUEST_GAP)

            if round_index + 1 < total_rounds:
                elapsed = time.monotonic() - round_start
                wait_seconds = max(0.0, WATCH_LOG_INTERVAL - elapsed)
                time.sleep(wait_seconds)

        finished_rooms = sum(
            1 for state in states if state.watch_seconds >= watch_minutes * 60
        )
        total_reports = sum((state.watch_seconds // int(WATCH_LOG_INTERVAL)) + state.heartbeat_count for state in states)
        print(
            f"[INFO] 观看任务完成，达成 {finished_rooms}/{len(states)} 个房间，"
            f"共发送 {total_reports} 次观看上报"
        )
        return finished_rooms, total_reports

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
        total_watch_reports = 0
        watched_rooms = 0
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

        watched_rooms, total_watch_reports = self.watch_live_rooms(
            targets,
            task_config.watch_minutes,
        )

        if (
            task_config.danmaku_count == 0
            and task_config.like_count <= 0
            and task_config.watch_minutes <= 0
        ):
            print("[WARN] 当前配置未执行任何操作")
            return

        print(
            f"[DONE] 全部任务完成，处理直播间 {len(targets)} 个，"
            f"成功点赞 {total_like_success} 次，成功发送弹幕 {total_danmaku_success} 条，"
            f"观看完成 {watched_rooms} 个房间 / {total_watch_reports} 次上报"
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
