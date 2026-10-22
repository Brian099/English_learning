from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Series(Base):
    """系列（视频目录第一层文件夹名）。"""

    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    level: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(8), default="en")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Video(Base):
    __tablename__ = "video"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 相对 MEDIA_ROOT 的路径，POSIX 分隔符
    rel_path: Mapped[str] = mapped_column(String(1024), unique=True)
    title: Mapped[str] = mapped_column(String(300))
    original_name: Mapped[str | None] = mapped_column(String(500))
    series_id: Mapped[int | None] = mapped_column(ForeignKey("series.id", ondelete="SET NULL"))
    language: Mapped[str] = mapped_column(String(8), default="en")

    # 媒体属性
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    speech_ms: Mapped[int] = mapped_column(Integer, default=0)   # 有语音的时长，完成判定的分母
    speech_pct: Mapped[float] = mapped_column(Float, default=0.0)
    wpm: Mapped[float] = mapped_column(Float, default=0.0)
    lufs: Mapped[float | None] = mapped_column(Float)            # 原始文件响度（看模式用）
    audio_lufs: Mapped[float | None] = mapped_column(Float)      # 归一化后音轨的实测响度（听模式用）
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    audio_rel: Mapped[str | None] = mapped_column(String(1024))   # 仅音轨版本（磨耳朵）
    cover_rel: Mapped[str | None] = mapped_column(String(1024))

    # 难度分级
    level: Mapped[str | None] = mapped_column(String(8))          # L1..L5
    level_score: Mapped[float | None] = mapped_column(Float)
    unique_words: Mapped[int] = mapped_column(Integer, default=0)
    oov_ratio: Mapped[float | None] = mapped_column(Float)

    tags: Mapped[str | None] = mapped_column(String(500))
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    mtime: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Transcript(Base):
    """ASR 结果。一台设备转一次即可，按 video 一对一。"""

    __tablename__ = "transcript"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("video.id", ondelete="CASCADE"), unique=True, index=True
    )
    model: Mapped[str] = mapped_column(String(64), default="small.en")
    language: Mapped[str] = mapped_column(String(8), default="en")
    text: Mapped[str] = mapped_column(Text, default="")
    segments: Mapped[list | None] = mapped_column(JSON)      # [{start,end,text}]
    speech_ms: Mapped[int] = mapped_column(Integer, default=0)
    words: Mapped[int] = mapped_column(Integer, default=0)
    params_hash: Mapped[str | None] = mapped_column(String(64))  # 幂等键
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Student(Base):
    __tablename__ = "student"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    avatar: Mapped[str | None] = mapped_column(String(200))
    daily_target_min: Mapped[int] = mapped_column(Integer, default=20)
    # 当前级别 L1~L5；留空表示由最近完成的内容自动推断
    level: Mapped[str | None] = mapped_column(String(8))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Device(Base):
    """已配对设备。token 只存 sha256，原文只在配对时返回一次。"""

    __tablename__ = "device"

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100), default="未命名设备")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    platform: Mapped[str | None] = mapped_column(String(50))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PairCode(Base):
    __tablename__ = "pair_code"

    code: Mapped[str] = mapped_column(String(12), primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlayEvent(Base):
    """只追加的播放事件。event_id 由客户端生成（UUIDv7），保证重传幂等。"""

    __tablename__ = "play_event"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_play_event_event_id"),
        Index("ix_play_event_student_started", "student_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64))
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id", ondelete="CASCADE"))
    device_id: Mapped[int | None] = mapped_column(ForeignKey("device.id", ondelete="SET NULL"))
    video_id: Mapped[int] = mapped_column(ForeignKey("video.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_ms: Mapped[int] = mapped_column(Integer, default=0)
    max_position_ms: Mapped[int] = mapped_column(Integer, default=0)
    seek_count: Mapped[int] = mapped_column(Integer, default=0)
    avg_rate: Mapped[float] = mapped_column(Float, default=1.0)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    client_version: Mapped[str | None] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DailyStat(Base):
    """按天聚合，报表查询 O(1)。由事件写入后重算，不手工维护。"""

    __tablename__ = "daily_stat"

    student_id: Mapped[int] = mapped_column(
        ForeignKey("student.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    effective_ms: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    session_count: Mapped[int] = mapped_column(Integer, default=0)


class Plan(Base):
    __tablename__ = "plan"
    __table_args__ = (UniqueConstraint("student_id", "day", name="uq_plan_student_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("student.id", ondelete="CASCADE"))
    day: Mapped[date] = mapped_column(Date)
    target_min: Mapped[int] = mapped_column(Integer, default=20)
    note: Mapped[str | None] = mapped_column(Text)
    # manual = 家长手动排的；auto = 程序自动生成（家长可覆盖）
    source: Mapped[str] = mapped_column(String(16), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlanItem(Base):
    __tablename__ = "plan_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("plan.id", ondelete="CASCADE"), index=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("video.id", ondelete="CASCADE"))
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(16), default="new")  # new | review
