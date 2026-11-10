"""Çekirdek birim ve entegrasyon testleri (kaydedilmiş gerçek YouTube verileriyle)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ytrss.classifier import CLASSIFIER_VERSION, classify, compute_next_meta_check
from ytrss.db import Database, ListQuery, build_list_sql, unread_counts
from ytrss.models import (
    ContentType,
    FeedEntry,
    FeedResult,
    LiveState,
    ResolvedChannel,
    TabEntry,
    VideoMetadata,
)
from ytrss.subscriptions import (
    BACKUP_VERSION,
    export_backup_file,
    export_subscriptions_opml,
    import_candidates_to_db,
    parse_subscriptions_file,
    parse_subscriptions_report_text,
    parse_subscriptions_text,
    restore_backup_file,
)
from ytrss.sync import (
    add_channel,
    backfill_missing_durations,
    fetch_single_video_metadata,
    reclassify_video,
    sync_channel,
    sync_channels,
)
from ytrss.ui.video_list import format_duration, thumbnail_corner_overlay
from ytrss.youtube.feeds import parse_feed
from ytrss.youtube.http import NetError
from ytrss.youtube.urls import InvalidChannelInput, parse_channel_input

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# 1. Kanal adresi ayrıştırma ve tekrar ekleme engeli
# ---------------------------------------------------------------------------

def test_parse_channel_inputs():
    cid = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert parse_channel_input(cid).value == cid
    assert parse_channel_input(f"https://www.youtube.com/channel/{cid}").value == cid
    assert parse_channel_input(f"https://www.youtube.com/channel/{cid}/videos").value == cid
    assert parse_channel_input(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    ).value == cid
    assert parse_channel_input(
        f"https://www.youtube.com/feeds/videos.xml?playlist_id=UULF{cid[2:]}"
    ).value == cid

    h = parse_channel_input("@GoogleDevelopers")
    assert h.kind == "handle" and h.value == "@GoogleDevelopers"
    h2 = parse_channel_input("https://www.youtube.com/@GoogleDevelopers/shorts")
    assert h2.kind == "handle" and h2.value == "@GoogleDevelopers"

    leg = parse_channel_input("https://www.youtube.com/user/GoogleDevelopers")
    assert leg.kind == "legacy" and leg.value == "user/GoogleDevelopers"

    for bad in [
        "",
        "https://example.com/@test",
        "https://www.youtube.com/watch?v=rsOiZE19RVo",
        "https://www.youtube.com/shorts/-nmcYcO-EE8",
    ]:
        with pytest.raises(InvalidChannelInput):
            parse_channel_input(bad)


class FakeSource:
    """Kaydedilmiş yanıtlarla çalışan sahte YouTube kaynağı."""

    def __init__(self) -> None:
        self.feeds: dict[tuple[str, str | None], FeedResult | Exception] = {}
        self.tabs: dict[tuple[str, str], list[TabEntry] | Exception] = {}
        self.metas: dict[str, VideoMetadata] = {}
        self.shorts_probes: dict[str, bool | None] = {}
        self.resolved: dict[str, ResolvedChannel] = {}
        self.meta_calls: list[str] = []

    def fetch_feed(self, channel_id, kind=None, cancel=None) -> FeedResult:
        v = self.feeds.get((channel_id, kind))
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise NetError("Bulunamadı", "not_found", 404)
        return v

    def list_tab(self, channel_id, tab, limit=15, cancel=None) -> list[TabEntry]:
        v = self.tabs.get((channel_id, tab), [])
        if isinstance(v, Exception):
            raise v
        return list(v)[:limit]

    def video_metadata(self, video_id, cancel=None) -> VideoMetadata:
        self.meta_calls.append(video_id)
        if video_id in self.metas:
            return self.metas[video_id]
        return VideoMetadata(video_id=video_id, ok=False, error="Kayıt yok", error_kind="unavailable")

    def shorts_probe(self, video_id, cancel=None) -> bool | None:
        return self.shorts_probes.get(video_id)

    def resolve(self, ref) -> ResolvedChannel:
        if ref.value in self.resolved:
            return self.resolved[ref.value]
        if ref.is_resolved:
            return ResolvedChannel(channel_id=ref.value, title="Test Kanalı")
        raise NetError("Kanal bulunamadı", "not_found", 404)


def test_duplicate_channel_prevention(tmp_path):
    db = Database(tmp_path / "test.db")
    src = FakeSource()
    cid = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    resolved = ResolvedChannel(channel_id=cid, title="Google for Developers", handle="@GoogleDevelopers")
    src.resolved["@GoogleDevelopers"] = resolved
    src.resolved[cid] = resolved

    r1, added1 = add_channel(db, src, "@GoogleDevelopers")
    r2, added2 = add_channel(db, src, f"https://www.youtube.com/channel/{cid}")
    r3, added3 = add_channel(db, src, f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}")

    assert added1 is True
    assert added2 is False
    assert added3 is False
    assert r1.channel_id == r2.channel_id == r3.channel_id == cid
    assert len(db.channels()) == 1


# ---------------------------------------------------------------------------
# 2. Gerçek YouTube örnekleriyle sınıflandırıcı doğrulaması (real_cases.jsonl)
# ---------------------------------------------------------------------------

def _load_real_cases() -> list[dict]:
    lines = (FIXTURES / "real_cases.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _evidence_from_real_case(rec: dict, include_meta: bool = True) -> dict[str, tuple[str, int]]:
    ev: dict[str, tuple[str, int]] = {}
    ts = 1794320000
    feeds = rec.get("feeds") or {}
    if feeds.get("UULF") is True:
        ev["feed_uulf"] = ("1", ts)
    if feeds.get("UUSH") is True:
        ev["feed_uush"] = ("1", ts)
    if feeds.get("UULV") is True:
        ev["feed_uulv"] = ("1", ts)

    sh = rec.get("shorts_head_status")
    if sh == 200:
        ev["shorts_head"] = ("200", ts)
    elif sh == 303:
        ev["shorts_head"] = ("303_watch", ts)

    if include_meta:
        ok = bool(rec.get("channel_id") or rec.get("live_status") or rec.get("availability"))
        meta = VideoMetadata(
            video_id=rec["video_id"],
            ok=ok,
            error=None if ok else "Video kullanılamıyor",
            error_kind=None if ok else "unavailable",
            title=rec.get("title"),
            channel_id=rec.get("channel_id"),
            duration=rec.get("duration"),
            live_status=rec.get("live_status"),
            media_type=rec.get("media_type"),
            release_timestamp=rec.get("release_timestamp"),
            timestamp=rec.get("timestamp"),
            availability=rec.get("availability"),
        )
        ev["ytdlp_meta"] = (json.dumps(meta.to_json()), ts)
    return ev


def test_real_youtube_cases_classification():
    cases = _load_real_cases()
    assert len(cases) >= 18

    for rec in cases:
        case = rec["case"]
        vid = rec["video_id"]
        cls = classify(_evidence_from_real_case(rec, include_meta=True), now=1794320000)

        if case == "live_active":
            assert cls.type == ContentType.LIVE, f"{vid} canlı yayın olmalıydı"
            assert cls.live_state == LiveState.LIVE
            assert cls.premiere_state is None
        elif case == "live_upcoming":
            assert cls.type == ContentType.LIVE, f"{vid} planlı canlı yayın olmalıydı"
            assert cls.live_state == LiveState.SCHEDULED
            assert cls.scheduled_at == rec["release_timestamp"]
        elif case == "live_completed":
            assert cls.type == ContentType.LIVE, f"{vid} tamamlanan yayın canlı kategorisinde kalmalı"
            assert cls.live_state == LiveState.COMPLETED
        elif case == "long_video_with_shorts_in_title":
            # Başlığında #shorts geçse bile normal video olmalı!
            assert "#shorts" in rec["title"].lower()
            assert cls.type == ContentType.VIDEO, f"{vid} #shorts başlığına rağmen normal video olmalı"
        elif case == "short_candidate":
            # Etiketsiz Shorts ve 1–3 dakikalık Shorts (64 sn, 73 sn, 86 sn, 90 sn)
            assert rec["title_has_#shorts"] is False
            assert cls.type == ContentType.SHORT, f"{vid} ({rec['duration']}s) Shorts olmalıydı"
        elif case == "normal_short_video":
            # Kısa süreli (27 sn, 37 sn) normal yatay videolar Shorts sayılmamalı!
            assert rec["duration"] < 60
            assert cls.type == ContentType.VIDEO, f"{vid} ({rec['duration']}s) normal video olmalıydı"
        elif case == "premiere_completed":
            # Tamamlanmış prömiyer canlı yayın sayılmamalı!
            assert cls.type == ContentType.VIDEO, f"{vid} prömiyer normal video olmalıydı"
            assert cls.premiere_state == LiveState.COMPLETED
            assert cls.live_state is None
        elif case == "unavailable_random_id":
            # Erişilemeyen video zorla normal video yapılmamalı!
            assert cls.type == ContentType.UNKNOWN, f"{vid} erişilemeyen içerik UNKNOWN olmalıydı"


def test_premiere_upcoming_and_live_not_counted_as_livestream():
    ts = 1794320000
    # Planlanmış prömiyer: media_type='video', live_status='is_upcoming'
    up_meta = VideoMetadata(
        video_id="premUp00001", ok=True, title="Yeni Albüm Prömiyeri",
        channel_id="UC3IZKseVpdzPSBaWxBxundA", duration=210,
        live_status="is_upcoming", media_type="video",
        release_timestamp=ts + 3600, availability="public",
    )
    cls_up = classify({"ytdlp_meta": (json.dumps(up_meta.to_json()), ts)}, now=ts)
    assert cls_up.type == ContentType.VIDEO
    assert cls_up.live_state is None
    assert cls_up.premiere_state == LiveState.SCHEDULED
    assert cls_up.scheduled_at == ts + 3600
    assert compute_next_meta_check(cls_up, ts) == ts + 900

    # Şu anda oynatılan prömiyer: media_type='video', live_status='is_live'
    live_meta = VideoMetadata(
        video_id="premLv00002", ok=True, title="Canlı Prömiyer",
        channel_id="UC3IZKseVpdzPSBaWxBxundA", duration=210,
        live_status="is_live", media_type="video",
        release_timestamp=ts - 60, availability="public",
    )
    cls_lv = classify({"ytdlp_meta": (json.dumps(live_meta.to_json()), ts)}, now=ts)
    assert cls_lv.type == ContentType.VIDEO
    assert cls_lv.live_state is None
    assert cls_lv.premiere_state == LiveState.LIVE
    assert compute_next_meta_check(cls_lv, ts) == ts + 300


def test_insufficient_and_conflicting_signals_stay_unknown():
    ts = 1794320000
    # 1. Hiç sinyal yok (yalnızca ana RSS'den geldi)
    c0 = classify({}, now=ts)
    assert c0.type == ContentType.UNKNOWN

    # 2. Yalnızca /shorts/ -> 303 /watch yönlendirmesi var (canlı mı normal mi bilinmiyor)
    c1 = classify({"shorts_head": ("303_watch", ts)}, now=ts)
    assert c1.type == ContentType.UNKNOWN

    # 3. /videos sekmesinde is_upcoming görüldü ama henüz yt-dlp media_type kontrolü yok
    c2 = classify({"tab_videos": (json.dumps({"live_status": "is_upcoming"}), ts)}, now=ts)
    assert c2.type == ContentType.UNKNOWN
    assert c2.needs_meta is True

    # 4. Çelişkili sinyaller: hem UULF hem UUSH
    c3 = classify({"feed_uulf": ("1", ts), "feed_uush": ("1", ts)}, now=ts)
    assert c3.type == ContentType.UNKNOWN
    assert "conflict" in c3.source

    # 5. Çelişkili sinyaller: UUSH (Shorts) ama /shorts/ID 303 /watch döndürdü
    c4 = classify({"feed_uush": ("1", ts), "shorts_head": ("303_watch", ts)}, now=ts)
    assert c4.type == ContentType.UNKNOWN
    assert "conflict" in c4.source


# ---------------------------------------------------------------------------
# 3. Eşitleme, tekrar kayıt engeli, kanal filtreleri ve kalıcılık
# ---------------------------------------------------------------------------

def test_sync_and_visibility_and_persistence(tmp_path):
    db_file = tmp_path / "app.sqlite3"
    db = Database(db_file)
    src = FakeSource()

    cid_a = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    ch_xml = (FIXTURES / "feeds" / "googledev_channel.xml").read_bytes()
    uulf_xml = (FIXTURES / "feeds" / "googledev_UULF.xml").read_bytes()
    uush_xml = (FIXTURES / "feeds" / "googledev_UUSH.xml").read_bytes()
    uulv_xml = (FIXTURES / "feeds" / "googledev_UULV.xml").read_bytes()

    src.feeds[(cid_a, None)] = parse_feed(ch_xml, cid_a)
    src.feeds[(cid_a, "videos")] = parse_feed(uulf_xml, cid_a)
    src.feeds[(cid_a, "shorts")] = parse_feed(uush_xml, cid_a)
    src.feeds[(cid_a, "streams")] = parse_feed(uulv_xml, cid_a)
    # /streams sekmesi: UULV'deki tüm yayınlar tamamlanmış (was_live) + 1 aktif yayın
    uulv_ids = [e.video_id for e in src.feeds[(cid_a, "streams")].entries]
    src.tabs[(cid_a, "streams")] = [
        TabEntry(video_id=vid, title=f"Stream {vid}", url=None, duration=1800,
                 live_status="was_live", position=i)
        for i, vid in enumerate(uulv_ids)
    ] + [
        TabEntry(video_id="activeLive1", title="24/7 Dev Live", url=None, duration=None,
                 live_status="is_live", position=99)
    ]
    src.metas["activeLive1"] = VideoMetadata(
        video_id="activeLive1", ok=True, title="24/7 Dev Live", channel_id=cid_a,
        live_status="is_live", media_type="livestream", release_timestamp=1794300000,
        availability="public",
    )

    db.insert_channel(cid_a, "Google for Developers", "@GoogleDevelopers", None)
    res1 = sync_channel(db, src, cid_a, force_tabs=True)
    assert res1.ok is True
    assert res1.new_count > 15

    # Türlerin doğru ayrıldığını kontrol et
    v_normal = db.video("7sKHiuE7J-Y")  # 72 sn normal video
    v_short = db.video("-nmcYcO-EE8")   # 32 sn Short
    v_done_live = db.video("-3pdAavNepg")  # tamamlanmış canlı yayın
    v_active_live = db.video("activeLive1")  # aktif canlı yayın

    assert v_normal["auto_type"] == "video"
    assert v_short["auto_type"] == "short"
    assert v_done_live["auto_type"] == "live" and v_done_live["live_state"] == "completed"
    assert v_active_live["auto_type"] == "live" and v_active_live["live_state"] == "live"

    # Okundu, yıldız ve kullanıcı tür düzeltmesi yap
    db.set_read(["7sKHiuE7J-Y"], True)
    db.set_starred("7sKHiuE7J-Y", True)
    db.set_user_type("-nmcYcO-EE8", "video")  # kullanıcı elle normal video yaptı

    # Başlık değiştiğinde ve tekrar eşitlendiğinde:
    # - Yeni kayıt oluşmamalı
    # - Okundu, yıldız ve user_type korunmalı
    # - Sınıflandırılmış videolar tekrar yt-dlp ile sorgulanmamalı
    src.meta_calls.clear()
    src.feeds[(cid_a, None)].entries[0].title = "Güncellenmiş Başlık"
    total_before = db.q1("SELECT COUNT(*) FROM videos")[0]
    res2 = sync_channel(db, src, cid_a, force_tabs=False)
    total_after = db.q1("SELECT COUNT(*) FROM videos")[0]
    assert res2.ok is True
    assert total_after == total_before
    assert src.meta_calls == []  # tamamlanmış/sınıflandırılmış hiçbir video tekrar sorgulanmadı!

    v_normal_after = db.video("7sKHiuE7J-Y")
    assert v_normal_after["is_read"] == 1
    assert v_normal_after["is_starred"] == 1
    assert db.video("-nmcYcO-EE8")["user_type"] == "video"
    assert db.video("rsOiZE19RVo")["title"] == "Güncellenmiş Başlık"

    # Kullanıcı tür düzeltmesini geri al
    db.set_user_type("-nmcYcO-EE8", None)

    # 3 kanallı görünürlük senaryosu (A, B, C) + hata izolasyonu
    cid_b = "UCBBBBBBBBBBBBBBBBBBBBBB"
    cid_c = "UCCCCCCCCCCCCCCCCCCCCCCC"
    cid_err = "UCERRRRRRRRRRRRRRRRRRRRR"
    db.insert_channel(cid_b, "Kanal B", "@kanalb", None)
    db.insert_channel(cid_c, "Kanal C", "@kanalc", None)
    db.insert_channel(cid_err, "Hatalı Kanal", "@hatali", None)

    for cid, pfx in ((cid_b, "B"), (cid_c, "C")):
        v_id = f"{pfx}_vid_00001"
        s_id = f"{pfx}_sht_00001"
        l_id = f"{pfx}_liv_00001"
        entries = [
            FeedEntry(video_id=v_id, channel_id=cid, title=f"{pfx} Video", published=1794310000),
            FeedEntry(video_id=s_id, channel_id=cid, title=f"{pfx} Short", published=1794310100),
            FeedEntry(video_id=l_id, channel_id=cid, title=f"{pfx} Live", published=1794310200),
        ]
        src.feeds[(cid, None)] = FeedResult(channel_id=cid, title=f"Kanal {pfx}", entries=entries)
        src.feeds[(cid, "videos")] = FeedResult(channel_id=cid, title="Videos", entries=[entries[0]])
        src.feeds[(cid, "shorts")] = FeedResult(channel_id=cid, title="Shorts", entries=[entries[1]])
        src.feeds[(cid, "streams")] = FeedResult(channel_id=cid, title="Streams", entries=[entries[2]])
        src.tabs[(cid, "streams")] = [
            TabEntry(video_id=l_id, title=f"{pfx} Live", url=None, duration=600,
                     live_status="was_live", position=0)
        ]
    src.feeds[(cid_err, None)] = NetError("Ağ zaman aşımı", "network")

    # Eşzamanlı çoklu kanal eşitlemesi: hatalı kanal diğerlerini durdurmamalı
    summary = sync_channels(db, src, [cid_b, cid_c, cid_err])
    assert summary.error_count == 1
    assert db.channel(cid_err)["last_error"] is not None
    assert db.channel(cid_b)["last_error"] is None
    assert db.channel(cid_c)["last_error"] is None

    # Kanal bazında görünürlük ayarları:
    # Genel varsayılanlar: Normal=açık(1), Shorts=kapalı(0), Canlı=kapalı(0)
    vis = db.visibility_defaults()
    assert (vis.video, vis.short, vis.live) == (1, 0, 0)

    # A: genel varsayılanı izler (yalnızca normal videolar)
    db.set_channel_prefs(cid_a, None, None, None)
    # B: normal videolar + Shorts açık (genel varsayılan Shorts kapalı olsa bile görünmeli!)
    db.set_channel_prefs(cid_b, 1, 1, 0)
    # C: yalnızca canlı yayınlar açık
    db.set_channel_prefs(cid_c, 0, 0, 1)

    db.close_thread_connection()

    # Uygulamayı yeniden başlatmayı simüle et: aynı dosyadan yeni Database nesnesi aç
    db2 = Database(db_file)
    vis2 = db2.visibility_defaults()

    def types_for(channel_id: str) -> set[str]:
        sql, params = build_list_sql(ListQuery(node=f"channel:{channel_id}"), vis2)
        return {r["auto_type"] for r in db2.q(sql, params)}

    assert types_for(cid_a) == {"video"}
    assert types_for(cid_b) == {"video", "short"}
    assert types_for(cid_c) == {"live"}

    # Gizlenen içerikler silinmemiş ve okunmuş sayılmamış olmalı
    c_vid = db2.video("C_vid_00001")
    assert c_vid is not None and c_vid["is_read"] == 0
    counts_before = unread_counts(db2, vis2)
    assert counts_before[f"channel:{cid_c}"] == 1  # yalnızca görünür olan canlı yayın sayılır

    # C kanalında normal videoları tekrar açınca geri gelmeli ve sayaç artmalı
    db2.set_channel_prefs(cid_c, 1, 0, 1)
    assert types_for(cid_c) == {"video", "live"}
    counts_after = unread_counts(db2, vis2)
    assert counts_after[f"channel:{cid_c}"] == 2


def test_subscriptions_import_csv_json_opml_and_export(tmp_path):
    # 1. Google Takeout CSV (Türkçe başlıklı)
    csv_tr = (
        "Kanal Kimliği,Kanal URL'si,Kanal Başlığı\n"
        "UC_x5XG1OV2P6uZZ5FSM9Ttw,http://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw,Google for Developers\n"
        "UCLA_DiR1FfKNvjuUpBHmylQ,http://www.youtube.com/channel/UCLA_DiR1FfKNvjuUpBHmylQ,NASA\n"
    )
    c_csv = parse_subscriptions_text(csv_tr, "abonelikler.csv")
    assert len(c_csv) == 2
    assert c_csv[0].channel_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    assert c_csv[0].title == "Google for Developers"
    assert c_csv[1].channel_id == "UCLA_DiR1FfKNvjuUpBHmylQ"

    # 2. Google Takeout JSON
    takeout_json = json.dumps([
        {
            "snippet": {
                "title": "NASASpaceflight",
                "resourceId": {"channelId": "UCSUu1lih2RifWkKtDOJdsBA"},
            }
        }
    ])
    c_json = parse_subscriptions_text(takeout_json, "subscriptions.json")
    assert len(c_json) == 1
    assert c_json[0].channel_id == "UCSUu1lih2RifWkKtDOJdsBA"
    assert c_json[0].title == "NASASpaceflight"

    # 3. Veritabanına aktarma, tekrar engeli ve OPML dışa/içe aktarma (grup koruma)
    db = Database(tmp_path / "subs.sqlite3")
    src = FakeSource()
    s1 = import_candidates_to_db(db, src, c_csv + c_json, prefs=(1, 1, 0))
    assert s1.added == 3
    assert s1.skipped_existing == 0

    # Tekrar aktarıldığında yeni kayıt oluşmamalı
    s2 = import_candidates_to_db(db, src, c_csv)
    assert s2.added == 0
    assert s2.skipped_existing == 2

    # Bir kanalı gruba ata ve OPML olarak dışa aktar
    gid = db.add_group("Uzay")
    db.set_channel_group("UCLA_DiR1FfKNvjuUpBHmylQ", gid)
    opml_file = tmp_path / "export.opml"
    exported_n = export_subscriptions_opml(db, opml_file)
    assert exported_n == 3

    # Yeni veritabanına OPML dosyasından geri yükle (klasör/grup adı korunmalı)
    db_imported = Database(tmp_path / "imported.sqlite3")
    opml_cands = parse_subscriptions_file(opml_file)
    assert len(opml_cands) == 3
    s3 = import_candidates_to_db(db_imported, src, opml_cands)
    assert s3.added == 3
    groups_in_new = {g["name"]: g["id"] for g in db_imported.groups()}
    assert "Uzay" in groups_in_new
    assert db_imported.channel("UCLA_DiR1FfKNvjuUpBHmylQ")["group_id"] == groups_in_new["Uzay"]


# ---------------------------------------------------------------------------
# 4. Şema geçişi (v1 -> v2), Sonra İzle, İzlendi, Süreler, Yedekleme, Bildirimler
# ---------------------------------------------------------------------------

def test_schema_migration_v1_to_v2_preserves_user_data(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy_v1.sqlite3"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
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
        INSERT INTO meta(key, value) VALUES ('schema_version', '1');
        INSERT INTO settings(key, value) VALUES ('show_video', '1'), ('show_short', '0'), ('show_live', '0');
        INSERT INTO channels(channel_id, title, handle, show_short, last_success_at, added_at)
        VALUES ('UC_x5XG1OV2P6uZZ5FSM9Ttw', 'Google Dev', '@GoogleDevelopers', 1, 1794300000, 1794200000);
        INSERT INTO videos(video_id, channel_id, title, published_at, first_seen_at, auto_type, user_type, is_read, is_starred)
        VALUES ('vidLegacy01', 'UC_x5XG1OV2P6uZZ5FSM9Ttw', 'Eski Video', 1794250000, 1794250000, 'short', 'video', 1, 1);
        """
    )
    con.commit()
    con.close()

    # v2 Database ile açıldığında veriler korunmalı ve yeni sütunlar hazır olmalı
    db = Database(db_path)
    assert db.q1("SELECT value FROM meta WHERE key='schema_version'")["value"] == "2"
    assert not db.get_setting("close_to_tray")
    ch = db.channel("UC_x5XG1OV2P6uZZ5FSM9Ttw")
    assert ch["title"] == "Google Dev"
    assert ch["show_short"] == 1
    assert ch["initial_sync_done"] == 1  # daha önce eşitlenmiş kanalda bildirim yağmuru önlenir

    v = db.video("vidLegacy01")
    assert v["title"] == "Eski Video"
    assert v["is_read"] == 1
    assert v["is_starred"] == 1
    assert v["user_type"] == "video"
    assert v["is_watched"] == 0
    assert v["watch_later"] == 0
    assert v["notified_new"] == 1


