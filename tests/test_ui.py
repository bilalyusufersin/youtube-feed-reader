"""PySide6 arayüz bileşenleri, sütun oranları, dikey küçük resim oranı ve filtre etkileşimi testleri."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from PySide6 import QtCore, QtGui, QtWidgets

from ytrss.db import Database
from ytrss.models import FeedEntry, FeedResult, TabEntry
from ytrss.sync import sync_channel
from ytrss.ui.main_window import MainWindow
from ytrss.ui.theme import apply_dark_theme
from ytrss.ui.thumb_cache import render_fitted_pixmap
from tests.test_core import FakeSource

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication([])
    apply_dark_theme(app)
    return app


def test_vertical_shorts_thumbnail_preserves_aspect_ratio(qapp):
    """9:16 dikey Shorts küçük resmi 16:9 kutuya çizildiğinde esnetilmemeli; yanlarda koyu boşluk kalmalı."""
    vertical_src = QtGui.QPixmap(90, 160)
    vertical_src.fill(QtGui.QColor("#ff0055"))

    fitted = render_fitted_pixmap(vertical_src, 112, 64, radius=0, bg_color=QtGui.QColor("#121418"))
    img = fitted.toImage()

    # Merkez piksel kırmızı (#ff0055), sol/sağ kenar pikselleri arka plan (#121418) olmalı
    center_color = img.pixelColor(112 // 2, 64 // 2)
    left_margin_color = img.pixelColor(6, 64 // 2)
    right_margin_color = img.pixelColor(112 - 6, 64 // 2)

    assert center_color.red() > 200
    assert left_margin_color == QtGui.QColor("#121418")
    assert right_margin_color == QtGui.QColor("#121418")


def test_main_window_three_columns_and_channel_filters(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("YTRSS_HOME", str(tmp_path / "xdg"))
    db = Database(tmp_path / "ui.sqlite3")
    src = FakeSource()

    cid = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    db.insert_channel(cid, "Google for Developers", "@GoogleDevelopers", None)
    entries = [
        FeedEntry(video_id='vidNormal01', channel_id=cid, title='Normal Video Başlığı', description='Açıklama 1', published=1794310000),
        FeedEntry(video_id='vidShort001', channel_id=cid, title='Dikey Shorts Başlığı', description='Açıklama 2', published=1794310100),
        FeedEntry(video_id='vidLive0001', channel_id=cid, title='Canlı Yayın Başlığı', description='Açıklama 3', published=1794310200),
    ]
    src.feeds[(cid, None)] = FeedResult(channel_id=cid, title="Google for Developers", entries=entries)
    src.feeds[(cid, "videos")] = FeedResult(channel_id=cid, title="Videos", entries=[entries[0]])
    src.feeds[(cid, "shorts")] = FeedResult(channel_id=cid, title="Shorts", entries=[entries[1]])
    src.feeds[(cid, "streams")] = FeedResult(channel_id=cid, title="Streams", entries=[entries[2]])
    src.tabs[(cid, "streams")] = [
        TabEntry(video_id="vidLive0001", title="Canlı Yayın Başlığı", url=None,
                 duration=None, live_status="is_live", position=0)
    ]
    sync_channel(db, src, cid, force_tabs=True)

    win = MainWindow(db, source=src, auto_start_sync=False)
    win.show()
    qapp.processEvents()

    # 1. Başlangıç sütun oranları yaklaşık %18 / %32 / %50 olmalı
    sizes = win.splitter.sizes()
    total = sum(sizes)
    r_left = sizes[0] / total
    r_mid = sizes[1] / total
    r_right = sizes[2] / total
    assert 0.14 <= r_left <= 0.22
    assert 0.28 <= r_mid <= 0.36
    assert 0.45 <= r_right <= 0.55

    # 2. Genel varsayılanlarda yalnızca Normal Video görünür (1 içerik)
    assert win.middle.model.video_count() == 1
    assert win.middle.model.all_video_ids() == ["vidNormal01"]

    # 3. Kanal için Shorts ve Canlı Yayın açıldığında 3 içerik de görünmeli
    db.set_channel_prefs(cid, 1, 1, 1)
    win.sidebar.reload()
    win.middle.reload()
    assert win.middle.model.video_count() == 3

    # 4. İlk videoyu seç -> sağ sütunda başlık ve sınıflandırma bilgisi görünmeli, okundu olmalı
    first_vid = win.middle.select_first_video()
    qapp.processEvents()
    assert first_vid == "vidLive0001"
    assert win.detail.current_video_id() == "vidLive0001"
    assert "Canlı Yayın Başlığı" in win.detail.title_label.text()
    assert db.video("vidLive0001")["is_read"] == 1

    # 5. Sağ sütundan elle tür değiştirme
    win._set_video_manual_type("vidLive0001", "video")
    assert db.video("vidLive0001")["user_type"] == "video"

    win.close()


def test_ui_watch_later_durations_shortcuts_technical_details_and_narrow_layout(
    qapp, tmp_path, monkeypatch
):
    monkeypatch.setenv("YTRSS_HOME", str(tmp_path / "xdg2"))
    db = Database(tmp_path / "ui2.sqlite3")
    src = FakeSource()

    cid = "UC_x5XG1OV2P6uZZ5FSM9Ttw"
    db.insert_channel(cid, "Google for Developers", "@GoogleDevelopers", None)
    db.set_channel_prefs(cid, 1, 1, 1)

    entries = [
        FeedEntry(video_id='vidNormal01', channel_id=cid, title='Kısa Normal Video', description='Açıklama 1', published=1794310000),
        FeedEntry(video_id='vidLong0002', channel_id=cid, title='Uzun Konferans Kaydı', description='Açıklama 2', published=1794310100),
        FeedEntry(video_id='vidNoDur003', channel_id=cid, title='Süresi Bilinmeyen Video', description='Açıklama 3', published=1794310200),
    ]
    src.feeds[(cid, None)] = FeedResult(channel_id=cid, title="Google for Developers", entries=entries)
    src.feeds[(cid, "videos")] = FeedResult(channel_id=cid, title="Videos", entries=entries)
    src.feeds[(cid, "shorts")] = FeedResult(channel_id=cid, title="Shorts", entries=[])
    src.feeds[(cid, "streams")] = FeedResult(channel_id=cid, title="Streams", entries=[])
    sync_channel(db, src, cid, force_tabs=True)
    db.mark_duration_checked("vidNormal01", 754)
    db.mark_duration_checked("vidLong0002", 3765)

    win = MainWindow(db, source=src, auto_start_sync=False)
    win.show()
    qapp.processEvents()

    # 1. Arayüz düzenlemeleri:
    # - "Bilgileri yenile" düğmesi adı
    # - "Teknik ayrıntılar" bölümü varsayılan olarak kapalı
    # - Sağ önizleme yüksekliği büyütülmüş (>= 250px)
    assert "Bilgileri yenile" in win.detail.btn_recheck.text()
    assert win.detail.btn_tech_toggle.isChecked() is False
    assert win.detail.tech_box.isHidden() is True
    assert win.detail.preview.height() >= 250
    assert win.detail.override_combo.isHidden() is False

    # 2. Videoyu seçmek "Okundu" yapar ama otomatik olarak "İzlendi" yapmamalı!
    first_vid = win.middle.select_first_video()
    qapp.processEvents()
    assert first_vid == "vidNoDur003"
    assert db.video("vidNoDur003")["is_read"] == 1
    assert db.video("vidNoDur003")["is_watched"] == 0
    # Süresi bilinmeyen videoda 00:00 gibi yanlış bir değer gösterilmemeli
    assert "00:00" not in win.detail.meta_info_label.text()
    assert "0:00" not in win.detail.meta_info_label.text()

    # 3. Klavye kısayolları: J ile sonraki videoya geç, W ile Sonra İzle'ye ekle, I ile İzlendi yap
    win.middle.list_view.setFocus()
    qapp.processEvents()
    QtTest_keyPress(qapp, win.middle.list_view, QtCore.Qt.Key.Key_J)
    qapp.processEvents()
    assert win.detail.current_video_id() == "vidLong0002"
    assert "1:02:45" in win.detail.meta_info_label.text()

    # W -> Sonra İzle'ye ekle, I -> İzlendi işaretle
    QtTest_keyPress(qapp, win.middle.list_view, QtCore.Qt.Key.Key_W)
    QtTest_keyPress(qapp, win.middle.list_view, QtCore.Qt.Key.Key_I)
    qapp.processEvents()
    assert db.video("vidLong0002")["watch_later"] == 1
    assert db.video("vidLong0002")["is_watched"] == 1

    # J ile 3. videoya (vidNormal01, 12:34) geç ve W ile onu da Sonra İzle'ye ekle
    QtTest_keyPress(qapp, win.middle.list_view, QtCore.Qt.Key.Key_J)
    QtTest_keyPress(qapp, win.middle.list_view, QtCore.Qt.Key.Key_W)
    qapp.processEvents()
    assert win.detail.current_video_id() == "vidNormal01"
    assert "12:34" in win.detail.meta_info_label.text()
    assert db.watch_later_ids() == ["vidLong0002", "vidNormal01"]

    # 4. Metin alanına (search_edit) yazarken tek tuş kısayolları (W, I, M, S, J, K) çalışmamalı!
    win.middle.search_edit.setFocus()
    qapp.processEvents()
    watched_before = db.video("vidNormal01")["is_watched"]
    QtTest_keyPress(qapp, win.middle.search_edit, QtCore.Qt.Key.Key_I)
    qapp.processEvents()
    assert db.video("vidNormal01")["is_watched"] == watched_before

    # 5. Sol menüden "Sonra İzle" görünümüne geç ve "İzlenenleri gizle" seçeneğini doğrula
    win._on_node_selected("watch_later")
    qapp.processEvents()
    assert win.middle.chk_hide_watched.isHidden() is False
    assert win.middle.model.all_video_ids() == ["vidLong0002", "vidNormal01"]

    win.middle.chk_hide_watched.setChecked(True)
    qapp.processEvents()
    assert win.middle.model.all_video_ids() == ["vidNormal01"]
    win.middle.chk_hide_watched.setChecked(False)
    qapp.processEvents()

    # 6. Süre filtresinin (duration_combo) arayüzde çalışması
    win._on_node_selected("all")
    idx_over_30 = win.middle.duration_combo.findData("over_30")
    win.middle.duration_combo.setCurrentIndex(idx_over_30)
    qapp.processEvents()
    assert win.middle.model.all_video_ids() == ["vidLong0002"]

    idx_any = win.middle.duration_combo.findData("any")
    win.middle.duration_combo.setCurrentIndex(idx_any)
    qapp.processEvents()

    # 7. Bildirim tıklamasıyla ilgili kaydın seçilmesi (select_and_show_video)
    win.select_and_show_video("vidNormal01")
    qapp.processEvents()
    assert win.detail.current_video_id() == "vidNormal01"

    # 8. Dar pencere boyutunda (980x660) üç sütunun da görünür ve kullanılabilir kalması
    win.resize(980, 660)
    qapp.processEvents()
    sizes_narrow = win.splitter.sizes()
    assert all(s >= 180 for s in sizes_narrow)

    win.close()


def QtTest_keyPress(qapp, widget: QtWidgets.QWidget, key: QtCore.Qt.Key) -> None:
    ev = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, key, QtCore.Qt.KeyboardModifier.NoModifier)
    qapp.sendEvent(widget, ev)


