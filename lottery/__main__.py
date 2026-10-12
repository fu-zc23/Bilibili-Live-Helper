"""天选时刻扫描入口（cron 短轮询运行: python3 -m lottery）

流程: 初始化 Cookie → 拉关注列表 → 增量解析/缓存各主播房间号
     → 逐房间 Check 进行中的天选 → 条件筛选（跳过送礼类）
     → 未参与过则 Join → lottery id 记入 state 去重
     → Join 成功后派生 stay.py 驻留子进程（维持在线到开奖）
"""
from pathlib import Path
import subprocess
import sys
import time

import requests

from src.client import BiliLiveClient
from src.config import build_parser, load_config_from_args, parse_task_config
from src.exceptions import BiliLiveError
from src.logger import log
from src.utils import load_json, save_json

STATE_PATH = Path(__file__).parent.parent / "lottery_state.json"
# 已参与记录保留天数（超过后清理，防止 state 无限增长）
STATE_TTL_DAYS = 7
ROOM_QUERY_GAP = 0.3  # 房间间请求间隔（秒）


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"joined": {}, "rooms": {}}
    try:
        state = load_json(STATE_PATH)
        if isinstance(state.get("joined"), dict) and isinstance(state.get("rooms"), dict):
            return state
    except (ValueError, OSError):
        pass
    return {"joined": {}, "rooms": {}}


def save_state(state: dict) -> None:
    save_json(STATE_PATH, state)


def prune_state(state: dict) -> None:
    """清掉过期的参与记录（天选 id 全局唯一且过期后不再出现）"""
    cutoff = time.time() - STATE_TTL_DAYS * 86400
    joined = state.get("joined") or {}
    stale = [k for k, ts in joined.items() if float(ts) < cutoff]
    for k in stale:
        del joined[k]
    if stale:
        log("INFO", f"清理 {len(stale)} 条过期的天选参与记录")


def resolve_follow_rooms(client: BiliLiveClient, state: dict) -> list[tuple[str, int, int]]:
    """关注列表 → [(uname, mid, room_id), ...]。房间号缓存于 state，仅新增关注触发空间查询"""
    rooms_cache: dict = state.setdefault("rooms", {})
    users = client.get_followings()
    if not users:
        return []

    resolved: list[tuple[str, int, int]] = []
    new_entries = 0
    for u in users:
        uname = str(u.get("uname", "") or "")
        mid = int(u.get("mid", 0) or 0)
        if mid <= 0:
            continue
        cached = rooms_cache.get(str(mid))
        if cached is not None:
            room_id = int(cached)
            if room_id > 0:
                resolved.append((uname, mid, room_id))
            continue

        # 新关注的用户: 查空间信息解析房间号
        # 接口明确返回无直播间才缓存 0；请求失败则跳过，下轮重试（避免临时风控被永久缓存）
        try:
            room_id = client.get_user_room_id(mid)
        except (requests.RequestException, BiliLiveError) as exc:
            log("WARN", f"解析 {uname}({mid}) 房间号失败，下轮重试: {exc}")
            continue
        rooms_cache[str(mid)] = room_id
        new_entries += 1
        if room_id > 0:
            resolved.append((uname, mid, room_id))

    if new_entries:
        log("INFO", f"解析 {new_entries} 个新增关注的房间号，累计有直播间的关注 {len(resolved)} 个")
    return resolved


def should_join(lottery: dict) -> tuple[bool, str]:
    """天选筛选: 返回 (是否参与, 原因)。参与条件不足时由服务器在 Join 时裁决"""
    gift_id = int(lottery.get("gift_id", 0) or 0)
    award_name = str(lottery.get("award_name", "") or "")

    if gift_id > 0:
        gift_name = str(lottery.get("gift_name", "") or "").strip() or f"礼物#{gift_id}"
        gift_num = int(lottery.get("gift_num", 0) or 0)
        return False, f"送礼类天选（需送 {gift_name}x{gift_num}）"
    if not award_name:
        return False, "缺少奖品信息"
    return True, ""