def test_watch_later_queue_reorder_persistence_and_watched_independence(tmp_path):
    db_file = tmp_path / "queue.sqlite3"
    db = Database(db_file)
    cid = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    db.insert_channel(cid, "Test Kanal", "@test", None)

    for idx, vid in enumerate(["vid_A000001", "vid_B000002", "vid_C000003"], start=1):
        db.upsert_video(video_id=vid, channel_id=cid, title=f'Video {idx}', description='', published_at=1794300000 + idx * 100, updated_at=None, thumb_url=None, duration=300 * idx)
        db.save_classification(vid, classify({"feed_uulf": ("1", 1794305000)}, now=1794305000), None)

    # 3 videoyu sırayla Sonra İzle kuyruğuna ekle
    db.set_watch_later("vid_A000001", True)
    db.set_watch_later("vid_B000002", True)
    db.set_watch_later("vid_C000003", True)
    assert db.watch_later_ids() == ["vid_A000001", "vid_B000002", "vid_C000003"]

    # Kuyrukta sürükle-bırak sıralamasını değiştir: C -> A -> B
    db.reorder_watch_later(["vid_C000003", "vid_A000001", "vid_B000002"])

    # İzlendi durumu okundu ve yıldızlı durumlarından bağımsız olmalı
    db.set_read(["vid_C000003"], True)
    db.set_starred("vid_C000003", True)
    assert db.video("vid_C000003")["is_watched"] == 0

    db.set_watched(["vid_A000001"], True)
    v_a = db.video("vid_A000001")
    assert v_a["is_watched"] == 1
    assert v_a["is_read"] == 0
    assert v_a["is_starred"] == 0

    db.close_thread_connection()

    # Yeniden açılışta sıra ve durumlar korunmalı
    db2 = Database(db_file)
    vis = db2.visibility_defaults()
    sql_all, params_all = build_list_sql(ListQuery(node="watch_later", hide_watched_in_queue=False), vis)
    rows_all = db2.q(sql_all, params_all)
    assert [r["video_id"] for r in rows_all] == ["vid_C000003", "vid_A000001", "vid_B000002"]

    # Sonra İzle'de "İzlenenleri gizle" seçeneği aktifken izlenen (vid_A000001) gizlenmeli
    sql_unwatched, params_unwatched = build_list_sql(
        ListQuery(node="watch_later", hide_watched_in_queue=True), vis
    )
    rows_unwatched = db2.q(sql_unwatched, params_unwatched)
    assert [r["video_id"] for r in rows_unwatched] == ["vid_C000003", "vid_B000002"]

    # Kuyruktan çıkarma
    db2.set_watch_later("vid_C000003", False)
    assert db2.watch_later_ids() == ["vid_A000001", "vid_B000002"]


