import json
from pathlib import Path
from typing import Any

from .exceptions import BiliLiveError


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise BiliLiveError("配置文件顶层必须是 JSON 对象")
    return data


def save_json(path: Path, data: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")


def parse_cookie_string(cookie_string: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for item in cookie_string.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def choose_message(messages: list[str], index: int) -> str:
    if not messages:
        raise BiliLiveError("至少需要提供一条弹幕内容")
    return messages[index % len(messages)]


def normalize_messages(config: dict[str, Any]) -> list[str]:
    messages = config.get("danmaku_messages")
    if isinstance(messages, list):
        normalized = [str(item).strip() for item in messages if str(item).strip()]
        if normalized:
            return normalized

    single_message = str(config.get("danmaku", "")).strip()
    return [single_message] if single_message else []
