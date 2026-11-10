"""Kanal eşitleme, sınırlı sekme keşfi ve akıllı üstveri yenileme motoru.

Akış:
1. Kanalın ana Atom/RSS akışı (`?channel_id=UC…`) çekilir.
2. YouTube'un türe ayrılmış yükleme akışları (`UULF` normal video, `UUSH` Shorts,
   `UULV` canlı yayın) çekilir. Bunlar hafif XML akışlarıdır; hem son ~15 öğeyi
   anında sınıflandırır hem de yoğun Shorts/yayın günlerinde ana akıştan düşen
   son içerikleri yakalar.
3. Gerekli olduğunda (yeni kanal, canlı yayın varlığı veya süre dolumu) kanalın
   `/streams` sekmesi sınırlı pencereyle (`limit=10`) sorgulanarak aktif/planlı
   yayınlar ve yayın durumları (`planlandı` / `canlı` / `tamamlandı`) doğrulanır.
4. Yalnızca türü henüz belirlenememiş veya aktif/planlı yayında olan videolar için
   bütçe dahilinde tekil `yt-dlp` üstverisi (`video_metadata`) ve gerekirse
   `/shorts/` yönlendirme kontrolü yapılır. Daha önce sınıflandırılmış normal
   videolar, Shorts ve tamamlanmış yayınlar tekrar sorgulanmaz.
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .classifier import CLASSIFIER_VERSION, classify, compute_next_meta_check
from .db import Database
from .models import ContentType, LiveState, ResolvedChannel
from .youtube.http import Cancelled, NetError
from .youtube.source import YouTubeSource
from .youtube.urls import parse_channel_input

log = logging.getLogger(__name__)


@dataclass(slots=True)
class NotificationEvent:
    video_id: str
    channel_id: str
    channel_title: str
    title: str
    kind: str          # "new_content" | "live_started"
    content_type: str  # "video" | "short" | "live"


@dataclass(slots=True)
class ChannelSyncResult:
    channel_id: str
    ok: bool
    new_count: int = 0
    updated_count: int = 0
    meta_queried: int = 0
    error: str | None = None
    warning: str | None = None
    notifications: list[NotificationEvent] = field(default_factory=list)


@dataclass(slots=True)
class SyncSummary:
    results: list[ChannelSyncResult] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total_new(self) -> int:
        return sum(r.new_count for r in self.results)

    @property
    def error_count(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    @property
    def all_notifications(self) -> list[NotificationEvent]:
        out: list[NotificationEvent] = []
        for r in self.results:
            if r.notifications:
                out.extend(r.notifications)
        return out


def add_channel(db: Database, source: YouTubeSource, user_input: str,
                group_id: int | None = None) -> tuple[ResolvedChannel, bool]:
    """Kullanıcı girdisini kalıcı `channel_id` değerine çözer ve veritabanına ekler.

    Dönüş: `(ResolvedChannel, yeni_eklendi_mi)`
    Aynı kanal farklı bir adresle (@ad, /channel/UC…, RSS) tekrar eklenmek istenirse
    `yeni_eklendi_mi = False` döner ve mevcut kayıt korunur.
    """
    ref = parse_channel_input(user_input)
    if ref.is_resolved:
        existing = db.channel(ref.value)
        if existing is not None:
            return ResolvedChannel(
                channel_id=existing["channel_id"],
                title=existing["title"],
                handle=existing["handle"],
                avatar_url=existing["avatar_url"],
            ), False
    resolved = source.resolve(ref)
    inserted = db.insert_channel(
        resolved.channel_id,
        resolved.title or resolved.channel_id,
        resolved.handle,
        resolved.avatar_url,
        group_id=group_id,
    )
    if not inserted:
        db.update_channel_info(resolved.channel_id, resolved.title, resolved.handle,
                               resolved.avatar_url)
    return resolved, inserted


def reclassify_video(db: Database, video_id: str, now: int | None = None,
                     *, attempts: int = 0, meta_error_kind: str | None = None,
                     keep_schedule: bool = False):
    ts = int(now if now is not None else time.time())
    ev = db.evidence(video_id)
    row = db.video(video_id)
    date_est = bool(row["date_estimated"]) if row else False
    cls = classify(ev, now=ts)
    next_check = None if keep_schedule else compute_next_meta_check(
        cls, ts, attempts=attempts, meta_error_kind=meta_error_kind,
        has_meta=("ytdlp_meta" in ev), date_estimated=date_est,
    )
    db.save_classification(video_id, cls, next_check, keep_schedule=keep_schedule)
    return cls


def ensure_classifier_version(db: Database) -> int:
    """Sınıflandırıcı sürümü değiştiyse mevcut kanıtlarla tüm videoları yeniden değerlendirir."""
    vids = db.videos_needing_reclassify(CLASSIFIER_VERSION)
    now = int(time.time())
    for vid in vids:
        reclassify_video(db, vid, now=now, keep_schedule=True)
    return len(vids)


def _ingest_entries(db: Database, channel_id: str, entries, signal: str | None,
                    now: int, touched: set[str],
                    already_upserted: set[str] | None = None) -> tuple[int, int]:
    new_n = upd_n = 0
    for e in entries:
        if already_upserted is None or e.video_id not in already_upserted:
            is_new = db.upsert_video(
                video_id=e.video_id,
                channel_id=channel_id,
                title=e.title,
                description=e.description,
                published_at=e.published,
                updated_at=e.updated,
                thumb_url=e.thumb_url,
                views=e.views,
                seen_in_feed=True,
            )
            if is_new:
                new_n += 1
            else:
                upd_n += 1
            if already_upserted is not None:
                already_upserted.add(e.video_id)
        if signal:
            db.add_evidence(e.video_id, signal, "1", now)
        touched.add(e.video_id)
    return new_n, upd_n


def fetch_single_video_metadata(db: Database, source: YouTubeSource, video_id: str,
                                *, probe_shorts_on_fail: bool = True,
                                cancel: threading.Event | None = None):
    """Tek bir video için yt-dlp üstverisini (ve gerekirse /shorts/ yönlendirmesini)
    çekip sınıflandırmayı günceller."""
    now = int(time.time())
    row = db.video(video_id)
    attempts = int(row["meta_attempts"]) if row else 0
    meta = source.video_metadata(video_id, cancel=cancel)
    db.add_evidence(video_id, "ytdlp_meta", meta.to_json(), now)
    if meta.ok:
        pub = meta.release_timestamp or meta.timestamp
        db.upsert_video(
            video_id=video_id,
            channel_id=row["channel_id"] if row else (meta.channel_id or ""),
            title=meta.title,
            description=meta.description,
            published_at=pub,
            thumb_url=meta.thumb_url,
            duration=meta.duration,
            seen_in_feed=False,
        )
        db.mark_duration_checked(video_id, meta.duration, now)
    elif probe_shorts_on_fail:
        sp = source.shorts_probe(video_id, cancel=cancel)
        if sp is True:
            db.add_evidence(video_id, "shorts_head", "200", now)
        elif sp is False:
            db.add_evidence(video_id, "shorts_head", "303_watch", now)
    db.record_meta_attempt(
        video_id, ok=meta.ok,
        availability=meta.availability if meta.ok else meta.error_kind,
    )
    return reclassify_video(
        db, video_id, now=now,
        attempts=0 if meta.ok else attempts + 1,
        meta_error_kind=None if meta.ok else meta.error_kind,
    )


def backfill_missing_durations(
    db: Database,
    source: YouTubeSource,
    video_ids: list[str] | None = None,
    *,
    limit: int = 5,
    cancel: threading.Event | None = None,
) -> list[str]:
    """Süresi bilinmeyen (duration IS NULL) videoların süresini arka planda tamamlar.

    Önbellekte süresi bulunan veya daha önce kontrol edilmiş olan videoları tekrar sorgulamaz.
    """
    rows = db.videos_missing_duration(video_ids, limit=limit)
    if not rows:
        return []
    updated: list[str] = []
    now = int(time.time())
    for r in rows:
        if cancel is not None and cancel.is_set():
            break
        vid = r["video_id"]
        try:
            meta = source.video_metadata(vid, cancel=cancel)
        except Cancelled:
            break
        except Exception:  # noqa: BLE001
            db.mark_duration_checked(vid, None, now)
            continue
        if meta.ok:
            db.mark_duration_checked(vid, meta.duration, now)
            if meta.duration and meta.duration > 0:
                updated.append(vid)
        else:
            db.mark_duration_checked(vid, None, now)
    return updated


def _evaluate_channel_notifications(
    db: Database,
    channel_id: str,
    is_initial_sync: bool,
    candidate_vids: set[str],
    prev_scheduled_ids: set[str],
) -> list[NotificationEvent]:
    """Kanal eşitlemesi sonrası bildirim olaylarını üretir.

    Kurallar:
    - İlk kanal eklemede / toplu aktarımda (`is_initial_sync=True`) geçmiş içerikler
      için bildirim yağmuru oluşturulmaz; hepsi `notified_new=1` olarak işaretlenir.
    - Aynı içerik için her yenilemede yeniden bildirim gönderilmez (`notified_new=1`).
    - Doğrulanan bir yayın `scheduled` durumundan `live` durumuna geçtiğinde
      isteğe bağlı bildirim üretilir (`notified_live_start=1`).
    """
    ch = db.channel(channel_id)
    if ch is None:
        return []

    if is_initial_sync:
        all_vids = list(db.channel_video_ids(channel_id))
        if all_vids:
            db.mark_notified_new(all_vids)
            live_done = [
                r["video_id"]
                for r in db.q(
                    "SELECT video_id FROM videos WHERE channel_id=? AND live_state IN ('live', 'completed')",
                    (channel_id,),
                )
            ]
            if live_done:
                db.mark_notified_live_start(live_done)
        db.mark_channel_initial_sync_done(channel_id)
        return []

    events: list[NotificationEvent] = []
    if not candidate_vids:
        return events

    ch_title = ch["title"] or channel_id
    notify_global = bool(db.get_setting("notifications_enabled"))
    notify_ch_allowed = ch["notify_enabled"] is None or int(ch["notify_enabled"]) == 1
    notify_live_trans = bool(db.get_setting("notify_live_transition"))

    new_marked: list[str] = []
    live_marked: list[str] = []

    for vid in sorted(candidate_vids):
        v = db.video(vid)
        if v is None:
            continue
        eff_t = v["user_type"] or v["auto_type"] or "unknown"
        live_st = v["live_state"]

        # 1. Planlandı -> Canlı geçişi
        if (
            vid in prev_scheduled_ids
            and eff_t == "live"
            and live_st == "live"
            and not int(v["notified_live_start"] or 0)
        ):
            live_marked.append(vid)
            new_marked.append(vid)
            if notify_global and notify_ch_allowed and notify_live_trans:
                events.append(
                    NotificationEvent(
                        video_id=vid,
                        channel_id=channel_id,
                        channel_title=ch_title,
                        title=v["title"] or vid,
                        kind="live_started",
                        content_type="live",
                    )
                )
            continue

        # 2. Yeni içerik bildirimi (türü kesinleşmiş olanlar)
        if not int(v["notified_new"] or 0) and eff_t != "unknown":
            new_marked.append(vid)
            if live_st in ("live", "completed"):
                live_marked.append(vid)
            if db.should_notify_channel_type(ch, eff_t):
                events.append(
                    NotificationEvent(
                        video_id=vid,
                        channel_id=channel_id,
                        channel_title=ch_title,
                        title=v["title"] or vid,
                        kind="new_content",
                        content_type=eff_t,
                    )
                )

    if new_marked:
        db.mark_notified_new(new_marked)
    if live_marked:
        db.mark_notified_live_start(live_marked)
    return events


def sync_channel(
    db: Database,
    source: YouTubeSource,
    channel_id: str,
    *,
    cancel: threading.Event | None = None,
    meta_budget: int = 6,
    force_tabs: bool = False,
) -> ChannelSyncResult:
    """Tek bir kanalı eşitler. Hatalar yakalanıp `ChannelSyncResult` içinde döndürülür."""
    if cancel is not None and cancel.is_set():
        raise Cancelled()

    now = int(time.time())
    ch = db.channel(channel_id)
    if ch is None:
        return ChannelSyncResult(channel_id=channel_id, ok=False, error="Kanal kaydı bulunamadı.")

    is_initial_sync = not bool(ch["initial_sync_done"]) and (ch["last_success_at"] is None)
    prev_scheduled_ids = {
        r["video_id"]
        for r in db.q(
            "SELECT video_id FROM videos WHERE channel_id=? AND live_state='scheduled'",
            (channel_id,),
        )
    }

    # 1. Ana kanal RSS akışı
    try:
        main_feed = source.fetch_feed(channel_id, kind=None, cancel=cancel)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        err_msg = str(e)
        db.set_channel_status(channel_id, ok=False, error=err_msg)
        return ChannelSyncResult(channel_id=channel_id, ok=False, error=err_msg)

    if main_feed.title and (not ch["title"] or ch["title"] == channel_id):
        db.update_channel_info(channel_id, title=main_feed.title)

    touched: set[str] = set()
    upserted_in_run: set[str] = set()
    warnings: list[str] = []
    new_total, upd_total = _ingest_entries(
        db, channel_id, main_feed.entries, None, now, touched, upserted_in_run
    )

    # 2. Türe ayrılmış yükleme listesi RSS akışları (UULF, UUSH, UULV)
    typed_map = [
        ("videos", "feed_uulf"),
        ("shorts", "feed_uush"),
        ("streams", "feed_uulv"),
    ]
    uulv_ids: set[str] = set()
    typed_failed = 0
    for kind, sig in typed_map:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        try:
            tf = source.fetch_feed(channel_id, kind=kind, cancel=cancel)
        except Cancelled:
            raise
        except NetError as e:
            if e.kind == "not_found":
                # Kanalda bu kategoride hiç içerik yoksa YouTube 404 döndürür (normaldir).
                continue
            typed_failed += 1
            log.info("Tür akışı alınamadı (%s/%s): %s", channel_id, kind, e)
            continue
        except Exception as e:  # noqa: BLE001
            typed_failed += 1
            log.info("Tür akışı hatası (%s/%s): %s", channel_id, kind, e)
            continue

        n_new, n_upd = _ingest_entries(
            db, channel_id, tf.entries, sig, now, touched, upserted_in_run
        )
        new_total += n_new
        upd_total += n_upd
        if kind == "streams":
            uulv_ids.update(e.video_id for e in tf.entries)

    # 3. Sınırlı /streams sekmesi sorgusu (canlı durumları ve 24/7 yayın keşfi için)
    live_tab_min = int(db.get_setting("live_tab_minutes") or 60)
    last_streams_tab = ch["tab_checked_streams"] or 0
    has_active_or_pending_live = db.q1(
        "SELECT 1 FROM videos WHERE channel_id=? AND "
        "(live_state IN ('live', 'scheduled') OR (auto_type='live' AND live_state IS NULL))",
        (channel_id,),
    ) is not None
    need_streams_tab = (
        force_tabs
        or bool(uulv_ids)
        or has_active_or_pending_live
        or (now - last_streams_tab >= live_tab_min * 60)
    )
    if need_streams_tab:
        try:
            tab_entries = source.list_tab(channel_id, "streams", limit=20, cancel=cancel)
            db.mark_tab_checked(channel_id, "streams", now)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Canlı yayın sekmesi okunamadı: {e}")
            tab_entries = []

        existing_ids = db.channel_video_ids(channel_id)
        for te in tab_entries:
            if te.video_id in existing_ids:
                db.upsert_video(
                    video_id=te.video_id, channel_id=channel_id,
                    title=te.title, description=None, published_at=None,
                    duration=te.duration, seen_in_feed=False,
                )
                db.add_evidence(
                    te.video_id, "tab_streams",
                    {"live_status": te.live_status, "duration": te.duration}, now,
                )
                touched.add(te.video_id)
            elif te.live_status in ("is_live", "is_upcoming"):
                # RSS penceresinde olmayan aktif (ör. 7/24) veya yeni planlanmış canlı yayın
                is_new = db.upsert_video(
                    video_id=te.video_id, channel_id=channel_id,
                    title=te.title, description="", published_at=now,
                    duration=te.duration, seen_in_feed=False, date_estimated=True,
                )
                if is_new:
                    new_total += 1
                db.add_evidence(
                    te.video_id, "tab_streams",
                    {"live_status": te.live_status, "duration": te.duration}, now,
                )
                touched.add(te.video_id)

    # 4. Dokunulan videoları mevcut kanıtlarla sınıflandır
    still_unknown: list[str] = []
    for vid in touched:
        row = db.video(vid)
        attempts = int(row["meta_attempts"]) if row else 0
        cls = reclassify_video(db, vid, now=now, attempts=attempts)
        if cls.type == ContentType.UNKNOWN:
            still_unknown.append(vid)

    # 5. Eğer tür akışları erişilemediyse ve belirsiz videolar kaldıysa sınırlı /videos ve /shorts sekmesi dene
    if still_unknown and typed_failed > 0:
        for tab_name, sig_name in (("videos", "tab_videos"), ("shorts", "tab_shorts")):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                t_entries = source.list_tab(channel_id, tab_name, limit=15, cancel=cancel)
                db.mark_tab_checked(channel_id, tab_name, now)
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                warnings.append(f"/{tab_name} sekmesi okunamadı: {e}")
                continue
            existing_ids = db.channel_video_ids(channel_id)
            for te in t_entries:
                if te.video_id in existing_ids:
                    db.upsert_video(
                        video_id=te.video_id, channel_id=channel_id,
                        title=te.title, description=None, published_at=None,
                        duration=te.duration, seen_in_feed=False,
                    )
                    db.add_evidence(
                        te.video_id, sig_name,
                        {"live_status": te.live_status, "duration": te.duration, "url": te.url},
                        now,
                    )
                    reclassify_video(db, te.video_id, now=now)

    # 6. Bu kanal için vadesi gelmiş tekil üstveri kontrollerini (bütçe dahilinde) çalıştır
    due_rows = [
        r for r in db.due_meta_checks(now, limit=meta_budget * 3)
        if r["channel_id"] == channel_id
    ][:meta_budget]
    meta_queried = 0
    for r in due_rows:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        try:
            fetch_single_video_metadata(db, source, r["video_id"], cancel=cancel)
            touched.add(r["video_id"])
            meta_queried += 1
        except Cancelled:
            raise
        except NetError as e:
            warnings.append(str(e))
            if e.kind == "rate_limited":
                break
        except Exception as e:  # noqa: BLE001
            log.info("Üstveri hatası (%s): %s", r["video_id"], e)

    notifications = _evaluate_channel_notifications(
        db,
        channel_id,
        is_initial_sync=is_initial_sync,
        candidate_vids=touched,
        prev_scheduled_ids=prev_scheduled_ids,
    )

    warn_str = "; ".join(warnings) if warnings else None
    db.set_channel_status(channel_id, ok=True, warning=warn_str)
    return ChannelSyncResult(
        channel_id=channel_id,
        ok=True,
        new_count=new_total,
        updated_count=upd_total,
        meta_queried=meta_queried,
        warning=warn_str,
        notifications=notifications,
    )


def sync_channels(
    db: Database,
    source: YouTubeSource,
    channel_ids: list[str] | None = None,
    *,
    cancel: threading.Event | None = None,
    max_workers: int = 3,
    meta_budget_per_channel: int = 5,
    on_channel_done=None,
) -> SyncSummary:
    """Birden fazla kanalı sınırlı eşzamanlılıkla eşitler.

    Tek bir kanalın hatası diğer kanalların güncellenmesini durdurmaz.
    """
    ensure_classifier_version(db)
    if channel_ids is None:
        channel_ids = [r["channel_id"] for r in db.channels()]
    summary = SyncSummary()
    if not channel_ids:
        return summary

    def _worker(cid: str) -> ChannelSyncResult:
        try:
            return sync_channel(
                db, source, cid, cancel=cancel,
                meta_budget=meta_budget_per_channel,
                force_tabs=(len(channel_ids) == 1),
            )
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Kanal eşitleme beklenmeyen hata (%s)", cid)
            db.set_channel_status(cid, ok=False, error=str(e))
            return ChannelSyncResult(channel_id=cid, ok=False, error=str(e))
        finally:
            db.close_thread_connection()

    workers = max(1, min(max_workers, len(channel_ids)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ytrss-sync") as ex:
        fut_map = {ex.submit(_worker, cid): cid for cid in channel_ids}
        for fut in as_completed(fut_map):
            cid = fut_map[fut]
            try:
                res = fut.result()
            except Cancelled:
                summary.cancelled = True
                break
            summary.results.append(res)
            if on_channel_done is not None:
                try:
                    on_channel_done(res)
                except Exception:  # noqa: BLE001
                    log.exception("on_channel_done callback hatası (%s)", cid)
    return summary
