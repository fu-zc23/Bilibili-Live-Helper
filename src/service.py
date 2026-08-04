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
from .models import MedalTaskInfo, TaskConfig, TargetRoom, WatchState
from .utils import choose_message, save_json


def _find_task(tasks: list[MedalTaskInfo], jump_type: str) -> MedalTaskInfo | None:
    """在任务列表中查找指定类型的任务"""
    for t in tasks:
        if t.jump_type == jump_type:
            return t
    return None


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

    def watch_live_rooms(
        self,
        targets: list[TargetRoom],
        session_minutes: int,
    ) -> None:
        """观看直播：为每个开播房间挂机观看，直到 API 报告 watchLive 任务完成"""
        if session_minutes <= 0:
            return

        live_targets = [
            t for t in targets
            if t.is_living and t.live_key and t.sub_session_key and t.play_url
        ]
        if not live_targets:
            print("[INFO] 没有符合条件的开播房间，跳过观看")
            return

        # 按还没完成的观看任务排序
        pending = []
        for t in live_targets:
            task = _find_task(t.tasks, "watchLive")
            if task and not task.is_done and task.daily_current < task.daily_limit:
                pending.append((task.daily_current, t))

        if not pending:
            # 都已达到上限，检查是否还有未完成的
            still_pending = [
                t for t in live_targets
                if not (_find_task(t.tasks, "watchLive") or MedalTaskInfo("", "", "", "", True, 0, 0)).is_done
            ]
            if not still_pending:
                print("[INFO] 所有房间观看任务已完成，跳过")
                return
            # 还有未完成但没上限信息的，保守做一个 session
            pending = [(0, t) for t in still_pending]

        pending.sort(key=lambda x: x[0])

        print(f"[INFO] 开始观看任务，共 {len(pending)} 个房间待处理")
        for _, target in pending:
            task = _find_task(target.tasks, "watchLive")
            remaining = (task.daily_limit - task.daily_current) * 15 if (task and task.daily_limit > 0) else session_minutes
            budget = min(session_minutes, max(1, remaining))

            print(
                f"[INFO] 观看 {target.target_name} (房间 {target.room_id})，"
                f"本轮 {budget} 分钟" +
                (f" (每日上限 {task.daily_limit * 15} 分钟, 已完成 {task.daily_current * 15} 分钟)" if task else "")
            )

            self.watch_room(target, budget)
            break  # 单次只挂一个房间，避免过长运行

    def watch_room(self, target: TargetRoom, session_minutes: int) -> None:
        if session_minutes <= 0:
            return

        state = WatchState(
            target=target,
            device_id=self.client.get_live_buvid(),
            session_uuid=str(uuid.uuid4()),
            player_guid=str(uuid.uuid4()),
            player_session_id=uuid.uuid4().hex[:11],
        )

        if not self.refresh_watch_session(state, action="初始化"):
            return

        total_rounds = session_minutes * int(WATCH_HEARTBEAT_INTERVAL / WATCH_LOG_INTERVAL)
        for round_index in range(total_rounds):
            if (
                state.failed_times >= WATCH_FAILURE_THRESHOLD
                or state.timestamp <= 0
                or not state.secret_key
                or not state.secret_rule
            ):
                print(f"[WARN] 房间 {state.target.room_id} 观看链路连续失败，提前结束")
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
                print(f"[WARN] 播放日志上报失败: {state.target.target_name} - {exc}")
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
                    state.secret_rule = [int(r) for r in (response.get("secret_rule") or [])]
                    state.heartbeat_count += 1
                    state.failed_times = 0
                    print(
                        f"[OK] 观看心跳 {state.heartbeat_count}/{session_minutes}: "
                        f"{state.target.target_name} 房间 {state.target.room_id}"
                    )
                except (requests.RequestException, BiliLiveError, TypeError, ValueError) as exc:
                    state.failed_times += 1
                    print(f"[WARN] 观看心跳失败: {state.target.target_name} - {exc}")
                    self.refresh_watch_session(state, action="重建")

            if round_index + 1 < total_rounds:
                elapsed = time.monotonic() - round_start
                time.sleep(max(0.0, WATCH_LOG_INTERVAL - elapsed))

    def run(self, task_config: TaskConfig) -> None:
        self.client.ensure_main_site_cookie()
        self.client.ensure_live_cookie()

        targets = self.client.resolve_target_rooms()
        if not targets:
            print("[WARN] 未找到可执行的粉丝牌直播间")
            return

        # 获取每个房间的勋章任务信息
        enriched_targets: list[TargetRoom] = []
        for target in targets:
            try:
                medal_data = self.client.get_activated_medal_info(target.anchor_id)
                is_lighted, tasks = self.client.parse_medal_tasks(medal_data)
                enriched_targets.append(TargetRoom(
                    room_id=target.room_id,
                    anchor_id=target.anchor_id,
                    anchor_level=target.anchor_level,
                    parent_area_id=target.parent_area_id,
                    area_id=target.area_id,
                    target_name=target.target_name,
                    is_living=target.is_living,
                    live_key=target.live_key,
                    sub_session_key=target.sub_session_key,
                    play_url=target.play_url,
                    track_id=target.track_id,
                    medal_is_lighted=is_lighted,
                    tasks=tasks,
                ))
            except (requests.RequestException, BiliLiveError, ValueError) as exc:
                print(f"[WARN] 获取 {target.target_name} 任务信息失败: {exc}")
                enriched_targets.append(target)
        targets = enriched_targets

        print(f"[INFO] 已获取 LIVE_BUVID: {'LIVE_BUVID' in self.client.session.cookies}")
        print(f"[INFO] 本次共处理 {len(targets)} 个直播间")

        total_like_success = 0
        total_danmaku_success = 0

        for target_index, target in enumerate(targets, start=1):
            room_status = "开播中" if target.is_living else "未开播"
            print(
                f"[INFO] === {target_index}/{len(targets)}: "
                f"{target.target_name} 房间 {target.room_id} ({room_status}) ==="
            )

            # --- 弹幕任务 ---
            danmaku_task = _find_task(target.tasks, "sendDanmu")
            danmaku_needed = 0
            if danmaku_task and not danmaku_task.is_done:
                danmaku_needed = max(0, danmaku_task.daily_limit - danmaku_task.daily_current)

            if danmaku_needed <= 0:
                status = f"已完成 ({danmaku_task.daily_current}/{danmaku_task.daily_limit})" if danmaku_task else "无任务"
                print(f"[INFO] 弹幕任务 {status}，跳过")
            else:
                print(f"[INFO] 弹幕任务还需 {danmaku_needed} 条 (上限 {danmaku_task.daily_limit})")
                if not task_config.danmaku_messages:
                    print("[WARN] 未配置弹幕消息，跳过弹幕")
                else:
                    for i in range(danmaku_needed):
                        message = choose_message(task_config.danmaku_messages, i)
                        try:
                            self.client.send_danmaku(target.room_id, message)
                            total_danmaku_success += 1
                            print(f"[OK] 弹幕 {i+1}/{danmaku_needed}: {message}")
                        except (requests.RequestException, BiliLiveError) as exc:
                            print(f"[WARN] 弹幕发送失败: {exc}")
                            break
                        if i + 1 < danmaku_needed:
                            wait = random.uniform(
                                task_config.danmaku_interval_min,
                                task_config.danmaku_interval_max,
                            )
                            print(f"[INFO] 等待 {wait:.1f} 秒")
                            time.sleep(wait)

            # --- 点赞任务 ---
            like_task = _find_task(target.tasks, "like")
            like_needed = 0
            if like_task and not like_task.is_done:
                like_needed = max(0, like_task.daily_limit - like_task.daily_current) * 30

            if like_needed <= 0:
                status = f"已完成 ({like_task.daily_current}/{like_task.daily_limit})" if like_task else "无任务"
                print(f"[INFO] 点赞任务 {status}，跳过")
            elif not target.is_living:
                print(f"[INFO] 点赞任务还需 {like_needed} 赞，但房间未开播，跳过")
            else:
                print(f"[INFO] 点赞任务还需 {like_needed} 赞 (上限 {like_task.daily_limit * 30})")
                self.like_room_multiple(
                    target.room_id,
                    target.anchor_id,
                    like_needed,
                    task_config.like_batch_size,
                    task_config.like_interval_min,
                    task_config.like_interval_max,
                )
                total_like_success += like_needed

            time.sleep(random.uniform(1.0, 3.0))  # 房间间隔，避免过快切换
            print(
                f"[INFO] 房间完成: {target.target_name}，"
                f"弹幕 {total_danmaku_success} 条，点赞 {total_like_success} 次"
            )

        # --- 观看任务 ---
        self.watch_live_rooms(targets, task_config.watch_session_minutes)

        print(
            f"[DONE] 全部任务完成，处理直播间 {len(targets)} 个，"
            f"弹幕 {total_danmaku_success} 条，点赞 {total_like_success} 次"
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

        task_config = TaskConfig(
            cookie=cookie,
            danmaku_messages=task_config.danmaku_messages,
            danmaku_interval_min=task_config.danmaku_interval_min,
            danmaku_interval_max=task_config.danmaku_interval_max,
            like_batch_size=task_config.like_batch_size,
            like_interval_min=task_config.like_interval_min,
            like_interval_max=task_config.like_interval_max,
            watch_session_minutes=task_config.watch_session_minutes,
        )
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
