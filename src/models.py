from dataclasses import dataclass, field


@dataclass(frozen=True)
class MedalTaskInfo:
    """单个亲密度任务的信息，来自 API 返回的 task_info 条目"""
    jump_type: str          # 任务类型: feedLight / watchLive / sendGift / sendDanmu / like
    title: str              # 任务名称
    sub_title: str          # 进度文本，如 "每日上限 7/10"
    add_text: str           # 奖励文本，如 "亲密度+1"
    is_done: bool           # 是否已完成
    daily_limit: int = 0    # 每日总上限
    daily_current: int = 0  # 当日已完成次数


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
    # 从 API 获取的任务信息
    medal_is_lighted: bool = True
    tasks: list[MedalTaskInfo] = field(default_factory=list)


@dataclass(frozen=True)
class TaskConfig:
    cookie: str
    danmaku_messages: list[str]
    danmaku_interval_min: float
    danmaku_interval_max: float
    like_batch_size: int
    like_interval_min: float
    like_interval_max: float
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
