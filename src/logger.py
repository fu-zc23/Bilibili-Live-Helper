from datetime import datetime
import sys


def _now() -> str:
    """返回当前时间字符串，格式: 2026-08-16 12:34:56"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(level: str, message: str = "") -> None:
    """打印带时间戳的日志行。

    level 取值如 INFO / WARN / OK / DONE / ERROR 等。
    ERROR 级别输出到 stderr，其余输出到 stdout。
    """
    stream = sys.stderr if level == "ERROR" else sys.stdout
    timestamp = _now()
    if message:
        print(f"[{timestamp}] [{level}] {message}", file=stream, flush=True)
    else:
        print(f"[{timestamp}] [{level}]", file=stream, flush=True)