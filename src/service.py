from datetime import datetime
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
from .utils import choose_message, load_json, save_json


class DailyTaskStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.date = datetime.now().astimezone().date().isoformat()
        self.rooms: dict[str, dict[str, bool | int]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = load_json(self.path)
        if data.get("date") != self.date:
            return
        rooms = data.get("rooms")
        if not isinstance(rooms, dict):
            return
        normalized: dict[str, dict[str, bool | int]] = {}
        for room_id, state in rooms.items():
            if not isinstance(state, dict):
                continue
            normalized[str(room_id)] = {
                "like_done": bool(state.get("like_done", False)),
                "danmaku_done": bool(state.get("danmaku_done", False)),
                "watch_minutes": max(0, int(state.get("watch_minutes", 0) or 0)),
            }
        self.rooms = normalized

    def _room_state(self, room_id: int) -> dict[str, bool | int]:
        key = str(room_id)
        if key not in self.rooms:
            self.rooms[key] = {
                "like_done": False,
                "danmaku_done": False,
                "watch_minutes": 0,
            }
        return self.rooms[key]

    def is_done(self, room_id: int, task_name: str) -> bool:
        return bool(self._room_state(room_id).get(task_name, False))

    def mark_done(self, room_id: int, task_name: str) -> None:
        self._room_state(room_id)[task_name] = True
        self.save()

    def get_watch_minutes(self, room_id: int) -> int:
        return int(self._room_state(room_id).get("watch_minutes", 0) or 0)

    def add_watch_minutes(self, room_id: int, minutes: int) -> None:
        room_state = self._room_state(room_id)
        room_state["watch_minutes"] = self.get_watch_minutes(room_id) + max(0, minutes)
        self.save()

    def save(self) -> None:
        save_json(
            self.path,
            {
                "date": self.date,
                "rooms": self.rooms,
            },
        )


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
    def __init__(self, client: BiliLiveClient, state_store: DailyTaskStateStore) -> None:
        self.client = client
        self.state_store = state_store

    @staticmethod
    def _is_time_check_failed(exc: Exception) -> bool:
        return "time check failed" in str(exc).lower()

    def refresh_watch_session(self, state: WatchState, *, action: str) -> bool:
        try:
            response = self.client.enter_room_heartbeat(
                state.target,
                device_id=state.device_id,
                seq_id=state.heartbeat_count,
                session_uuid=state.session_uuid,
            )
            state.timestamp = int(response.get("timestamp", 0) or 0)
            state.secret_key = str(response.get("secret_key", "") or "")
            state.secret_rule = [int(rule) for rule in (response.get("secret_rule") or [])]
            state.failed_times = 0
            print(f"[OK] {action}观看会话: {state.target.target_name} 房间 {state.target.room_id}")
            return True
        except (requests.RequestException, BiliLiveError, TypeError, ValueError) as exc:
            state.failed_times += 1
            print(
                f"[WARN] {action}观看会话失败: {state.target.target_name} 房间 {state.target.room_id} "
                f"({state.failed_times}/{WATCH_FAILURE_THRESHOLD}) - {exc}"
            )
            return False

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

    def watch_room(
        self,
        target: TargetRoom,
        session_minutes: int,
        watched_today: int,
        daily_target_minutes: int,
    ) -> tuple[int, int]:
        if session_minutes <= 0:
            return 0, 0

        state = WatchState(
            target=target,
            device_id=self.client.get_live_buvid(),
            session_uuid=str(uuid.uuid4()),
            player_guid=str(uuid.uuid4()),
            player_session_id=uuid.uuid4().hex[:11],
        )
        print(
            f"[INFO] 开始观看房间 {target.room_id}，"
            f"本轮计划 {session_minutes} 分钟，今日已看 {watched_today}/{daily_target_minutes} 分钟"
        )

        if not self.refresh_watch_session(state, action="初始化"):
            return 0, 0

        total_rounds = session_minutes * int(WATCH_HEARTBEAT_INTERVAL / WATCH_LOG_INTERVAL)
        for round_index in range(total_rounds):
            if (
                state.failed_times >= WATCH_FAILURE_THRESHOLD
                or state.timestamp <= 0
                or not state.secret_key
                or not state.secret_rule
            ):
                print(f"[WARN] 房间 {state.target.room_id} 观看链路连续失败，提前结束本房间观看")
                break

            round_start = time.monotonic()
            try:
                next_watch_seconds = state.watch_seconds + int(WATCH_LOG_INTERVAL)
                self.client.report_watch_log(
                    state.target,
                    player_guid=state.player_guid,
                    player_session_id=state.player_session_id,
                    watch_seconds=next_watch_seconds,
                )
                state.watch_seconds = next_watch_seconds
                state.failed_times = 0
            except (requests.RequestException, BiliLiveError, TypeError, ValueError) as exc:
                state.failed_times += 1
                print(
                    f"[WARN] 播放日志上报失败: {state.target.target_name} 房间 {state.target.room_id} "
                    f"({state.failed_times}/{WATCH_FAILURE_THRESHOLD}) - {exc}"
                )
                time.sleep(min(WATCH_REQUEST_GAP * 2, WATCH_LOG_INTERVAL))
                continue

            if state.watch_seconds % int(WATCH_HEARTBEAT_INTERVAL) == 0:
                try:
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
                    state.failed_times = 0
                    print(
                        f"[OK] 观看心跳 {watched_today + state.heartbeat_count}/{daily_target_minutes}: "
                        f"{state.target.target_name} 房间 {state.target.room_id} "
                        f"(本轮 {state.heartbeat_count} 分钟，今日累计 {watched_today + state.heartbeat_count} 分钟)"
                    )
                except (requests.RequestException, BiliLiveError, TypeError, ValueError) as exc:
                    state.failed_times += 1
                    print(
                        f"[WARN] 观看心跳失败: {state.target.target_name} 房间 {state.target.room_id} "
                        f"({state.failed_times}/{WATCH_FAILURE_THRESHOLD}) - {exc}"
                    )
                    if self._is_time_check_failed(exc):
                        self.refresh_watch_session(state, action="重建")

            if round_index + 1 < total_rounds:
                elapsed = time.monotonic() - round_start
                wait_seconds = max(0.0, WATCH_LOG_INTERVAL - elapsed)
                time.sleep(wait_seconds)

        watched_minutes = state.heartbeat_count
        total_reports = (state.watch_seconds // int(WATCH_LOG_INTERVAL)) + state.heartbeat_count
        if watched_minutes > 0:
            self.state_store.add_watch_minutes(target.room_id, watched_minutes)
        print(
            f"[INFO] 房间 {target.room_id} 本轮观看结束，"
            f"新增 {watched_minutes} 分钟，今日累计 {watched_today + watched_minutes}/{daily_target_minutes} 分钟"
        )
        return watched_minutes, total_reports

    def watch_live_rooms(
        self,
        targets: list[TargetRoom],
        daily_target_minutes: int,
        session_minutes: int,
    ) -> tuple[int, int]:
        live_targets = [
            target
            for target in targets
            if target.is_living and target.live_key and target.sub_session_key and target.play_url
        ]
        if daily_target_minutes <= 0 or session_minutes <= 0:
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

        ranked_targets = sorted(
            enumerate(live_targets),
            key=lambda item: (self.state_store.get_watch_minutes(item[1].room_id), item[0]),
        )
        watched_rooms = 0
        total_reports = 0
        print(
            f"[INFO] 开始直播间观看任务，共 {len(live_targets)} 个开播房间，"
            f"每房间当天目标 {daily_target_minutes} 分钟，单次运行最多连续观看 {session_minutes} 分钟"
        )
        for _, target in ranked_targets:
            watched_today = self.state_store.get_watch_minutes(target.room_id)
            if watched_today >= daily_target_minutes:
                print(
                    f"[INFO] 房间 {target.room_id} 今日已观看 {watched_today} 分钟，"
                    "达到今日目标，跳过"
                )
                continue

            room_budget = min(session_minutes, daily_target_minutes - watched_today)
            added_minutes, room_reports = self.watch_room(
                target,
                room_budget,
                watched_today,
                daily_target_minutes,
            )
            total_reports += room_reports
            if added_minutes > 0:
                watched_rooms += 1
            break

        print(
            f"[INFO] 观看任务完成，本轮观看 {watched_rooms} 个房间，"
            f"单次目标 {session_minutes} 分钟，"
            f"共发送 {total_reports} 次观看上报"
        )
        return watched_rooms, total_reports

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
                if self.state_store.is_done(target.room_id, "like_done"):
                    print(f"[INFO] 房间 {target.room_id} 今日已完成点赞，跳过")
                elif not target.is_living:
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
                    self.state_store.mark_done(target.room_id, "like_done")

            room_danmaku_success = 0
            if self.state_store.is_done(target.room_id, "danmaku_done"):
                print(f"[INFO] 房间 {target.room_id} 今日已完成弹幕，跳过")
            else:
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
                if task_config.danmaku_count > 0:
                    self.state_store.mark_done(target.room_id, "danmaku_done")

            print(
                f"[INFO] 房间完成: {target.target_name}，"
                f"点赞 {room_like_success} 次，弹幕 {room_danmaku_success} 条"
            )

        watched_rooms, total_watch_reports = self.watch_live_rooms(
            targets,
            task_config.watch_minutes,
            task_config.watch_session_minutes,
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
        state_store = DailyTaskStateStore(config_path.with_name("task_state.json"))
        service = BiliLiveService(client, state_store)
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
