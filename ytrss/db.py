"""SQLite veri katmanı.

- Her iş parçacığı kendi bağlantısını kullanır (threading.local); WAL kipi okuma
  ve yazmanın birbirini bekletmesini en aza indirir.
- Kanal görünürlük tercihleri NULL = "genel varsayılanı izle", 0/1 = kanal için
  kapalı/açık olarak saklanır.
- Okundu/yıldız durumu ve kullanıcının elle seçtiği tür, akış güncellemelerinde
  hiçbir zaman ezilmez.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 2

DEFAULT_SETTINGS: dict[str, Any] = {
    "show_video": 1,
    "show_short": 0,
    "show_live": 0,
    "show_unknown": 0,             # türü belirlenemeyenler ana listelerde gösterilsin mi
    "auto_refresh_minutes": 30,     # 0 = kapalı
    "refresh_on_start": 1,
    "live_tab_minutes": 60,         # /streams sekmesi kanal başına en sık bu aralıkla sorgulanır
    "thumb_cache_mb": 200,
    "theme": "dark",                # dark | light | system
    "mark_read_on_select": 1,
    "watch_later_hide_watched": 0,  # Sonra İzle görünümünde izlenenleri gizle
    "notifications_enabled": 1,     # Genel masaüstü bildirimleri
    "notify_video": 1,              # Genel varsayılan: normal video bildirimi
    "notify_short": 0,              # Genel varsayılan: Shorts bildirimi
    "notify_live": 0,               # Genel varsayılan: canlı yayın bildirimi
    "notify_live_transition": 1,    # Planlandı -> Canlı geçiş bildirimi
    "tray_enabled": 1,              # Sistem tepsisi simgesi
    "close_to_tray": 0,             # Pencere kapatılınca tepside çalışmaya devam et (varsayılan kapalı)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    sort_order INTEGER NOT NULL DEFAULT 0,
    expanded INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS channels (
    channel_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    handle TEXT,
    avatar_url TEXT,
    group_id INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    show_video INTEGER,   -- NULL: genel varsayılan
    show_short INTEGER,
    show_live INTEGER,
    notify_enabled INTEGER, -- NULL: genel varsayılan, 1: açık, 0: kapalı
    notify_video INTEGER,   -- NULL: genel varsayılan
    notify_short INTEGER,
    notify_live INTEGER,
    initial_sync_done INTEGER NOT NULL DEFAULT 0,
    added_at INTEGER NOT NULL,
    last_refresh_at INTEGER,
    last_success_at INTEGER,
    last_error TEXT,
    last_error_at INTEGER,
    last_warning TEXT,
    tab_checked_videos INTEGER,
    tab_checked_shorts INTEGER,
    tab_checked_streams INTEGER
);
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(channel_id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    published_at INTEGER,
    date_estimated INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER,
    thumb_url TEXT,
    duration INTEGER,
    duration_checked_at INTEGER,
    views INTEGER,
    auto_type TEXT NOT NULL DEFAULT 'unknown',
    user_type TEXT,
    live_state TEXT,
    premiere_state TEXT,
    scheduled_at INTEGER,
    class_source TEXT,
    class_reason TEXT,
    class_confidence TEXT,
    classified_at INTEGER,
    classifier_version INTEGER,
    availability TEXT,
    meta_checked_at INTEGER,
    meta_attempts INTEGER NOT NULL DEFAULT 0,
    next_meta_check_at INTEGER,
    is_read INTEGER NOT NULL DEFAULT 0,
    is_starred INTEGER NOT NULL DEFAULT 0,
    is_watched INTEGER NOT NULL DEFAULT 0,
    watch_later INTEGER NOT NULL DEFAULT 0,
    watch_later_order INTEGER,
    watch_later_added_at INTEGER,
    notified_new INTEGER NOT NULL DEFAULT 0,
    notified_live_start INTEGER NOT NULL DEFAULT 0,
    first_seen_at INTEGER NOT NULL,
    last_seen_in_feed_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_videos_channel_pub ON videos(channel_id, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_videos_pub ON videos(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_videos_next_meta ON videos(next_meta_check_at)
    WHERE next_meta_check_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS evidence (
    video_id TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
    signal TEXT NOT NULL,
    value TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    PRIMARY KEY (video_id, signal)
);
"""

