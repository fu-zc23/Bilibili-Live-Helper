import argparse
from pathlib import Path

from .exceptions import BiliLiveError
from .models import TaskConfig
from .utils import load_json, normalize_messages


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="简洁版 B 站直播间点赞/发弹幕脚本")
    parser.add_argument(
        "--config",
        default="live_helper_config.json",
        help="配置文件路径，默认读取当前目录下的 live_helper_config.json",
    )
    parser.add_argument("--cookie", help="完整 Cookie 字符串，可覆盖配置文件中的 cookie")
    parser.add_argument("--danmaku", help="单条弹幕，可覆盖配置文件中的 danmaku_messages")
    parser.add_argument("--danmaku-count", type=int, help="本次运行发送弹幕条数")
    parser.add_argument("--like-click-time", type=int, help="兼容旧配置，表示每次请求点赞数")
    parser.add_argument("--like-count", type=int, help="点赞总次数")
    parser.add_argument("--like-batch-size", type=int, help="每次请求点赞数，建议 1-10")
    parser.add_argument("--login", action="store_true", help="强制执行扫码登录并保存 Cookie")
    parser.add_argument(
        "--run-after-login",
        action="store_true",
        help="扫码登录成功后继续执行点赞/发弹幕任务",
    )
    return parser


def load_config_from_args(args: argparse.Namespace) -> dict:
    config_path = Path(args.config)
    config = load_json(config_path) if config_path.exists() else {}

    if args.cookie:
        config["cookie"] = args.cookie
    if args.danmaku:
        config["danmaku_messages"] = [args.danmaku]
    if args.danmaku_count is not None:
        config["danmaku_count"] = args.danmaku_count
    if args.like_click_time is not None:
        config["like_click_time"] = args.like_click_time
    if args.like_count is not None:
        config["like_count"] = args.like_count
    if args.like_batch_size is not None:
        config["like_batch_size"] = args.like_batch_size

    return config


def parse_task_config(config: dict) -> TaskConfig:
    return TaskConfig(
        cookie=str(config.get("cookie", "")).strip(),
        danmaku_messages=normalize_messages(config),
        danmaku_count=int(config.get("danmaku_count", 1)),
        danmaku_interval_min=float(config.get("danmaku_interval_min", 8)),
        danmaku_interval_max=float(config.get("danmaku_interval_max", 12)),
        like_count=int(config.get("like_count", 10)),
        like_batch_size=int(config.get("like_batch_size", config.get("like_click_time", 10))),
        like_interval_min=float(config.get("like_interval_min", 1.5)),
        like_interval_max=float(config.get("like_interval_max", 3)),
    )


def validate_task_config(task_config: TaskConfig) -> None:
    if task_config.danmaku_count < 0:
        raise BiliLiveError("danmaku_count 不能小于 0")
    if task_config.like_count < 0:
        raise BiliLiveError("like_count 不能小于 0")
    if task_config.like_batch_size <= 0:
        raise BiliLiveError("like_batch_size 必须大于 0")
    if task_config.danmaku_count > 0 and not task_config.danmaku_messages:
        raise BiliLiveError("需要发送弹幕时，danmaku_messages 不能为空")
    if task_config.danmaku_interval_min <= 0 or task_config.danmaku_interval_max <= 0:
        raise BiliLiveError("弹幕间隔必须大于 0")
    if task_config.danmaku_interval_min > task_config.danmaku_interval_max:
        raise BiliLiveError("danmaku_interval_min 不能大于 danmaku_interval_max")
    if task_config.like_interval_min <= 0 or task_config.like_interval_max <= 0:
        raise BiliLiveError("点赞间隔必须大于 0")
    if task_config.like_interval_min > task_config.like_interval_max:
        raise BiliLiveError("like_interval_min 不能大于 like_interval_max")
