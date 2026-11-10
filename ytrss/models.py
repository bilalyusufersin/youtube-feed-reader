"""Ortak veri tipleri."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ContentType(str, Enum):
    VIDEO = "video"      # Normal video
    SHORT = "short"      # Shorts
    LIVE = "live"        # Canlı yayın (planlı / canlı / tamamlanmış)
    UNKNOWN = "unknown"  # Henüz belirlenemedi

    @property
    def label(self) -> str:
        return CONTENT_TYPE_LABELS[self]


CONTENT_TYPE_LABELS = {
    ContentType.VIDEO: "Video",
    ContentType.SHORT: "Shorts",
    ContentType.LIVE: "Canlı yayın",
    ContentType.UNKNOWN: "Belirlenemedi",
}


class LiveState(str, Enum):
    SCHEDULED = "scheduled"  # Planlandı
    LIVE = "live"            # Şu anda canlı
    COMPLETED = "completed"  # Tamamlandı

    @property
    def label(self) -> str:
        return LIVE_STATE_LABELS[self]


LIVE_STATE_LABELS = {
    LiveState.SCHEDULED: "Planlandı",
    LiveState.LIVE: "Şu anda canlı",
    LiveState.COMPLETED: "Tamamlandı",
}

# Prömiyerler canlı yayın sayılmaz; içerik türü video/shorts kalır, ayrıca
# prömiyer durumu (aynı üç değer) saklanır.
PREMIERE_STATE_LABELS = {
    LiveState.SCHEDULED: "Prömiyer planlandı",
    LiveState.LIVE: "Prömiyer sürüyor",
    LiveState.COMPLETED: "Prömiyer yapıldı",
}


@dataclass(slots=True)
class FeedEntry:
    """RSS/Atom akışından okunmuş tek içerik."""
    video_id: str
    channel_id: str
    title: str
    description: str = ""
    published: int | None = None   # unix saniye
    updated: int | None = None
    thumb_url: str | None = None
    thumb_w: int | None = None
    thumb_h: int | None = None
    views: int | None = None
    link: str | None = None


@dataclass(slots=True)
class FeedResult:
    channel_id: str
    title: str | None
    entries: list[FeedEntry] = field(default_factory=list)


@dataclass(slots=True)
class TabEntry:
    """Kanal sekmesi (/videos, /shorts, /streams) hızlı listelemesinden bir kayıt.

    Hızlı (flat) listelemede alanlar eksik olabilir: süre, tarih vb. None gelebilir.
    """
    video_id: str
    title: str | None
    url: str | None
    duration: int | None
    live_status: str | None   # yt-dlp: is_live, is_upcoming, was_live, post_live, not_live, None
    position: int


@dataclass(slots=True)
class VideoMetadata:
    """yt-dlp ile (indirmeden) okunmuş tek video bilgisi."""
    video_id: str
    ok: bool
    error: str | None = None
    error_kind: str | None = None   # unavailable | private | network | rate_limited | other
    title: str | None = None
    description: str | None = None
    channel_id: str | None = None
    duration: int | None = None
    live_status: str | None = None
    media_type: str | None = None
    release_timestamp: int | None = None
    timestamp: int | None = None
    availability: str | None = None
    thumb_url: str | None = None

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}  # type: ignore[attr-defined]

    @classmethod
    def from_json(cls, data: dict) -> "VideoMetadata":
        return cls(**{k: data.get(k) for k in cls.__slots__ if k in data})  # type: ignore[attr-defined]


@dataclass(slots=True)
class ResolvedChannel:
    channel_id: str
    title: str | None = None
    handle: str | None = None
    avatar_url: str | None = None