_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})


def tr_fold(s: str | None) -> str:
    """Türkçe uyumlu büyük/küçük harf duyarsız karşılaştırma için katlama."""
    if not s:
        return ""
    return s.translate(_TR_LOWER).casefold().replace("ı", "i")


@dataclass(slots=True)
class Visibility:
    video: int
    short: int
    live: int
    unknown: int


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        with self._write_lock:
            self.conn().executescript(SCHEMA)
        with self.tx() as con:
            self._migrate(con)

    def _migrate(self, con: sqlite3.Connection) -> None:
        """Mevcut kullanıcı verilerini koruyarak şema geçişlerini uygular."""
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        prev_ver = int(row["value"]) if row and str(row["value"]).isdigit() else 1

        ch_cols = {r["name"] for r in con.execute("PRAGMA table_info(channels)").fetchall()}
        new_ch_cols = [
            ("notify_enabled", "INTEGER"),
            ("notify_video", "INTEGER"),
            ("notify_short", "INTEGER"),
            ("notify_live", "INTEGER"),
            ("initial_sync_done", "INTEGER NOT NULL DEFAULT 0"),
        ]
        added_init_sync = False
        for col_name, col_def in new_ch_cols:
            if col_name not in ch_cols:
                con.execute(f"ALTER TABLE channels ADD COLUMN {col_name} {col_def}")
                if col_name == "initial_sync_done":
                    added_init_sync = True
        if added_init_sync:
            con.execute(
                "UPDATE channels SET initial_sync_done=1 WHERE last_success_at IS NOT NULL"
            )

        v_cols = {r["name"] for r in con.execute("PRAGMA table_info(videos)").fetchall()}
        new_v_cols = [
            ("duration_checked_at", "INTEGER"),
            ("is_watched", "INTEGER NOT NULL DEFAULT 0"),
            ("watch_later", "INTEGER NOT NULL DEFAULT 0"),
            ("watch_later_order", "INTEGER"),
            ("watch_later_added_at", "INTEGER"),
            ("notified_new", "INTEGER NOT NULL DEFAULT 0"),
            ("notified_live_start", "INTEGER NOT NULL DEFAULT 0"),
        ]
        added_notified_new = False
        for col_name, col_def in new_v_cols:
            if col_name not in v_cols:
                con.execute(f"ALTER TABLE videos ADD COLUMN {col_name} {col_def}")
                if col_name == "notified_new":
                    added_notified_new = True
        if added_notified_new:
            con.execute("UPDATE videos SET notified_new=1")
            con.execute(
                "UPDATE videos SET notified_live_start=1 WHERE live_state IN ('live', 'completed')"
            )

        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_videos_watch_later "
            "ON videos(watch_later, watch_later_order)"
        )

        if row is None:
            con.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif prev_ver != SCHEMA_VERSION:
            con.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )

    # --- bağlantı yönetimi -------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=10, isolation_level=None,
                                  check_same_thread=True)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.execute("PRAGMA foreign_keys=ON")
            con.execute("PRAGMA busy_timeout=8000")
            con.create_function("tr_fold", 1, tr_fold, deterministic=True)
            self._local.con = con
        return con

    def close_thread_connection(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Yazma işlemi: süreç içi kilit + BEGIN IMMEDIATE."""
        con = self.conn()
        with self._write_lock:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            else:
                if con.in_transaction:
                    con.execute("COMMIT")

    def q(self, sql: str, params: Iterable | dict = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def q1(self, sql: str, params: Iterable | dict = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    # --- ayarlar -----------------------------------------------------------
    def get_setting(self, key: str) -> Any:
        row = self.q1("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return DEFAULT_SETTINGS.get(key)
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def set_setting(self, key: str, value: Any) -> None:
        with self.tx() as con:
            con.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))

    def visibility_defaults(self) -> Visibility:
        return Visibility(int(self.get_setting("show_video")), int(self.get_setting("show_short")),
                          int(self.get_setting("show_live")), int(self.get_setting("show_unknown")))

    # --- gruplar -----------------------------------------------------------
    def groups(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM groups ORDER BY sort_order, name COLLATE NOCASE")

    def add_group(self, name: str) -> int:
        with self.tx() as con:
            mx = con.execute("SELECT COALESCE(MAX(sort_order), 0) FROM groups").fetchone()[0]
            cur = con.execute("INSERT INTO groups(name, sort_order) VALUES(?, ?)", (name, mx + 1))
            return int(cur.lastrowid)

    def rename_group(self, group_id: int, name: str) -> None:
        with self.tx() as con:
            con.execute("UPDATE groups SET name=? WHERE id=?", (name, group_id))

    def delete_group(self, group_id: int) -> None:
        with self.tx() as con:
            con.execute("UPDATE channels SET group_id=NULL WHERE group_id=?", (group_id,))
            con.execute("DELETE FROM groups WHERE id=?", (group_id,))

    def set_group_expanded(self, group_id: int, expanded: bool) -> None:
        with self.tx() as con:
            con.execute("UPDATE groups SET expanded=? WHERE id=?", (int(expanded), group_id))

    # --- kanallar ----------------------------------------------------------
    def channels(self) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM channels ORDER BY title COLLATE NOCASE")

    def channel(self, channel_id: str) -> sqlite3.Row | None:
        return self.q1("SELECT * FROM channels WHERE channel_id=?", (channel_id,))

    def insert_channel(self, channel_id: str, title: str, handle: str | None,
                       avatar_url: str | None, group_id: int | None = None) -> bool:
        """Kanalı ekler. Zaten varsa False döner (tekrar ekleme engeli: channel_id birincil anahtar)."""
        with self.tx() as con:
            cur = con.execute(
                "INSERT OR IGNORE INTO channels(channel_id, title, handle, avatar_url, group_id,"
                " added_at) VALUES(?, ?, ?, ?, ?, ?)",
                (channel_id, title or channel_id, handle, avatar_url, group_id, int(time.time())))
            return cur.rowcount == 1

    def update_channel_info(self, channel_id: str, title: str | None = None,
                            handle: str | None = None, avatar_url: str | None = None) -> None:
        with self.tx() as con:
            con.execute(
                "UPDATE channels SET title=COALESCE(?, title), handle=COALESCE(?, handle),"
                " avatar_url=COALESCE(?, avatar_url) WHERE channel_id=?",
                (title or None, handle, avatar_url, channel_id))

    def set_channel_prefs(self, channel_id: str, show_video: int | None, show_short: int | None,
                          show_live: int | None) -> None:
        with self.tx() as con:
            con.execute("UPDATE channels SET show_video=?, show_short=?, show_live=?"
                        " WHERE channel_id=?", (show_video, show_short, show_live, channel_id))

    def set_channel_notify_prefs(
        self,
        channel_id: str,
        notify_enabled: int | None,
        notify_video: int | None,
        notify_short: int | None,
        notify_live: int | None,
    ) -> None:
        with self.tx() as con:
            con.execute(
                "UPDATE channels SET notify_enabled=?, notify_video=?, notify_short=?, notify_live=? "
                "WHERE channel_id=?",
                (notify_enabled, notify_video, notify_short, notify_live, channel_id),
            )

    def mark_channel_initial_sync_done(self, channel_id: str) -> None:
        with self.tx() as con:
            con.execute("UPDATE channels SET initial_sync_done=1 WHERE channel_id=?", (channel_id,))

    def should_notify_channel_type(self, channel_row: sqlite3.Row | dict, eff_type: str) -> bool:
        """Kanal ve içerik türü bazında bildirim gönderilip gönderilmeyeceğini hesaplar."""
        if not bool(self.get_setting("notifications_enabled")):
            return False
        ch = dict(channel_row)
        if ch.get("notify_enabled") is not None and int(ch["notify_enabled"]) == 0:
            return False
        if eff_type == "video":
            val = ch.get("notify_video")
            if val is not None:
                return int(val) == 1
            if ch.get("show_video") is not None:
                return int(ch["show_video"]) == 1 and bool(self.get_setting("notify_video"))
            return bool(self.get_setting("notify_video"))
        if eff_type == "short":
            val = ch.get("notify_short")
            if val is not None:
                return int(val) == 1
            if ch.get("show_short") is not None and int(ch["show_short"]) == 0:
                return False
            return bool(self.get_setting("notify_short"))
        if eff_type == "live":
            val = ch.get("notify_live")
            if val is not None:
                return int(val) == 1
            if ch.get("show_live") is not None and int(ch["show_live"]) == 0:
                return False
            return bool(self.get_setting("notify_live"))
        return False

    def set_channel_group(self, channel_id: str, group_id: int | None) -> None:
        with self.tx() as con:
            con.execute("UPDATE channels SET group_id=? WHERE channel_id=?", (group_id, channel_id))

    def rename_channel(self, channel_id: str, title: str) -> None:
        with self.tx() as con:
            con.execute("UPDATE channels SET title=? WHERE channel_id=?", (title, channel_id))

    def delete_channel(self, channel_id: str) -> None:
        with self.tx() as con:
            con.execute("DELETE FROM channels WHERE channel_id=?", (channel_id,))

    def set_channel_status(self, channel_id: str, *, ok: bool, error: str | None = None,
                           warning: str | None = None) -> None:
        now = int(time.time())
        with self.tx() as con:
            if ok:
                con.execute("UPDATE channels SET last_refresh_at=?, last_success_at=?,"
                            " last_error=NULL, last_error_at=NULL, last_warning=?"
                            " WHERE channel_id=?", (now, now, warning, channel_id))
            else:
                con.execute("UPDATE channels SET last_refresh_at=?, last_error=?, last_error_at=?"
                            " WHERE channel_id=?", (now, error, now, channel_id))

    def mark_tab_checked(self, channel_id: str, tab: str, when: int | None = None) -> None:
        assert tab in ("videos", "shorts", "streams")
        with self.tx() as con:
            con.execute(f"UPDATE channels SET tab_checked_{tab}=? WHERE channel_id=?",
                        (when or int(time.time()), channel_id))

    # --- videolar (yazma) ----------------------------------------------------
    def upsert_video(self, *, video_id: str, channel_id: str, title: str | None,
                     description: str | None, published_at: int | None,
                     updated_at: int | None = None, thumb_url: str | None = None,
                     views: int | None = None, duration: int | None = None,
                     seen_in_feed: bool = True, date_estimated: bool = False) -> bool:
        """video_id ile ekle/güncelle. Yeni kayıt ise True.

        Var olan kayıtta başlık/açıklama/küçük resim güncellenir; okundu, yıldız,
        kullanıcı türü ve sınıflandırma alanlarına dokunulmaz.
        """
        now = int(time.time())
        if not thumb_url and video_id:
            thumb_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
        with self.tx() as con:
            row = con.execute("SELECT published_at, date_estimated FROM videos WHERE video_id=?",
                              (video_id,)).fetchone()
            if row is None:
                con.execute(
                    "INSERT INTO videos(video_id, channel_id, title, description, published_at,"
                    " date_estimated, updated_at, thumb_url, views, duration, first_seen_at,"
                    " last_seen_in_feed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (video_id, channel_id, title or "", description or "",
                     published_at if published_at is not None else now,
                     int(date_estimated or published_at is None), updated_at, thumb_url, views,
                     duration, now, now if seen_in_feed else None))
                return True
            # Mevcut kayıt: yalnızca içerik alanları
            pub = row["published_at"]
            est = row["date_estimated"]
            if published_at is not None and (est or pub is None) and not date_estimated:
                pub, est = published_at, 0
            con.execute(
                "UPDATE videos SET title=CASE WHEN ?<>'' THEN ? ELSE title END,"
                " description=CASE WHEN ?<>'' THEN ? ELSE description END,"
                " published_at=?, date_estimated=?, updated_at=COALESCE(?, updated_at),"
                " thumb_url=COALESCE(?, thumb_url), views=COALESCE(?, views),"
                " duration=COALESCE(?, duration),"
                " last_seen_in_feed_at=CASE WHEN ? THEN ? ELSE last_seen_in_feed_at END"
                " WHERE video_id=?",
                (title or "", title or "", description or "", description or "", pub, est,
                 updated_at, thumb_url, views, duration, int(seen_in_feed), now, video_id))
            return False

    def video_exists(self, video_id: str) -> bool:
        return self.q1("SELECT 1 FROM videos WHERE video_id=?", (video_id,)) is not None

    def add_evidence(self, video_id: str, signal: str, value: Any, when: int | None = None) -> None:
        val = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        with self.tx() as con:
            con.execute("INSERT INTO evidence(video_id, signal, value, observed_at)"
                        " VALUES(?,?,?,?) ON CONFLICT(video_id, signal) DO UPDATE SET"
                        " value=excluded.value, observed_at=excluded.observed_at",
                        (video_id, signal, val, when or int(time.time())))

    def evidence(self, video_id: str) -> dict[str, tuple[str, int]]:
        return {r["signal"]: (r["value"], r["observed_at"])
                for r in self.q("SELECT signal, value, observed_at FROM evidence WHERE video_id=?",
                                (video_id,))}

    def save_classification(self, video_id: str, cls, next_check: int | None,
                            keep_schedule: bool = False) -> None:
        with self.tx() as con:
            con.execute(
                "UPDATE videos SET auto_type=?, live_state=?, premiere_state=?,"
                " scheduled_at=COALESCE(?, scheduled_at), class_source=?, class_reason=?,"
                " class_confidence=?, classified_at=?, classifier_version=?"
                + ("" if keep_schedule else ", next_meta_check_at=?")
                + " WHERE video_id=?",
                (cls.type.value, cls.live_state.value if cls.live_state else None,
                 cls.premiere_state.value if cls.premiere_state else None, cls.scheduled_at,
                 cls.source, cls.reason, cls.confidence, cls.classified_at, cls.version,
                 *(() if keep_schedule else (next_check,)), video_id))

    def record_meta_attempt(self, video_id: str, *, ok: bool, availability: str | None) -> None:
        now = int(time.time())
        with self.tx() as con:
            con.execute("UPDATE videos SET meta_checked_at=?, meta_attempts=meta_attempts+1,"
                        " availability=COALESCE(?, availability) WHERE video_id=?",
                        (now, availability, video_id))
            if ok:
                con.execute("UPDATE videos SET meta_attempts=0 WHERE video_id=?", (video_id,))

    def set_next_meta_check(self, video_id: str, when: int | None) -> None:
        with self.tx() as con:
            con.execute("UPDATE videos SET next_meta_check_at=? WHERE video_id=?", (when, video_id))

    def set_read(self, video_ids: Iterable[str] | str, read: bool) -> None:
        ids = [video_ids] if isinstance(video_ids, str) else list(video_ids)
        with self.tx() as con:
            con.executemany("UPDATE videos SET is_read=? WHERE video_id=?",
                            [(int(read), v) for v in ids])

    def set_starred(self, video_id: str, starred: bool) -> None:
        with self.tx() as con:
            con.execute("UPDATE videos SET is_starred=? WHERE video_id=?", (int(starred), video_id))

    def set_watched(self, video_ids: Iterable[str] | str, watched: bool) -> None:
        """İzlendi / İzlenmedi durumunu değiştirir (okundu ve yıldızlı durumlarından bağımsızdır)."""
        ids = [video_ids] if isinstance(video_ids, str) else list(video_ids)
        with self.tx() as con:
            con.executemany(
                "UPDATE videos SET is_watched=? WHERE video_id=?",
                [(int(watched), v) for v in ids],
            )

    def set_watch_later(
        self,
        video_id: str,
        in_queue: bool,
        *,
        order: int | None = None,
        added_at: int | None = None,
    ) -> None:
        """Videoyu Sonra İzle kuyruğuna ekler veya kuyruktan çıkarır."""
        now = int(added_at if added_at is not None else time.time())
        with self.tx() as con:
            if in_queue:
                cur_row = con.execute(
                    "SELECT watch_later, watch_later_order FROM videos WHERE video_id=?",
                    (video_id,),
                ).fetchone()
                if cur_row is None:
                    return
                if cur_row["watch_later"] and cur_row["watch_later_order"] is not None and order is None:
                    return
                if order is None:
                    mx = con.execute(
                        "SELECT COALESCE(MAX(watch_later_order), 0) FROM videos WHERE watch_later=1"
                    ).fetchone()[0]
                    order = int(mx) + 1
                con.execute(
                    "UPDATE videos SET watch_later=1, watch_later_order=?, "
                    "watch_later_added_at=COALESCE(watch_later_added_at, ?) WHERE video_id=?",
                    (int(order), now, video_id),
                )
            else:
                con.execute(
                    "UPDATE videos SET watch_later=0, watch_later_order=NULL, "
                    "watch_later_added_at=NULL WHERE video_id=?",
                    (video_id,),
                )

    def reorder_watch_later(self, ordered_video_ids: list[str]) -> None:
        """Sonra İzle kuyruğundaki videoların sırasını kalıcı olarak günceller.

        Eğer `ordered_video_ids` izlenenlerin gizlenmesi nedeniyle yalnızca görünür
        alt kümeyi içeriyorsa, gizli olanların göreli konumlarını koruyarak sıralar.
        """
        if not ordered_video_ids:
            return
        with self.tx() as con:
            all_rows = con.execute(
                "SELECT video_id FROM videos WHERE watch_later=1 "
                "ORDER BY COALESCE(watch_later_order, 999999999) ASC, "
                "COALESCE(watch_later_added_at, 0) ASC, video_id"
            ).fetchall()
            current_all = [r["video_id"] for r in all_rows]
            subset_set = set(ordered_video_ids)
            ordered_clean = [vid for vid in ordered_video_ids if vid in set(current_all)]
            if not ordered_clean:
                return
            it = iter(ordered_clean)
            merged: list[str] = []
            for vid in current_all:
                if vid in subset_set:
                    nxt = next(it, None)
                    if nxt is not None:
                        merged.append(nxt)
                else:
                    merged.append(vid)
            for rem in it:
                merged.append(rem)
            con.executemany(
                "UPDATE videos SET watch_later_order=? WHERE video_id=?",
                [(idx, vid) for idx, vid in enumerate(merged, start=1)],
            )

    def watch_later_ids(self, include_watched: bool = True) -> list[str]:
        extra = "" if include_watched else " AND is_watched=0"
        rows = self.q(
            f"SELECT video_id FROM videos WHERE watch_later=1{extra} "
            "ORDER BY COALESCE(watch_later_order, 999999999) ASC, "
            "COALESCE(watch_later_added_at, 0) ASC, video_id"
        )
        return [r["video_id"] for r in rows]

    def set_user_type(self, video_id: str, user_type: str | None) -> None:
        with self.tx() as con:
            con.execute("UPDATE videos SET user_type=? WHERE video_id=?", (user_type, video_id))

    def mark_notified_new(self, video_ids: Iterable[str]) -> None:
        ids = list(video_ids)
        if not ids:
            return
        with self.tx() as con:
            con.executemany(
                "UPDATE videos SET notified_new=1 WHERE video_id=?",
                [(vid,) for vid in ids],
            )

    def mark_notified_live_start(self, video_ids: Iterable[str]) -> None:
        ids = list(video_ids)
        if not ids:
            return
        with self.tx() as con:
            con.executemany(
                "UPDATE videos SET notified_live_start=1 WHERE video_id=?",
                [(vid,) for vid in ids],
            )

    def mark_duration_checked(self, video_id: str, duration: int | None = None, when: int | None = None) -> None:
        now = int(when if when is not None else time.time())
        with self.tx() as con:
            if duration is not None and duration > 0:
                con.execute(
                    "UPDATE videos SET duration=?, duration_checked_at=? WHERE video_id=?",
                    (int(duration), now, video_id),
                )
            else:
                con.execute(
                    "UPDATE videos SET duration_checked_at=? WHERE video_id=?",
                    (now, video_id),
                )

    def videos_missing_duration(
        self, video_ids: Iterable[str] | None = None, limit: int = 6
    ) -> list[sqlite3.Row]:
        """Süresi bilinmeyen ve daha önce süre kontrolü yapılmamış videoları döndürür."""
        if video_ids is not None:
            ids = list(video_ids)
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            return self.q(
                f"SELECT video_id, channel_id, auto_type, user_type, live_state "
                f"FROM videos WHERE video_id IN ({placeholders}) "
                "AND (duration IS NULL OR duration <= 0) "
                "AND duration_checked_at IS NULL "
                "AND COALESCE(live_state, '') NOT IN ('live', 'scheduled') "
                "ORDER BY published_at DESC LIMIT ?",
                (*ids, limit),
            )
        return self.q(
            "SELECT video_id, channel_id, auto_type, user_type, live_state "
            "FROM videos WHERE (duration IS NULL OR duration <= 0) "
            "AND duration_checked_at IS NULL "
            "AND COALESCE(live_state, '') NOT IN ('live', 'scheduled') "
            "ORDER BY published_at DESC LIMIT ?",
            (limit,),
        )

    # --- videolar (okuma) ------------------------------------------------------
    def video(self, video_id: str) -> sqlite3.Row | None:
        return self.q1("SELECT v.*, c.title AS channel_title, c.avatar_url FROM videos v"
                       " JOIN channels c USING(channel_id) WHERE v.video_id=?", (video_id,))

    def channel_video_ids(self, channel_id: str) -> set[str]:
        return {r[0] for r in self.q("SELECT video_id FROM videos WHERE channel_id=?",
                                     (channel_id,))}

    def due_meta_checks(self, now: int, limit: int) -> list[sqlite3.Row]:
        # Canlı/planlı yayınlar önce, sonra belirsizler; en eski vade önce.
        return self.q(
            "SELECT video_id, channel_id, auto_type, live_state, premiere_state, scheduled_at,"
            " meta_attempts FROM videos WHERE next_meta_check_at IS NOT NULL"
            " AND next_meta_check_at<=? ORDER BY"
            " CASE WHEN live_state='live' OR premiere_state='live' THEN 0"
            "      WHEN live_state='scheduled' OR premiere_state='scheduled' THEN 1"
            "      WHEN auto_type='live' THEN 2 ELSE 3 END, next_meta_check_at LIMIT ?",
            (now, limit))

    def videos_needing_reclassify(self, version: int) -> list[str]:
        return [r[0] for r in self.q(
            "SELECT video_id FROM videos WHERE classifier_version IS NULL OR classifier_version<>?",
            (version,))]


# --- görünürlük ve liste sorguları -----------------------------------------------

EFFECTIVE_TYPE = "COALESCE(v.user_type, v.auto_type)"


def visible_sql(vis: Visibility) -> tuple[str, dict]:
    """Kanal tercihi (NULL ise genel varsayılan) ile içeriğin görünürlüğü."""
    sql = (f"(CASE {EFFECTIVE_TYPE}"
           " WHEN 'video' THEN COALESCE(c.show_video, :gv)"
           " WHEN 'short' THEN COALESCE(c.show_short, :gs)"
           " WHEN 'live' THEN COALESCE(c.show_live, :gl)"
           " ELSE :gu END) = 1")
    return sql, {"gv": vis.video, "gs": vis.short, "gl": vis.live, "gu": vis.unknown}


def start_of_today() -> int:
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


@dataclass(slots=True)
class ListQuery:
    node: str = "all"                 # all | today | unread | starred | watch_later | unknown | group:<id> | channel:<cid>
    read_filter: str = "all"          # all | unread | starred
    type_filter: str = "any"          # any | video | short | live | unknown
    duration_filter: str = "any"      # any | under_5 | 5_to_15 | 15_to_30 | over_30 | unknown
    search: str = ""
    hide_watched_in_queue: bool = False


def build_list_sql(q: ListQuery, vis: Visibility) -> tuple[str, dict]:
    where: list[str] = []
    params: dict[str, Any] = {}
    vsql, vparams = visible_sql(vis)
    node = q.node
    if node == "unknown":
        where.append(f"{EFFECTIVE_TYPE}='unknown'")
    elif node == "starred":
        where.append("v.is_starred=1")
    elif node == "watch_later":
        where.append("v.watch_later=1")
        if q.hide_watched_in_queue:
            where.append("v.is_watched=0")
    else:
        where.append(vsql)
        params.update(vparams)
        if node == "today":
            where.append("v.published_at>=:today")
            params["today"] = start_of_today()
        elif node == "unread":
            where.append("v.is_read=0")
        elif node.startswith("group:"):
            where.append("c.group_id=:gid")
            params["gid"] = int(node.split(":", 1)[1])
        elif node.startswith("channel:"):
            where.append("v.channel_id=:cid")
            params["cid"] = node.split(":", 1)[1]
    if q.read_filter == "unread":
        where.append("v.is_read=0")
    elif q.read_filter == "starred":
        where.append("v.is_starred=1")
    if q.type_filter != "any":
        where.append(f"{EFFECTIVE_TYPE}=:tf")
        params["tf"] = q.type_filter
    if q.duration_filter == "under_5":
        where.append("(v.duration IS NOT NULL AND v.duration > 0 AND v.duration < 300)")
    elif q.duration_filter == "5_to_15":
        where.append("(v.duration IS NOT NULL AND v.duration >= 300 AND v.duration < 900)")
    elif q.duration_filter == "15_to_30":
        where.append("(v.duration IS NOT NULL AND v.duration >= 900 AND v.duration < 1800)")
    elif q.duration_filter == "over_30":
        where.append("(v.duration IS NOT NULL AND v.duration >= 1800)")
    elif q.duration_filter == "unknown":
        where.append("(v.duration IS NULL OR v.duration <= 0)")
    if q.search.strip():
        params["s"] = f"%{tr_fold(q.search.strip())}%"
        where.append("(tr_fold(v.title) LIKE :s OR tr_fold(v.description) LIKE :s"
                     " OR tr_fold(c.title) LIKE :s)")
    if node == "watch_later":
        order_sql = (
            " ORDER BY COALESCE(v.watch_later_order, 999999999) ASC, "
            "COALESCE(v.watch_later_added_at, 0) ASC, v.video_id"
        )
    else:
        order_sql = " ORDER BY v.published_at DESC, v.video_id"
    sql = ("SELECT v.video_id, v.channel_id, v.title, substr(v.description, 1, 400) AS description,"
           " v.published_at, v.date_estimated, v.thumb_url, v.duration, v.auto_type, v.user_type,"
           " v.live_state, v.premiere_state, v.scheduled_at, v.is_read, v.is_starred,"
           " v.is_watched, v.watch_later, v.watch_later_order, v.watch_later_added_at,"
           " v.availability, c.title AS channel_title, c.avatar_url"
           " FROM videos v JOIN channels c USING(channel_id)"
           " WHERE " + " AND ".join(where or ["1"]) +
           order_sql)
    return sql, params


def unread_counts(db: Database, vis: Visibility) -> dict[str, int]:
    """Kenar çubuğu sayaçları: görünürlük tercihleriyle tutarlı okunmamış sayıları."""
    vsql, params = visible_sql(vis)
    params = dict(params, today=start_of_today())
    counts: dict[str, int] = {}
    total = today = 0
    for r in db.q(
            f"SELECT v.channel_id, c.group_id, COUNT(*) AS n,"
            f" SUM(v.published_at>=:today) AS t FROM videos v JOIN channels c USING(channel_id)"
            f" WHERE v.is_read=0 AND {vsql} GROUP BY v.channel_id", params):
        counts[f"channel:{r['channel_id']}"] = r["n"]
        if r["group_id"] is not None:
            key = f"group:{r['group_id']}"
            counts[key] = counts.get(key, 0) + r["n"]
        total += r["n"]
        today += r["t"] or 0
    counts["all"] = total
    counts["unread"] = total
    counts["today"] = today
    counts["starred"] = db.q1("SELECT COUNT(*) FROM videos WHERE is_starred=1")[0]
    hide_watched = bool(db.get_setting("watch_later_hide_watched"))
    if hide_watched:
        counts["watch_later"] = db.q1(
            "SELECT COUNT(*) FROM videos WHERE watch_later=1 AND is_watched=0"
        )[0]
    else:
        counts["watch_later"] = db.q1(
            "SELECT COUNT(*) FROM videos WHERE watch_later=1"
        )[0]
    counts["unknown"] = db.q1(
        f"SELECT COUNT(*) FROM videos v WHERE {EFFECTIVE_TYPE}='unknown' AND v.is_read=0")[0]
    return counts

