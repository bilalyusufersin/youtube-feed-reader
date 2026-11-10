"""YouTube kanal RSS/Atom akışlarını okuma (feedparser)."""
from __future__ import annotations

import calendar
import logging

import feedparser

from ..models import FeedEntry, FeedResult
from .urls import VIDEO_ID_RE

log = logging.getLogger(__name__)


class FeedParseError(Exception):
    pass


def _ts(struct) -> int | None:
    if not struct:
        return None
    try:
        return int(calendar.timegm(struct))
    except (TypeError, ValueError, OverflowError):
        return None


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_feed(content: bytes | str, expected_channel_id: str | None = None) -> FeedResult:
    """YouTube Atom akışını ayrıştırır.

    RSS yalnızca son ~15 içeriği verir; bu fonksiyon sadece akışta olanı döndürür,
    kanal geçmişinin tamamı hakkında hiçbir şey söylemez.
    """
    d = feedparser.parse(content)
    if d.get("bozo") and not d.get("entries") and not d.get("feed", {}).get("title"):
        raise FeedParseError(f"Akış okunamadı: {d.get('bozo_exception')}")
    feed = d.get("feed", {})
    title = feed.get("title")
    entries: list[FeedEntry] = []
    for e in d.get("entries", []):
        vid = e.get("yt_videoid")
        if not vid:
            eid = e.get("id", "")
            vid = eid.rsplit(":", 1)[-1] if eid.startswith("yt:video:") else None
        if not vid or not VIDEO_ID_RE.match(vid):
            log.debug("Geçersiz video kimliği atlandı: %r", vid)
            continue
        cid = e.get("yt_channelid") or expected_channel_id or ""
        if cid and not cid.startswith("UC"):
            cid = "UC" + cid
        thumb = (e.get("media_thumbnail") or [{}])[0]
        stats = e.get("media_statistics") or {}
        desc = e.get("summary") or ""
        if not desc:
            for c in e.get("media_content", []) or []:
                desc = c.get("description") or ""
                if desc:
                    break
        entries.append(FeedEntry(
            video_id=vid,
            channel_id=cid,
            title=(e.get("title") or "").strip(),
            description=desc.strip(),
            published=_ts(e.get("published_parsed")),
            updated=_ts(e.get("updated_parsed")),
            thumb_url=thumb.get("url"),
            thumb_w=_int(thumb.get("width")),
            thumb_h=_int(thumb.get("height")),
            views=_int(stats.get("views")),
            link=e.get("link"),
        ))
    cid = expected_channel_id
    if not cid:
        fc = feed.get("yt_channelid")
        if fc:
            cid = fc if fc.startswith("UC") else "UC" + fc
        elif entries:
            cid = entries[0].channel_id
    return FeedResult(channel_id=cid or "", title=title, entries=entries)