def start_stay_process(room_id: int, lottery_id: int, remain_seconds: int) -> None:
    """以 detached 子进程驻留到开奖（维持弹幕 WS 在线，满足开奖时在直播间的判定）"""
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "lottery.stay",
                str(room_id),
                str(lottery_id),
                str(max(0, remain_seconds)),
            ],
            stdout=sys.stdout if sys.stdout else None,
            stderr=sys.stderr if sys.stderr else None,
            start_new_session=True,  # 脱离 cron 进程组，父进程退出后继续运行
        )
        log("INFO", f"天选 #{lottery_id} 已启动驻留子进程（剩余 {remain_seconds}s + 开奖缓冲）")
    except OSError as exc:
        log("WARN", f"天选 #{lottery_id} 启动驻留子进程失败: {exc}")


def scan_and_join(client: BiliLiveClient) -> int:
    state = load_state()
    prune_state(state)

    try:
        rooms = resolve_follow_rooms(client, state)
    except (requests.RequestException, BiliLiveError) as exc:
        log("WARN", f"获取关注列表失败: {exc}")
        return 0
    if not rooms:
        log("WARN", "关注列表中无带直播间的房间，跳过本轮")
        save_state(state)
        return 0

    joined = state["joined"]
    join_count = 0
    active_seen = 0

    for uname, mid, room_id in rooms:
        try:
            lottery = client.get_anchor_lottery(room_id)
        except (requests.RequestException, BiliLiveError) as exc:
            log("WARN", f"查询 {uname} 房间 {room_id} 天选失败: {exc}")
            time.sleep(ROOM_QUERY_GAP)
            continue

        if lottery is None:
            time.sleep(ROOM_QUERY_GAP)
            continue

        active_seen += 1
        lottery_id = int(lottery.get("id", 0) or 0)
        key = str(lottery_id)
        remain = int(lottery.get("time", 0) or 0)
        award = str(lottery.get("award_name", "") or "")
        award_num = int(lottery.get("award_num", 0) or 0)
        require_text = str(lottery.get("require_text", "") or "")

        if key in joined:
            log("INFO", f"{uname}: 天选 #{lottery_id} 已处理过，跳过")
            time.sleep(ROOM_QUERY_GAP)
            continue

        ok, reason = should_join(lottery)
        if not ok:
            log("INFO", f"{uname}: 天选 #{lottery_id} ({award}x{award_num}) {reason}，跳过")
            joined[key] = time.time()
            time.sleep(ROOM_QUERY_GAP)
            continue

        log(
            "INFO",
            f"{uname}: 天选 #{lottery_id} {award}x{award_num} 剩余 {remain}s "
            f"条件[{require_text or '无'}]，尝试参与",
        )
        try:
            client.join_anchor_lottery(room_id, lottery_id)
            joined[key] = time.time()
            join_count += 1
            log("OK", f"{uname}: 已参与天选 #{lottery_id}")
            start_stay_process(room_id, lottery_id, remain)
        except (requests.RequestException, BiliLiveError) as exc:
            log("WARN", f"{uname}: 参与天选 #{lottery_id} 失败: {exc}")
            joined[key] = time.time()  # 失败也记录，防止风控下高频重试

        time.sleep(ROOM_QUERY_GAP)

    save_state(state)
    log("DONE", f"本轮天选扫描完成: 房间 {len(rooms)}，活跃天选 {active_seen}，新参与 {join_count}")
    return join_count


def run(client: BiliLiveClient) -> int:
    client.ensure_main_site_cookie()
    client.ensure_live_cookie()
    scan_and_join(client)
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = load_config_from_args(args)
        task_config = parse_task_config(config)
        if not task_config.cookie:
            log("ERROR", "未检测到 Cookie，请先运行 main.py 完成扫码登录")
            return 1

        client = BiliLiveClient(cookie_string=task_config.cookie)
        return run(client)
    except requests.HTTPError as exc:
        log("ERROR", f"HTTP 请求失败: {exc}")
    except requests.RequestException as exc:
        log("ERROR", f"网络请求异常: {exc}")
    except BiliLiveError as exc:
        log("ERROR", f"{exc}")
    except ValueError as exc:
        log("ERROR", f"配置解析失败: {exc}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
