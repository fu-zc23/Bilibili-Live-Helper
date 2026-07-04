from dataclasses import dataclass


@dataclass(frozen=True)
class TargetRoom:
    room_id: int
    anchor_id: int
    target_name: str
    is_living: bool


@dataclass(frozen=True)
class TaskConfig:
    cookie: str
    danmaku_messages: list[str]
    danmaku_count: int
    danmaku_interval_min: float
    danmaku_interval_max: float
    like_count: int
    like_batch_size: int
    like_interval_min: float
    like_interval_max: float
