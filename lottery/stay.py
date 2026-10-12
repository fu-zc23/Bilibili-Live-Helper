"""天选时刻驻留子进程: 维持弹幕 WebSocket 在线直到开奖

用法: python3 -m lottery.stay <room_id> <lottery_id> <remain_seconds>
由 lottery 包的扫描入口在 Join 成功后以 detached 子进程启动。
"开奖时刻需在直播间内" 的在线判定基于弹幕 WS 连接，故需驻留。
"""
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import websocket

from src.client import BiliLiveClient
from src.config import build_parser, load_config_from_args, parse_task_config
from src.logger import log

WS_HEADER_LEN = 16
HEARTBEAT_INTERVAL = 30.0
# 开奖流程缓冲: 到点后多挂一会，覆盖开奖判定和结果广播
SETTLE_BUFFER_SECONDS = 90


def pack(op: int, body: bytes = b"") -> bytes:
    return struct.pack(">IHHII", WS_HEADER_LEN + len(body), WS_HEADER_LEN, 1, op, 1) + body


def main() -> int:
    if len(sys.argv) != 4:
        log("ERROR", "用法: python3 -m lottery.stay <room_id> <lottery_id> <remain_seconds>")
        return 1
    room_id = int(sys.argv[1])
    lottery_id = int(sys.argv[2])
    remain_seconds = int(sys.argv[3])

    parser = build_parser()
    args, _ = parser.parse_known_args()
    config = load_config_from_args(args)
    task_config = parse_task_config(config)
    if not task_config.cookie:
        log("ERROR", "未检测到 Cookie")
        return 1

    client = BiliLiveClient(cookie_string=task_config.cookie)
    client.ensure_main_site_cookie()
    client.ensure_live_cookie()

    payload = client.request(
        "GET",
        "https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo",
        params=client.sign_wbi_params({"id": room_id, "type": 0}),
        headers=client.live_headers(room_id),
    )
    data = payload.get("data") or {}
    token = str(data.get("token", ""))
    host_list = data.get("host_list") or []
    host = (host_list[-1] or {}).get("host", "broadcastlv.chat.bilibili.com")
    url = f"wss://{host}:2245/sub"

    auth = json.dumps({
        "uid": int(client.uid),
        "roomid": room_id,
        "protover": 3,
        "platform": "web",
        "type": 2,
        "key": token,
    }, ensure_ascii=False).encode()

    deadline = time.time() + remain_seconds + SETTLE_BUFFER_SECONDS
    log("INFO", f"驻留天选 #{lottery_id} 房间 {room_id}，时长 {int(deadline - time.time())}s")

    last_heartbeat = 0.0
    while time.time() < deadline:
        try:
            ws = websocket.create_connection(url, timeout=10)
            ws.send(pack(7, auth))
            ws.settimeout(5)
            last_heartbeat = 0.0

            while time.time() < deadline:
                if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                    ws.send(pack(2))
                    last_heartbeat = time.time()
                try:
                    ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
            ws.close()
            log("OK", f"天选 #{lottery_id} 驻留结束（房间 {room_id}）")
            return 0
        except Exception as exc:
            log("WARN", f"天选 #{lottery_id} WS 断开，5s 后重连: {exc}")
            time.sleep(5)

    log("OK", f"天选 #{lottery_id} 驻留结束（房间 {room_id}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
