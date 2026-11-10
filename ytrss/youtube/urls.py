"""Kullanıcının girdiği kanal adresini ayrıştırma (ağ kullanmaz)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlparse

CHANNEL_ID_RE = re.compile(r"^UC[0-9A-Za-z_-]{22}$")
VIDEO_ID_RE = re.compile(r"^[0-9A-Za-z_-]{11}$")
_HANDLE_RE = re.compile(r"^@[^\s/?#]{1,100}$")

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


class InvalidChannelInput(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChannelRef:
    """Ayrıştırılmış kanal başvurusu.

    kind: "channel_id" (kesin), "handle" (@ad, ağ ile çözülmeli),
          "legacy" (/c/ veya /user/ adresi, ağ ile çözülmeli),
          "video" (video kimliği; yalnızca allow_video=True ise kanalını bulmak için)
    """
    kind: str
    value: str

    @property
    def is_resolved(self) -> bool:
        return self.kind == "channel_id"

    def page_url(self) -> str:
        if self.kind == "channel_id":
            return f"https://www.youtube.com/channel/{self.value}"
        if self.kind == "handle":
            return "https://www.youtube.com/" + quote(self.value, safe="@")
        if self.kind == "video":
            return f"https://www.youtube.com/watch?v={self.value}"
        return "https://www.youtube.com/" + self.value


def parse_channel_input(text: str, *, allow_video: bool = False) -> ChannelRef:
    """Kanal RSS bağlantısı, /channel/UC… adresi, @kullanıcı adresi veya @ad kabul eder.

    `allow_video=True` verilirse /watch?v=… veya /shorts/… bağlantısından video kimliğini
    ayrıştırır (ancak bağlantının /shorts/ biçiminde olması ASLA tür kanıtı olarak saklanmaz).
    """
    s = (text or "").strip()
    if not s:
        raise InvalidChannelInput("Kanal adresi boş.")
    if CHANNEL_ID_RE.match(s):
        return ChannelRef("channel_id", s)
    if s.startswith("@"):
        handle = unquote(s.split("/")[0].split("?")[0])
        if _HANDLE_RE.match(handle):
            return ChannelRef("handle", handle)
        raise InvalidChannelInput("Geçersiz @kullanıcı adı.")

    if "://" not in s:
        s = "https://" + s
    u = urlparse(s)
    host = (u.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise InvalidChannelInput("Bu bir YouTube adresi değil.")
    path = unquote(u.path or "/")
    parts = [p for p in path.split("/") if p]
    q = parse_qs(u.query)

    if host == "youtu.be":
        vid = parts[0] if parts else ""
        if allow_video and VIDEO_ID_RE.match(vid):
            return ChannelRef("video", vid)
        raise InvalidChannelInput(
            "Bu bir video bağlantısı. Lütfen kanalın adresini (@ad veya /channel/UC…) girin."
        )

    # RSS: /feeds/videos.xml?channel_id=UC…  veya ?playlist_id=UU…
    if parts[:2] == ["feeds", "videos.xml"]:
        cid = (q.get("channel_id") or [""])[0]
        if CHANNEL_ID_RE.match(cid):
            return ChannelRef("channel_id", cid)
        pid = (q.get("playlist_id") or [""])[0]
        m = re.match(r"^UU(?:LF|SH|LV|MF|MO|MS|PS|PV)?([0-9A-Za-z_-]{22})$", pid)
        if m:
            return ChannelRef("channel_id", "UC" + m.group(1))
        if (q.get("user") or [""])[0]:
            return ChannelRef("legacy", "user/" + q["user"][0])
        raise InvalidChannelInput("RSS bağlantısında kanal kimliği bulunamadı.")

    if not parts:
        raise InvalidChannelInput("Adreste kanal bilgisi yok.")
    head = parts[0]
    if head == "channel" and len(parts) > 1:
        if CHANNEL_ID_RE.match(parts[1]):
            return ChannelRef("channel_id", parts[1])
        raise InvalidChannelInput("Geçersiz kanal kimliği (UC… biçiminde olmalı).")
    if head.startswith("@"):
        if _HANDLE_RE.match(head):
            return ChannelRef("handle", head)
        raise InvalidChannelInput("Geçersiz @kullanıcı adı.")
    if head in ("c", "user") and len(parts) > 1:
        return ChannelRef("legacy", f"{head}/{parts[1]}")
    if head in ("watch", "shorts", "live", "embed") or "v" in q:
        if allow_video:
            vid = (q.get("v") or [parts[1] if len(parts) > 1 else ""])[0]
            if VIDEO_ID_RE.match(vid):
                return ChannelRef("video", vid)
        raise InvalidChannelInput(
            "Bu bir video bağlantısı. Lütfen kanalın adresini (@ad veya /channel/UC…) girin.")
    raise InvalidChannelInput("Desteklenmeyen kanal adresi biçimi.")


def channel_feed_url(channel_id: str) -> str:
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"


# YouTube'un kanal yükleme listesinin türlere ayrılmış hâlleri (anahtarsız RSS ile okunabilir).
TYPED_PLAYLIST_PREFIX = {"videos": "UULF", "shorts": "UUSH", "streams": "UULV"}


def typed_feed_url(channel_id: str, kind: str) -> str:
    prefix = TYPED_PLAYLIST_PREFIX[kind]
    return f"https://www.youtube.com/feeds/videos.xml?playlist_id={prefix}{channel_id[2:]}"


def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def shorts_url(video_id: str) -> str:
    return f"https://www.youtube.com/shorts/{video_id}"


def tab_url(channel_id: str, tab: str) -> str:
    return f"https://www.youtube.com/channel/{channel_id}/{tab}"

