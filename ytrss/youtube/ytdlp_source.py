"""yt-dlp ile anahtarsız ve indirmesiz bilgi okuma.

- Kanal sekmesi hızlı listelemesi (extract_flat): tek istekte ilk sayfa (~30 kayıt).
  Bu listelemede süre/tarih gibi alanlar eksik olabilir.
- Tek video bilgisi (process=False, format işleme yok, indirme yok).
- @ad / eski /c/ /user/ adreslerini kalıcı UC… kimliğine çözme.

Bu modül yt-dlp'nin dahili bilgi sözlüğünü kendi veri tiplerimize çevirir; YouTube
değişikliklerinde çoğu zaman sadece yt-dlp'yi güncellemek yeterlidir.
"""
from __future__ import annotations

import logging
import threading

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from ..models import ResolvedChannel, TabEntry, VideoMetadata
from .http import RATE_LIMITER, Cancelled, NetError
from .urls import CHANNEL_ID_RE, VIDEO_ID_RE, tab_url, watch_url

log = logging.getLogger(__name__)

YTDLP_VERSION = yt_dlp.version.__version__


class _Logger:
    """yt-dlp çıktısını uygulama günlüğüne yönlendirir; JS çalışma zamanı uyarısını bastırır."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        if "JavaScript runtime" in msg:
            return  # format çözmüyoruz; yalnızca metadata okuyoruz
        self.warnings.append(msg)
        log.debug("yt-dlp uyarı: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp hata: %s", msg)


def _base_opts(timeout: int) -> dict:
    return {
        "quiet": True,
        "no_warnings": False,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": timeout,
        "extractor_retries": 1,
        "ignore_no_formats_error": True,
        "check_formats": False,
        "getcomments": False,
        "writesubtitles": False,
        "noprogress": True,
        "logger": _Logger(),
        "extractor_args": {"youtube": {"skip": ["dash", "hls", "translated_subs"]}},
    }


def classify_error(message: str) -> str:
    m = message.lower()
    if "429" in m or "too many requests" in m:
        return "rate_limited"
    if "confirm you" in m and "bot" in m:
        return "bot_check"
    if "private video" in m:
        return "private"
    if "members-only" in m or "join this channel" in m or "members only" in m:
        return "members_only"
    if "confirm your age" in m or "age-restricted" in m or "inappropriate for some users" in m:
        return "age_restricted"
    if ("video unavailable" in m or "has been removed" in m or "no longer available" in m
            or "account associated" in m or "does not exist" in m or "is not available" in m
            or "copyright" in m):
        return "unavailable"
    if ("timed out" in m or "timeout" in m or "getaddrinfo" in m or "connection" in m
            or "network is unreachable" in m or "name or service not known" in m
            or "temporary failure in name resolution" in m or "unable to download" in m):
        return "network"
    return "other"


ERROR_KIND_LABELS = {
    "rate_limited": "YouTube hız sınırı uyguladı (429).",
    "bot_check": "YouTube bot doğrulaması istedi; anahtarsız erişim geçici olarak engellendi.",
    "private": "Video gizli.",
    "members_only": "Yalnızca kanal üyelerine açık.",
    "age_restricted": "Yaş kısıtlı; oturum açmadan bilgi alınamıyor.",
    "unavailable": "Video kullanılamıyor (kaldırılmış olabilir).",
    "network": "Ağ hatası.",
    "other": "Bilinmeyen hata.",
}


def _check_rate(kind: str) -> None:
    if kind in ("rate_limited", "bot_check"):
        RATE_LIMITER.penalize(300 if kind == "bot_check" else 90)


def _int(v) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def list_tab(channel_id: str, tab: str, limit: int = 15, timeout: int = 15,
             cancel: threading.Event | None = None) -> list[TabEntry]:
    """Kanalın /videos, /shorts veya /streams sekmesinin ilk `limit` kaydı.

    Sınırlı bir pencere döner: burada görünmemek o türe ait olmamak demek DEĞİLDİR.
    """
    assert tab in ("videos", "shorts", "streams")
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    RATE_LIMITER.wait(cancel)
    opts = _base_opts(timeout)
    opts.update({"extract_flat": "in_playlist", "playlistend": limit, "lazy_playlist": True,
                 "noplaylist": False})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(tab_url(channel_id, tab), download=False, process=False)
            out: list[TabEntry] = []
            for i, e in enumerate(info.get("entries") or []):
                if i >= limit:
                    break
                vid = e.get("id")
                if not vid or not VIDEO_ID_RE.match(vid):
                    continue
                out.append(TabEntry(
                    video_id=vid, title=e.get("title"), url=e.get("url"),
                    duration=_int(e.get("duration")), live_status=e.get("live_status"),
                    position=i,
                ))
            return out
    except (DownloadError, ExtractorError) as e:
        msg = str(e)
        if "does not have a" in msg and "tab" in msg:
            return []  # kanalda bu sekme yok (ör. hiç canlı yayın yapılmamış)
        kind = classify_error(msg)
        _check_rate(kind)
        raise NetError(f"Sekme okunamadı ({tab}): {ERROR_KIND_LABELS.get(kind, msg)}",
                       "rate_limited" if kind in ("rate_limited", "bot_check") else "network")


def video_metadata(video_id: str, timeout: int = 20,
                   cancel: threading.Event | None = None) -> VideoMetadata:
    """Tek videonun bilgisini indirmeden okur. Hata durumunda ok=False döner (istisna atmaz),
    ancak hız sınırı/iptal durumlarında istisna atar."""
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    RATE_LIMITER.wait(cancel)
    opts = _base_opts(timeout)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(watch_url(video_id), download=False, process=False)
    except (DownloadError, ExtractorError) as e:
        msg = str(e).replace("ERROR: ", "")
        kind = classify_error(msg)
        _check_rate(kind)
        if kind in ("rate_limited", "bot_check"):
            raise NetError(ERROR_KIND_LABELS[kind], "rate_limited", 429)
        return VideoMetadata(video_id=video_id, ok=False, error=msg[:500], error_kind=kind)
    thumb = info.get("thumbnail")
    if (not info.get("channel_id") and info.get("live_status") is None
            and info.get("availability") is None and not (info.get("title") or "")):
        return VideoMetadata(
            video_id=video_id, ok=False,
            error="Video kullanılamıyor veya üstveri boş döndü.",
            error_kind="unavailable",
        )
    return VideoMetadata(
        video_id=video_id, ok=True,
        title=info.get("title"), description=info.get("description"),
        channel_id=info.get("channel_id"),
        duration=_int(info.get("duration")),
        live_status=info.get("live_status"),
        media_type=info.get("media_type"),
        release_timestamp=_int(info.get("release_timestamp")),
        timestamp=_int(info.get("timestamp")),
        availability=info.get("availability"),
        thumb_url=thumb,
    )


def resolve_channel_page(page_url: str, timeout: int = 20) -> ResolvedChannel:
    """@ad veya /c/, /user/ adresini kanal kimliğine çözer (yt-dlp, ilk sayfa)."""
    RATE_LIMITER.wait(None)
    opts = _base_opts(timeout)
    opts.update({"extract_flat": "in_playlist", "playlistend": 1, "lazy_playlist": True,
                 "noplaylist": False})
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(page_url, download=False, process=False)
    except (DownloadError, ExtractorError) as e:
        msg = str(e)
        kind = classify_error(msg)
        _check_rate(kind)
        if kind == "network":
            raise NetError("Kanal sayfasına ulaşılamadı (ağ hatası).", "network")
        if "404" in msg or kind == "unavailable" or "does not exist" in msg.lower():
            raise NetError("Kanal bulunamadı.", "not_found", 404)
        raise NetError(f"Kanal çözümlenemedi: {ERROR_KIND_LABELS.get(kind, msg)}", kind)
    cid = info.get("channel_id") or ""
    if not CHANNEL_ID_RE.match(cid):
        raise NetError("Kanal kimliği bulunamadı.", "not_found")
    avatar = None
    for t in info.get("thumbnails") or []:
        if "avatar" in str(t.get("id", "")):
            avatar = t.get("url")
    handle = info.get("uploader_id")
    if handle and not handle.startswith("@"):
        handle = None
    return ResolvedChannel(channel_id=cid, title=info.get("channel") or info.get("uploader"),
                           handle=handle, avatar_url=avatar)