def test_duration_formatting_filters_and_background_backfill(tmp_path):
    # 1. Biçimlendirme kuralları: bilinmiyorsa asla 00:00 gösterme!
    assert format_duration(None) == ""
    assert format_duration(0) == ""
    assert format_duration(-5) == ""
    assert format_duration(45) == "0:45"
    assert format_duration(754) == "12:34"
    assert format_duration(3765) == "1:02:45"

    # 2. Küçük resim köşe rozetleri: CANLI, Planlandı, normal süre
    assert thumbnail_corner_overlay({"auto_type": "live", "live_state": "live", "duration": 1200})[0] == "CANLI"
    assert thumbnail_corner_overlay({"auto_type": "live", "live_state": "scheduled"})[0] == "Planlandı"
    assert thumbnail_corner_overlay({"auto_type": "live", "live_state": "completed", "duration": 3765})[0] == "1:02:45"
    assert thumbnail_corner_overlay({"auto_type": "video", "duration": None}) == ("", "", "")

    # 3. Çakışmayan süre filtreleri + mevcut filtrelerle birlikte çalışma
    db = Database(tmp_path / "durations.sqlite3")
    cid = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    db.insert_channel(cid, "Süre Kanalı", "@sure", None)
    db.set_channel_prefs(cid, 1, 1, 1)

    samples = [
        ("vid_u5_____", "Kısa Normal", "video", 180, 0),       # < 300s (under_5)
        ("vid_5_15___", "Orta Video", "video", 300, 0),        # 300..899s (5_to_15, sınır: 300)
        ("vid_15_30__", "Uzun Ders", "video", 900, 1),         # 900..1799s (15_to_30, sınır: 900, okundu)
        ("vid_o30____", "Belgesel", "video", 1800, 0),         # >= 1800s (over_30, sınır: 1800)
        ("vid_unkdur_", "Süresiz Video", "video", None, 0),    # unknown
    ]
    for i, (vid, title, vtype, dur, is_read) in enumerate(samples):
        db.upsert_video(video_id=vid, channel_id=cid, title=title, description='', published_at=1794300000 + i * 10, updated_at=None, thumb_url=None, duration=dur)
        db.save_classification(vid, classify({"feed_uulf": ("1", 1794305000)}, now=1794305000), None)
        if is_read:
            db.set_read([vid], True)
            db.set_starred(vid, True)

    vis = db.visibility_defaults()

    def query_ids(df: str, read_filter: str = "all", search: str = "") -> list[str]:
        sql, params = build_list_sql(
            ListQuery(
                node=f"channel:{cid}",
                type_filter="video",
                read_filter=read_filter,
                search=search,
                duration_filter=df,
            ),
            vis,
        )
        return [r["video_id"] for r in db.q(sql, params)]

    assert set(query_ids("any")) == {s[0] for s in samples}
    assert query_ids("under_5") == ["vid_u5_____"]
    assert query_ids("5_to_15") == ["vid_5_15___"]
    assert query_ids("15_to_30") == ["vid_15_30__"]
    assert query_ids("over_30") == ["vid_o30____"]
    assert query_ids("unknown") == ["vid_unkdur_"]

    # Aralıkların hiçbir videoda çakışmadığını doğrula
    buckets = [
        set(query_ids("under_5")),
        set(query_ids("5_to_15")),
        set(query_ids("15_to_30")),
        set(query_ids("over_30")),
        set(query_ids("unknown")),
    ]
    for i in range(len(buckets)):
        for j in range(i + 1, len(buckets)):
            assert buckets[i].isdisjoint(buckets[j])

    # Okunma ve arama filtresiyle birlikte çalışma
    assert query_ids("15_to_30", read_filter="unread") == []
    assert query_ids("15_to_30", read_filter="starred") == ["vid_15_30__"]
    assert query_ids("any", search="Belgesel") == ["vid_o30____"]

    # 4. Arka plan süre tamamlama (backfill_missing_durations)
    src = FakeSource()
    src.metas["vid_unkdur_"] = VideoMetadata(
        video_id="vid_unkdur_", ok=True, title="Süresiz Video",
        channel_id=cid, duration=45, media_type="video", availability="public",
    )
    updated_ids = backfill_missing_durations(db, src, limit=5)
    assert updated_ids == ["vid_unkdur_"]
    v_filled = db.video("vid_unkdur_")
    assert v_filled["duration"] == 45
    # 45 sn olmasına rağmen Shorts yapılmamalı, mevcut 'video' türü korunmalı!
    assert v_filled["auto_type"] == "video"


