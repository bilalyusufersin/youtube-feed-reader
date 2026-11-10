"""YouTube veri kaynağı arayüzü ve gerçek (ağ) uygulaması.

Senkronizasyon katmanı yalnızca bu arayüzü kullanır; testlerde kaydedilmiş
yanıtlardan beslenen sahte bir kaynak verilir.
"""
from __future__ import annotations

import logging
import threading
from typing import Protocol

from ..models import FeedResult, ResolvedChannel, TabEntry, VideoMetadata
from . import ytdlp_source
from .feeds import FeedParseError, parse_feed
from .http import Http, NetError
from .urls import ChannelRef, channel_feed_url, shorts_url, typed_feed_url

log = logging.getLogger(__name__)


class YouTubeSource(Protocol):
    def fetch_feed(self, channel_id: str, kind: str | None = None,
                   cancel: threading.Event | None = None) -> FeedResult: ...

    def list_tab(self, channel_id: str, tab: str, limit: int = 15,
                 cancel: threading.Event | None = None) -> list[TabEntry]: ...

    def video_metadata(self, video_id: str,
                       cancel: threading.Event | None = None) -> VideoMetadata: ...

    def shorts_probe(self, video_id: str,
                     cancel: threading.Event | None = None) -> bool | None: ...

    def resolve(self, ref: ChannelRef) -> ResolvedChannel: ...


class LiveSource:
    """Gerçek YouTube erişimi: RSS için httpx, sekme/video bilgisi için yt-dlp."""

    def __init__(self, http: Http | None = None) -> None:
        self.http = http or Http()

    def close(self) -> None:
        self.http.close()

    def fetch_feed(self, channel_id, kind=None, cancel=None) -> FeedResult:
        url = channel_feed_url(channel_id) if kind is None else typed_feed_url(channel_id, kind)
        resp = self.http.get(url, cancel=cancel)
        try:
            return parse_feed(resp.content, expected_channel_id=channel_id)
        except FeedParseError as e:
            raise NetError(str(e), "http") from e

    def list_tab(self, channel_id, tab, limit=15, cancel=None) -> list[TabEntry]:
        return ytdlp_source.list_tab(channel_id, tab, limit=limit, cancel=cancel)

    def video_metadata(self, video_id, cancel=None) -> VideoMetadata:
        return ytdlp_source.video_metadata(video_id, cancel=cancel)

    def shorts_probe(self, video_id, cancel=None) -> bool | None:
        """/shorts/ID adresine yönlendirme izlemeden HEAD isteği.

        200 → YouTube içeriği Shorts oynatıcısında sunuyor; 30x → /watch'a yönlendiriyor.
        Başka her durum (onay sayfası vb.) → None (kanıt yok).
        """
        try:
            resp = self.http.request("HEAD", shorts_url(video_id), cancel=cancel,
                                     follow_redirects=False,
                                     ok_statuses=(200, 301, 302, 303, 307, 308))
        except NetError as e:
            if e.kind == "rate_limited":
                raise
            return None
        if resp.status_code == 200:
            return True
        loc = resp.headers.get("location", "")
        if "/watch" in loc and video_id in loc:
            return False
        return None

    def resolve(self, ref: ChannelRef) -> ResolvedChannel:
        if ref.kind == "channel_id":
            try:
                feed = self.fetch_feed(ref.value)
            except NetError as e:
                if e.kind == "not_found":
                    raise NetError("Bu kimliğe sahip bir kanal bulunamadı.", "not_found") from e
                raise
            res = ResolvedChannel(channel_id=ref.value, title=feed.title)
            try:  # avatar ve @ad isteğe bağlı; başarısızlık eklemeyi engellemez
                extra = ytdlp_source.resolve_channel_page(ref.page_url())
                res.handle, res.avatar_url = extra.handle, extra.avatar_url
                res.title = res.title or extra.title
            except NetError as e:
                log.info("Kanal ek bilgisi alınamadı: %s", e)
            return res
        return ytdlp_source.resolve_channel_page(ref.page_url())

