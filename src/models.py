from dataclasses import dataclass, field


@dataclass(frozen=True)
class TargetRoom:
    room_id: int
    anchor_id: int
    anchor_level: int
    parent_area_id: int
    area_id: int
    target_name: str
    is_living: bool
    live_key: str = ""
    sub_session_key: str = ""
    play_url: str = ""
    track_id: int = -99998


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
    watch_minutes: int
    watch_session_minutes: int


@dataclass
class WatchState:
    target: TargetRoom
    device_id: str
    session_uuid: str
    player_guid: str
    player_session_id: str
    heartbeat_count: int = 0
    failed_times: int = 0
    watch_seconds: int = 0
    timestamp: int = 0
    secret_key: str = ""
    secret_rule: list[int] = field(default_factory=list)