def test_newsflash_opml_import_summary_and_versioned_backup_merge(tmp_path):
    newsflash_opml = """<?xml version="1.0" encoding="UTF-8"?>
    <opml version="2.0">
      <body>
        <outline text="Teknoloji">
          <outline text="Google for Developers" type="rss"
                   xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw"/>
          <outline text="Google Dev Tekrar" type="rss"
                   xmlUrl="https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw"/>
          <outline text="Fedora Magazine" type="rss"
                   xmlUrl="https://fedoramagazine.org/feed/"/>
        </outline>
        <outline text="Uzay">
          <outline text="NASA" type="rss"
                   xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id=UCLA_DiR1FfKNvjuUpBHmylQ"/>
        </outline>
        <outline text="Hacker News" type="rss" xmlUrl="https://news.ycombinator.com/rss"/>
      </body>
    </opml>
    """
    report = parse_subscriptions_report_text(newsflash_opml, "newsflash.opml")
    assert len(report.candidates) == 2
    assert sum(c.channel_id == "UC_x5XG1OV2P6uZZ5FSM9Ttw" for c in report.candidates) == 1
    assert len(report.unsupported) == 2
    unsupported_titles = {u.title for u in report.unsupported}
    assert "Fedora Magazine" in unsupported_titles
    assert "Hacker News" in unsupported_titles
    assert all("YouTube dışı" in u.reason for u in report.unsupported)

    # Veritabanı 1 oluştur: kanal, grup, özel tür/bildirim tercihi, video durumları ve Sonra İzle sırası
    db1 = Database(tmp_path / "db1.sqlite3")
    src = FakeSource()
    s_imp = import_candidates_to_db(db1, src, report.candidates)
    assert s_imp.added == 2

    cid_g = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    cid_n = "UCLA_DiR1FfKNvjuUpBHmylQ"
    db1.set_channel_prefs(cid_g, 1, 1, 0)
    db1.set_channel_notify_prefs(cid_g, 1, 1, 0, 1)

    for idx, vid in enumerate(["vidBackup01", "vidBackup02"], start=1):
        db1.upsert_video(video_id=vid, channel_id=cid_g, title=f'Yedek Video {idx}', description='Açıklama', published_at=1794300000 + idx, updated_at=None, thumb_url=None, duration=600)
        db1.save_classification(vid, classify({"feed_uulf": ("1", 1794305000)}, now=1794305000), None)

    db1.set_read(["vidBackup01"], True)
    db1.set_starred("vidBackup01", True)
    db1.set_watched(["vidBackup01"], True)
    db1.set_user_type("vidBackup02", "short")
    db1.set_watch_later("vidBackup02", True)
    db1.set_watch_later("vidBackup01", True)
    db1.reorder_watch_later(["vidBackup02", "vidBackup01"])

    backup_path = tmp_path / "full_backup.json"
    export_backup_file(db1, backup_path)
    raw_backup = json.loads(backup_path.read_text(encoding="utf-8"))
    assert raw_backup["format"] == "ytrss-backup"
    assert raw_backup["version"] == BACKUP_VERSION

    # Hedef veritabanı (db2): içinde zaten 1 kanal ve kullanıcının yerel durumu olan 1 video var
    db2 = Database(tmp_path / "db2.sqlite3")
    db2.insert_channel(cid_g, "Google for Developers", "@GoogleDevelopers", None)
    db2.set_channel_prefs(cid_g, 1, 0, 1)  # mevcut kullanıcı tercihi ezilmemeli
    db2.upsert_video(video_id='vidBackup02', channel_id=cid_g, title='Yedek Video 2', description='', published_at=1794300002, updated_at=None, thumb_url=None, duration=600)
    db2.set_read(["vidBackup02"], True)  # db2'de okunmuş; yedekte okunmamış -> okunmuş kalmalı!

    res = restore_backup_file(db2, backup_path, merge=True)
    assert res.channels_added == 1       # yalnızca NASA eklendi
    assert res.channels_merged == 1      # Google Dev birleştirildi
    assert res.videos_added == 1         # vidBackup01 eklendi
    assert res.videos_merged == 1        # vidBackup02 birleştirildi

    # Tekrar geri yükleme hiçbir kaydı çoğaltmamalı
    res_again = restore_backup_file(db2, backup_path, merge=True)
    assert res_again.channels_added == 0
    assert res_again.videos_added == 0
    assert len(db2.channels()) == 2

    # Durumların korunduğunu doğrula
    ch_g2 = db2.channel(cid_g)
    assert (ch_g2["show_video"], ch_g2["show_short"], ch_g2["show_live"]) == (1, 0, 1)
    v1 = db2.video("vidBackup01")
    v2 = db2.video("vidBackup02")
    assert v1["is_read"] == 1 and v1["is_starred"] == 1 and v1["is_watched"] == 1
    assert v2["is_read"] == 1  # mevcut okundu durumu ezilmedi
    assert v2["user_type"] == "short"  # yedekteki elle tür düzeltmesi korundu
    assert db2.watch_later_ids() == ["vidBackup02", "vidBackup01"]


def test_notifications_initial_sync_no_storm_and_live_transition_and_manual_type_refresh(tmp_path, monkeypatch):
    # Bir yayının durum değişikliği, önceki üstveriden daha yeni bir gözlem olmalı.
    clock = [1794305000]
    monkeypatch.setattr("ytrss.sync.time.time", lambda: clock[0])
    db = Database(tmp_path / "notify.sqlite3")
    src = FakeSource()
    cid = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    db.insert_channel(cid, "Bildirim Kanalı", "@bildirim", None)
    db.set_channel_prefs(cid, 1, 1, 1)
    db.set_channel_notify_prefs(cid, 1, 1, 0, 1)  # Video ve Canlı bildirimleri açık, Shorts kapalı

    e_old = FeedEntry(video_id="vidInitial1", channel_id=cid, title="İlk Kurulum Videosu", published=1794300000)
    src.feeds[(cid, None)] = FeedResult(channel_id=cid, title="Kanal", entries=[e_old])
    src.feeds[(cid, "videos")] = FeedResult(channel_id=cid, title="Videos", entries=[e_old])
    src.feeds[(cid, "shorts")] = FeedResult(channel_id=cid, title="Shorts", entries=[])
    src.feeds[(cid, "streams")] = FeedResult(channel_id=cid, title="Streams", entries=[])

    # 1. İlk kanal eşitlemesinde geçmiş içerikler için bildirim yağmuru oluşmamalı!
    r_init = sync_channel(db, src, cid, force_tabs=True)
    assert r_init.ok is True
    assert r_init.notifications == []
    assert db.channel(cid)["initial_sync_done"] == 1
    assert db.video("vidInitial1")["notified_new"] == 1

    # 2. Sonraki yenilemede yeni 1 normal video, 1 Shorts ve 1 planlanmış canlı yayın gelsin
    e_new_vid = FeedEntry(video_id="vidNew00001", channel_id=cid, title="Yeni Normal Video", published=1794310000)
    e_new_sht = FeedEntry(video_id="shtNew00001", channel_id=cid, title="Yeni Shorts", published=1794310100)
    e_new_liv = FeedEntry(video_id="livSched001", channel_id=cid, title="Yaklaşan Yayın", published=1794310200)

    src.feeds[(cid, None)] = FeedResult(
        channel_id=cid, title="Kanal", entries=[e_old, e_new_vid, e_new_sht, e_new_liv]
    )
    src.feeds[(cid, "videos")] = FeedResult(channel_id=cid, title="Videos", entries=[e_old, e_new_vid])
    src.feeds[(cid, "shorts")] = FeedResult(channel_id=cid, title="Shorts", entries=[e_new_sht])
    src.feeds[(cid, "streams")] = FeedResult(channel_id=cid, title="Streams", entries=[e_new_liv])
    src.tabs[(cid, "streams")] = [
        TabEntry(video_id="livSched001", title="Yaklaşan Yayın", url=None,
                 duration=None, live_status="is_upcoming", position=0)
    ]
    src.metas["livSched001"] = VideoMetadata(
        video_id="livSched001", ok=True, title="Yaklaşan Yayın", channel_id=cid,
        live_status="is_upcoming", media_type="livestream", release_timestamp=1794350000,
        availability="public",
    )

    r_second = sync_channel(db, src, cid, force_tabs=True)
    notif_ids = [n.video_id for n in r_second.notifications]
    # Normal video ve canlı yayın için bildirim üretilmeli, Shorts (notify_short=0) için üretilmemeli!
    assert "vidNew00001" in notif_ids
    assert "livSched001" in notif_ids
    assert "shtNew00001" not in notif_ids

    # 3. Tekrar yenilendiğinde aynı içerikler için tekrar bildirim gönderilmemeli!
    r_third = sync_channel(db, src, cid, force_tabs=True)
    assert r_third.notifications == []

    # 4. Planlanmış yayın (scheduled) canlı (live) durumuna geçtiğinde bildirim üretilmeli
    clock[0] += 60
    src.tabs[(cid, "streams")] = [
        TabEntry(video_id="livSched001", title="Yaklaşan Yayın", url=None,
                 duration=None, live_status="is_live", position=0)
    ]
    src.metas["livSched001"] = VideoMetadata(
        video_id="livSched001", ok=True, title="Yaklaşan Yayın", channel_id=cid,
        live_status="is_live", media_type="livestream", release_timestamp=1794350000,
        availability="public",
    )
    # next_meta_check_at değerini geçmişe çek ki yeniden kontrol edilsin
    db.set_next_meta_check("livSched001", 1)
    r_live = sync_channel(db, src, cid, force_tabs=True)
    assert len(r_live.notifications) == 1
    assert r_live.notifications[0].video_id == "livSched001"
    assert r_live.notifications[0].kind == "live_started"

    # Bir sonraki yenilemede aynı canlı geçişi tekrar bildirilmemeli
    r_live_again = sync_channel(db, src, cid, force_tabs=True)
    assert r_live_again.notifications == []

    # 5. "Bilgileri yenile" (refresh_single_video_metadata) kullanıcının elle verdiği türü ezmemeli
    db.set_user_type("vidNew00001", "short")
    src.metas["vidNew00001"] = VideoMetadata(
        video_id="vidNew00001", ok=True, title="Yeni Normal Video", channel_id=cid,
        duration=950, live_status="not_live", media_type="video", availability="public",
    )
    refreshed = fetch_single_video_metadata(db, src, "vidNew00001")
    assert refreshed.type == ContentType.VIDEO
    v_refreshed = db.video("vidNew00001")
    assert v_refreshed["duration"] == 950
    assert v_refreshed["auto_type"] == "video"
    assert v_refreshed["user_type"] == "short"  # kullanıcının elle seçtiği tür korundu!



