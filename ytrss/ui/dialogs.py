"""Kanal ekleme, kanal bazlı görünürlük/bildirim ayarları, genel ayarlar,
toplu kanal aktarımı / sürümlü yedekleme ve klavye kısayolları yardım pencereleri."""
from __future__ import annotations

import time
from PySide6 import QtCore, QtGui, QtWidgets

from ..db import Database
from ..subscriptions import (
    BACKUP_VERSION,
    SubscriptionCandidate,
    SubscriptionParseReport,
    UnsupportedEntry,
    export_backup_file,
    export_subscriptions_opml,
    fetch_subscriptions_from_browser,
    parse_subscriptions_report_file,
    parse_subscriptions_report_text,
    restore_backup_file,
)
from ..youtube.urls import channel_feed_url
from .theme import TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY, WARN_AMBER


def format_timestamp_tr(ts: int | None) -> str:
    if not ts:
        return "—"
    lt = time.localtime(ts)
    months = [
        "", "Oca", "Şub", "Mar", "Nis", "May", "Haz",
        "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara",
    ]
    return f"{lt.tm_mday:02d} {months[lt.tm_mon]} {lt.tm_year} {lt.tm_hour:02d}:{lt.tm_min:02d}"


def _make_tri_combo(
    default_is_on: bool,
    current_val: int | None,
    *,
    prefix: str = "Bu kanal için",
) -> QtWidgets.QComboBox:
    cb = QtWidgets.QComboBox()
    def_txt = "Açık" if default_is_on else "Kapalı"
    cb.addItem(f"Genel varsayılanı izle ({def_txt})", None)
    cb.addItem(f"{prefix} Açık", 1)
    cb.addItem(f"{prefix} Kapalı", 0)
    if current_val is None:
        cb.setCurrentIndex(0)
    elif int(current_val) == 1:
        cb.setCurrentIndex(1)
    else:
        cb.setCurrentIndex(2)
    return cb


class AddChannelDialog(QtWidgets.QDialog):
    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("YouTube Kanalı Ekle")
        self.setMinimumWidth(480)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QtWidgets.QLabel(
            "Kanalın <b>@kullanıcı</b> adresini, <b>/channel/UC…</b> bağlantısını veya "
            "<b>RSS akış bağlantısını</b> girin. Adres kalıcı <code>channel_id</code> "
            "değerine çözümlenerek kaydedilir."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QtWidgets.QFormLayout()
        form.setSpacing(10)

        self.url_edit = QtWidgets.QLineEdit()
        self.url_edit.setPlaceholderText(
            "Örn: @GoogleDevelopers, https://www.youtube.com/channel/UC… veya RSS adresi"
        )
        form.addRow("Kanal adresi:", self.url_edit)

        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.addItem("Grup yok", None)
        for g in self.db.groups():
            self.group_combo.addItem(g["name"], int(g["id"]))
        form.addRow("Kanal grubu:", self.group_combo)

        vis = self.db.visibility_defaults()
        self.cb_video = _make_tri_combo(bool(vis.video), None)
        self.cb_short = _make_tri_combo(bool(vis.short), None)
        self.cb_live = _make_tri_combo(bool(vis.live), None)
        form.addRow("Normal videolar:", self.cb_video)
        form.addRow("Shorts içerikleri:", self.cb_short)
        form.addRow("Canlı yayınlar:", self.cb_live)

        layout.addLayout(form)

        note = QtWidgets.QLabel(
            "Not: YouTube RSS akışları yalnızca son içerik penceresini (~15 kayıt) sunar; "
            "kanalın tüm geçmiş arşivi indirilmez ve ilk eklemede geçmiş içerikler için bildirim gönderilmez."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #9da5b4; font-size: 12px;")
        layout.addWidget(note)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setText("Kanalı Ekle")
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("İptal")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def values(self) -> tuple[str, int | None, tuple[int | None, int | None, int | None]]:
        return (
            self.url_edit.text().strip(),
            self.group_combo.currentData(),
            (
                self.cb_video.currentData(),
                self.cb_short.currentData(),
                self.cb_live.currentData(),
            ),
        )


class ChannelSettingsDialog(QtWidgets.QDialog):
    def __init__(self, db: Database, channel_id: str, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.channel_id = channel_id
        self.ch = db.channel(channel_id)
        self.setWindowTitle("Kanal Ayarları — Görünürlük ve Bildirimler")
        self.setMinimumWidth(540)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        info_box = QtWidgets.QFrame()
        info_box.setObjectName("InfoBox")
        info_layout = QtWidgets.QFormLayout(info_box)
        info_layout.setContentsMargins(12, 10, 12, 10)
        info_layout.setSpacing(6)

        cid_lbl = QtWidgets.QLabel(f"<code>{channel_id}</code>")
        cid_lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        info_layout.addRow("Kalıcı kanal kimliği:", cid_lbl)

        if self.ch["handle"]:
            h_lbl = QtWidgets.QLabel(self.ch["handle"])
            h_lbl.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
            info_layout.addRow("Kullanıcı adı:", h_lbl)

        rss_row = QtWidgets.QHBoxLayout()
        rss_url = channel_feed_url(channel_id)
        rss_edit = QtWidgets.QLineEdit(rss_url)
        rss_edit.setReadOnly(True)
        copy_btn = QtWidgets.QPushButton("Kopyala")
        copy_btn.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(rss_url))
        rss_row.addWidget(rss_edit, 1)
        rss_row.addWidget(copy_btn)
        info_layout.addRow("RSS akışı:", rss_row)

        status_txt = format_timestamp_tr(self.ch["last_success_at"])
        if self.ch["last_error"]:
            status_txt += f"\nSon hata ({format_timestamp_tr(self.ch['last_error_at'])}): {self.ch['last_error']}"
        elif self.ch["last_warning"]:
            status_txt += f"\nUyarı: {self.ch['last_warning']}"
        st_lbl = QtWidgets.QLabel(status_txt)
        st_lbl.setWordWrap(True)
        if self.ch["last_error"]:
            st_lbl.setStyleSheet("color: #ff7070;")
        elif self.ch["last_warning"]:
            st_lbl.setStyleSheet("color: #f59e0b;")
        info_layout.addRow("Son başarılı eşitleme:", st_lbl)

        layout.addWidget(info_box)

        form = QtWidgets.QFormLayout()
        form.setSpacing(9)

        self.title_edit = QtWidgets.QLineEdit(self.ch["title"])
        form.addRow("Kanal adı:", self.title_edit)

        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.addItem("Grup yok", None)
        cur_gid = self.ch["group_id"]
        for i, g in enumerate(self.db.groups(), start=1):
            self.group_combo.addItem(g["name"], int(g["id"]))
            if cur_gid == g["id"]:
                self.group_combo.setCurrentIndex(i)
        form.addRow("Kanal grubu:", self.group_combo)

        vis = self.db.visibility_defaults()
        self.cb_video = _make_tri_combo(bool(vis.video), self.ch["show_video"])
        self.cb_short = _make_tri_combo(bool(vis.short), self.ch["show_short"])
        self.cb_live = _make_tri_combo(bool(vis.live), self.ch["show_live"])

        form.addRow("Normal video görünürlüğü:", self.cb_video)
        form.addRow("Shorts görünürlüğü:", self.cb_short)
        form.addRow("Canlı yayın görünürlüğü:", self.cb_live)

        layout.addLayout(form)

        # Kanal bazlı bildirim tercihleri
        notif_box = QtWidgets.QFrame()
        notif_box.setObjectName("InfoBox")
        n_form = QtWidgets.QFormLayout(notif_box)
        n_form.setContentsMargins(12, 10, 12, 10)
        n_form.setSpacing(8)

        n_hdr = QtWidgets.QLabel("<b>Bu Kanal İçin Masaüstü Bildirimleri</b>")
        n_form.addRow(n_hdr)

        self.cb_notify_enabled = _make_tri_combo(
            bool(self.db.get_setting("notifications_enabled")),
            self.ch["notify_enabled"],
            prefix="Bildirimler",
        )
        self.cb_notify_video = _make_tri_combo(
            bool(self.db.get_setting("notify_video")),
            self.ch["notify_video"],
            prefix="Bildirim",
        )
        self.cb_notify_short = _make_tri_combo(
            bool(self.db.get_setting("notify_short")),
            self.ch["notify_short"],
            prefix="Bildirim",
        )
        self.cb_notify_live = _make_tri_combo(
            bool(self.db.get_setting("notify_live")),
            self.ch["notify_live"],
            prefix="Bildirim",
        )
        n_form.addRow("Kanal bildirim durumu:", self.cb_notify_enabled)
        n_form.addRow("Normal video bildirimi:", self.cb_notify_video)
        n_form.addRow("Shorts bildirimi:", self.cb_notify_short)
        n_form.addRow("Canlı yayın bildirimi:", self.cb_notify_live)
        layout.addWidget(notif_box)

        hint = QtWidgets.QLabel(
            "Gizlenen içerik türleri veritabanından silinmez ve otomatik olarak okunmuş "
            "sayılmaz. Seçeneği tekrar açtığınızda içerikler ve okunmamış sayaçları geri gelir."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9da5b4; font-size: 12px;")
        layout.addWidget(hint)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText("Kaydet")
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("İptal")
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _save(self) -> None:
        new_title = self.title_edit.text().strip()
        if new_title:
            self.db.rename_channel(self.channel_id, new_title)
        self.db.set_channel_group(self.channel_id, self.group_combo.currentData())
        self.db.set_channel_prefs(
            self.channel_id,
            self.cb_video.currentData(),
            self.cb_short.currentData(),
            self.cb_live.currentData(),
        )
        self.db.set_channel_notify_prefs(
            self.channel_id,
            self.cb_notify_enabled.currentData(),
            self.cb_notify_video.currentData(),
            self.cb_notify_short.currentData(),
            self.cb_notify_live.currentData(),
        )
        self.accept()


class GlobalSettingsDialog(QtWidgets.QDialog):
    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Genel Ayarlar, Bildirimler ve Sistem Tepsisi")
        self.setMinimumWidth(520)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        # 1. Görünürlük varsayılanları
        vis_box = QtWidgets.QFrame()
        vis_box.setObjectName("InfoBox")
        v_lay = QtWidgets.QVBoxLayout(vis_box)
        v_lay.setContentsMargins(12, 10, 12, 10)
        v_lay.setSpacing(6)

        v_lay.addWidget(QtWidgets.QLabel("<b>Genel İçerik Türü Görünürlük Varsayılanları</b>"))
        sub = QtWidgets.QLabel(
            "Özel tercih belirlenmemiş kanallar bu genel varsayılanları takip eder. "
            "Kanal ayarlarından açık veya kapalı olarak sabitlenen seçenekler genel "
            "varsayılandan etkilenmez."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #9da5b4; font-size: 12px;")
        v_lay.addWidget(sub)

        self.chk_video = QtWidgets.QCheckBox("Normal videoları göster (önerilen: açık)")
        self.chk_video.setChecked(bool(db.get_setting("show_video")))
        self.chk_short = QtWidgets.QCheckBox("Shorts içeriklerini göster (varsayılan: kapalı)")
        self.chk_short.setChecked(bool(db.get_setting("show_short")))
        self.chk_live = QtWidgets.QCheckBox("Canlı yayınları göster (varsayılan: kapalı)")
        self.chk_live.setChecked(bool(db.get_setting("show_live")))
        self.chk_unknown = QtWidgets.QCheckBox(
            "Türü henüz belirlenememiş içerikleri ana listelerde de göster"
        )
        self.chk_unknown.setChecked(bool(db.get_setting("show_unknown")))

        v_lay.addWidget(self.chk_video)
        v_lay.addWidget(self.chk_short)
        v_lay.addWidget(self.chk_live)
        v_lay.addWidget(self.chk_unknown)
        layout.addWidget(vis_box)

        # 2. Bildirimler ve Sistem Tepsisi
        notif_box = QtWidgets.QFrame()
        notif_box.setObjectName("InfoBox")
        n_lay = QtWidgets.QVBoxLayout(notif_box)
        n_lay.setContentsMargins(12, 10, 12, 10)
        n_lay.setSpacing(6)

        n_lay.addWidget(QtWidgets.QLabel("<b>Bildirimler ve Sistem Tepsisi</b>"))

        self.chk_notify_enabled = QtWidgets.QCheckBox("Masaüstü bildirimlerini etkinleştir")
        self.chk_notify_enabled.setChecked(bool(db.get_setting("notifications_enabled")))
        self.chk_notify_video = QtWidgets.QCheckBox("Yeni normal videolar için bildirim gönder")
        self.chk_notify_video.setChecked(bool(db.get_setting("notify_video")))
        self.chk_notify_short = QtWidgets.QCheckBox("Yeni Shorts içerikleri için bildirim gönder")
        self.chk_notify_short.setChecked(bool(db.get_setting("notify_short")))
        self.chk_notify_live = QtWidgets.QCheckBox("Yeni canlı yayınlar için bildirim gönder")
        self.chk_notify_live.setChecked(bool(db.get_setting("notify_live")))
        self.chk_notify_live_trans = QtWidgets.QCheckBox(
            "Doğrulanan bir yayın 'Planlandı' durumundan 'Canlı' durumuna geçtiğinde bildirim gönder"
        )
        self.chk_notify_live_trans.setChecked(bool(db.get_setting("notify_live_transition")))

        self.chk_tray_enabled = QtWidgets.QCheckBox("Sistem tepsisinde (System Tray) uygulama simgesi göster")
        self.chk_tray_enabled.setChecked(bool(db.get_setting("tray_enabled")))
        self.chk_close_to_tray = QtWidgets.QCheckBox(
            "Pencere kapatılınca tepside çalışmaya devam et (varsayılan: kapalı)"
        )
        self.chk_close_to_tray.setChecked(bool(db.get_setting("close_to_tray")))

        n_lay.addWidget(self.chk_notify_enabled)
        n_lay.addWidget(self.chk_notify_video)
        n_lay.addWidget(self.chk_notify_short)
        n_lay.addWidget(self.chk_notify_live)
        n_lay.addWidget(self.chk_notify_live_trans)
        n_lay.addWidget(self.chk_tray_enabled)
        n_lay.addWidget(self.chk_close_to_tray)
        layout.addWidget(notif_box)

        # 3. Yenileme ve önbellek
        form = QtWidgets.QFormLayout()
        form.setSpacing(10)

        self.spin_refresh = QtWidgets.QSpinBox()
        self.spin_refresh.setRange(0, 720)
        self.spin_refresh.setSingleStep(5)
        self.spin_refresh.setSpecialValueText("Kapalı (yalnızca elle)")
        self.spin_refresh.setSuffix(" dakika")
        self.spin_refresh.setValue(int(db.get_setting("auto_refresh_minutes") or 0))
        form.addRow("Otomatik yenileme sıklığı:", self.spin_refresh)

        self.chk_start_refresh = QtWidgets.QCheckBox("Uygulama açıldığında arka planda yenile")
        self.chk_start_refresh.setChecked(bool(db.get_setting("refresh_on_start")))
        form.addRow("", self.chk_start_refresh)

        self.chk_auto_read = QtWidgets.QCheckBox(
            "İçerik seçildiğinde otomatik okundu işaretle ('İzlendi' durumunu etkilemez)"
        )
        self.chk_auto_read.setChecked(bool(db.get_setting("mark_read_on_select")))
        form.addRow("", self.chk_auto_read)

        self.spin_cache = QtWidgets.QSpinBox()
        self.spin_cache.setRange(20, 2000)
        self.spin_cache.setSingleStep(20)
        self.spin_cache.setSuffix(" MB")
        self.spin_cache.setValue(int(db.get_setting("thumb_cache_mb") or 200))
        form.addRow("Küçük resim önbellek sınırı:", self.spin_cache)

        layout.addLayout(form)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setText("Kaydet")
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("İptal")
        btns.accepted.connect(self._save)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _save(self) -> None:
        self.db.set_setting("show_video", int(self.chk_video.isChecked()))
        self.db.set_setting("show_short", int(self.chk_short.isChecked()))
        self.db.set_setting("show_live", int(self.chk_live.isChecked()))
        self.db.set_setting("show_unknown", int(self.chk_unknown.isChecked()))
        self.db.set_setting("notifications_enabled", int(self.chk_notify_enabled.isChecked()))
        self.db.set_setting("notify_video", int(self.chk_notify_video.isChecked()))
        self.db.set_setting("notify_short", int(self.chk_notify_short.isChecked()))
        self.db.set_setting("notify_live", int(self.chk_notify_live.isChecked()))
        self.db.set_setting("notify_live_transition", int(self.chk_notify_live_trans.isChecked()))
        self.db.set_setting("tray_enabled", int(self.chk_tray_enabled.isChecked()))
        self.db.set_setting("close_to_tray", int(self.chk_close_to_tray.isChecked()))
        self.db.set_setting("auto_refresh_minutes", int(self.spin_refresh.value()))
        self.db.set_setting("refresh_on_start", int(self.chk_start_refresh.isChecked()))
        self.db.set_setting("mark_read_on_select", int(self.chk_auto_read.isChecked()))
        self.db.set_setting("thumb_cache_mb", int(self.spin_cache.value()))
        self.accept()


class _BrowserScanWorker(QtCore.QThread):
    succeeded = QtCore.Signal(object)  # list[SubscriptionCandidate]
    failed = QtCore.Signal(str)

    def __init__(self, browser: str, parent=None) -> None:
        super().__init__(parent)
        self.browser = browser

    def run(self) -> None:
        try:
            items = fetch_subscriptions_from_browser(self.browser)
            self.succeeded.emit(items)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class ImportSubscriptionsDialog(QtWidgets.QDialog):
    """Toplu kanal aktarımı (NewsFlash OPML, Google Takeout CSV/JSON, Tarayıcı, Metin),
    OPML dışa aktarma ve sürümlü tam yedekleme / geri yükleme penceresi."""

    backupRestored = QtCore.Signal(object)  # BackupRestoreSummary

    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self._candidates: list[SubscriptionCandidate] = []
        self._unsupported: list[UnsupportedEntry] = []
        self._scan_worker: _BrowserScanWorker | None = None

        self.setWindowTitle("Toplu Kanal Aktarımı, OPML ve Sürümlü Yedekleme")
        self.setMinimumSize(720, 620)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(10)

        # Kaynak sekmeleri
        self.tabs = QtWidgets.QTabWidget()

        # Sekme 1: Dosyadan Aktar (NewsFlash OPML/XML, Google Takeout CSV/JSON)
        tab_file = QtWidgets.QWidget()
        tf_lay = QtWidgets.QVBoxLayout(tab_file)
        tf_lay.setContentsMargins(12, 12, 12, 12)
        tf_lay.setSpacing(8)

        tf_desc = QtWidgets.QLabel(
            "<b>NewsFlash</b>'tan dışa aktarılan <b>OPML / XML</b> dosyasını veya "
            "<b>Google Takeout</b> <code>abonelikler.csv</code> / <code>subscriptions.json</code> "
            "dosyasını seçin. OPML içindeki kanal grupları korunur; YouTube dışındaki kaynaklar "
            "desteklenmeyen olarak raporlanır."
        )
        tf_desc.setWordWrap(True)
        tf_lay.addWidget(tf_desc)

        tf_btns = QtWidgets.QHBoxLayout()
        self.btn_pick_file = QtWidgets.QPushButton("Dosya Seç (OPML / CSV / JSON)…")
        self.btn_pick_file.setObjectName("PrimaryButton")
        self.btn_pick_file.clicked.connect(self._pick_file)
        tf_btns.addWidget(self.btn_pick_file)

        self.btn_export_opml = QtWidgets.QPushButton("Kanalları OPML Olarak Dışa Aktar…")
        self.btn_export_opml.clicked.connect(self._export_opml)
        tf_btns.addWidget(self.btn_export_opml)

        self.btn_open_takeout = QtWidgets.QPushButton("Google Takeout'u Aç")
        self.btn_open_takeout.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(
                QtCore.QUrl("https://takeout.google.com/takeout/custom/youtube")
            )
        )
        tf_btns.addWidget(self.btn_open_takeout)
        tf_btns.addStretch(1)
        tf_lay.addLayout(tf_btns)

        self.file_status_lbl = QtWidgets.QLabel("Henüz dosya seçilmedi.")
        self.file_status_lbl.setStyleSheet("color: #9da5b4; font-size: 12px;")
        tf_lay.addWidget(self.file_status_lbl)
        self.tabs.addTab(tab_file, "1. OPML / Takeout Dosyası")

        # Sekme 2: Sürümlü Tam Yedekleme ve Geri Yükleme
        tab_backup = QtWidgets.QWidget()
        tbk_lay = QtWidgets.QVBoxLayout(tab_backup)
        tbk_lay.setContentsMargins(12, 12, 12, 12)
        tbk_lay.setSpacing(8)

        tbk_desc = QtWidgets.QLabel(
            f"<b>Sürümlü Tam Yedekleme (v{BACKUP_VERSION}):</b> Kanalları, grupları, kanal bazındaki "
            "tür ve bildirim tercihlerini, kayıtlı içerikleri, <b>okundu / yıldızlı / izlendi</b> "
            "durumlarını ve <b>Sonra İzle sırasını</b> tek bir JSON yedek dosyasına kaydeder.<br>"
            "Geri yükleme varsayılan olarak <b>birleştirme</b> kipinde çalışır; mevcut kayıtları "
            "çoğaltmaz ve kullanıcı durumlarını sessizce ezmez."
        )
        tbk_desc.setWordWrap(True)
        tbk_lay.addWidget(tbk_desc)

        tbk_btns = QtWidgets.QHBoxLayout()
        self.btn_export_backup = QtWidgets.QPushButton("Tam Yedek Oluştur (.json)…")
        self.btn_export_backup.setObjectName("PrimaryButton")
        self.btn_export_backup.clicked.connect(self._export_full_backup)
        tbk_btns.addWidget(self.btn_export_backup)

        self.btn_restore_backup = QtWidgets.QPushButton("Yedekten Geri Yükle (Birleştir)…")
        self.btn_restore_backup.clicked.connect(self._restore_full_backup)
        tbk_btns.addWidget(self.btn_restore_backup)
        tbk_btns.addStretch(1)
        tbk_lay.addLayout(tbk_btns)

        self.backup_status_lbl = QtWidgets.QLabel("Tam yedekleme veya geri yükleme için yukarıdaki düğmeleri kullanın.")
        self.backup_status_lbl.setWordWrap(True)
        self.backup_status_lbl.setStyleSheet("color: #9da5b4; font-size: 12px;")
        tbk_lay.addWidget(self.backup_status_lbl)
        self.tabs.addTab(tab_backup, "2. Tam Yedekleme / Geri Yükleme")

        # Sekme 3: Tarayıcı Oturumundan Doğrudan Çek
        tab_browser = QtWidgets.QWidget()
        tb_lay = QtWidgets.QVBoxLayout(tab_browser)
        tb_lay.setContentsMargins(12, 12, 12, 12)
        tb_lay.setSpacing(8)

        tb_desc = QtWidgets.QLabel(
            "Bilgisayarınızdaki web tarayıcısında YouTube oturumunuz açıksa, API anahtarı veya "
            "şifre girmeden doğrudan <code>youtube.com/feed/channels</code> listenizi çekebilirsiniz."
        )
        tb_desc.setWordWrap(True)
        tb_lay.addWidget(tb_desc)

        tb_row = QtWidgets.QHBoxLayout()
        tb_row.addWidget(QtWidgets.QLabel("Tarayıcı:"))
        self.browser_combo = QtWidgets.QComboBox()
        for b_id, b_label in (
            ("firefox", "Firefox"),
            ("chrome", "Google Chrome"),
            ("chromium", "Chromium"),
            ("brave", "Brave"),
            ("vivaldi", "Vivaldi"),
            ("edge", "Microsoft Edge"),
        ):
            self.browser_combo.addItem(b_label, b_id)
        tb_row.addWidget(self.browser_combo)

        self.btn_scan_browser = QtWidgets.QPushButton("Tarayıcıdaki Abonelikleri Tara")
        self.btn_scan_browser.setObjectName("PrimaryButton")
        self.btn_scan_browser.clicked.connect(self._scan_browser)
        tb_row.addWidget(self.btn_scan_browser)
        tb_row.addStretch(1)
        tb_lay.addLayout(tb_row)

        self.browser_status_lbl = QtWidgets.QLabel(
            "Tarayıcı seçip 'Tarayıcıdaki Abonelikleri Tara' düğmesine basın."
        )
        self.browser_status_lbl.setWordWrap(True)
        self.browser_status_lbl.setStyleSheet("color: #9da5b4; font-size: 12px;")
        tb_lay.addWidget(self.browser_status_lbl)
        self.tabs.addTab(tab_browser, "3. Tarayıcıdan Çek")

        # Sekme 4: Metin / Çoklu Bağlantı Yapıştır
        tab_paste = QtWidgets.QWidget()
        tp_lay = QtWidgets.QVBoxLayout(tab_paste)
        tp_lay.setContentsMargins(12, 10, 12, 10)
        tp_lay.setSpacing(6)

        self.paste_edit = QtWidgets.QPlainTextEdit()
        self.paste_edit.setPlaceholderText(
            "Her satıra bir kanal gelecek şekilde @kullanıcı, https://www.youtube.com/channel/UC… "
            "veya RSS adreslerini (ya da kopyalanmış CSV/OPML metnini) buraya yapıştırın…"
        )
        self.paste_edit.setMaximumHeight(95)
        tp_lay.addWidget(self.paste_edit)

        btn_parse_paste = QtWidgets.QPushButton("Yapıştırılan Metni Çözümle ve Özetle")
        btn_parse_paste.clicked.connect(self._parse_pasted)
        tp_lay.addWidget(btn_parse_paste, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
        self.tabs.addTab(tab_paste, "4. Metin / Çoklu Bağlantı")

        layout.addWidget(self.tabs)

        # Aktarım öncesi özet şeridi (Eklenecek / Zaten Bulunan / Desteklenmeyen)
        self.summary_frame = QtWidgets.QFrame()
        self.summary_frame.setObjectName("BannerBox")
        sf_lay = QtWidgets.QHBoxLayout(self.summary_frame)
        sf_lay.setContentsMargins(12, 8, 12, 8)
        self.summary_label = QtWidgets.QLabel(
            "<b>Aktarım Özeti:</b> Eklenecek: <b>0</b>  ·  "
            "Zaten bulunan: <b>0</b>  ·  Desteklenmeyen (YouTube dışı / geçersiz): <b>0</b>"
        )
        self.summary_label.setWordWrap(True)
        sf_lay.addWidget(self.summary_label, 1)
        layout.addWidget(self.summary_frame)

        # Önizleme ve kanal seçimi listesi
        preview_hdr = QtWidgets.QHBoxLayout()
        self.preview_count_lbl = QtWidgets.QLabel("<b>Aday Kayıtlar (0)</b>")
        preview_hdr.addWidget(self.preview_count_lbl, 1)

        btn_sel_all = QtWidgets.QPushButton("Tümünü Seç")
        btn_sel_all.clicked.connect(lambda: self._set_all_checked(True))
        preview_hdr.addWidget(btn_sel_all)

        btn_sel_none = QtWidgets.QPushButton("Seçimi Kaldır")
        btn_sel_none.clicked.connect(lambda: self._set_all_checked(False))
        preview_hdr.addWidget(btn_sel_none)
        layout.addLayout(preview_hdr)

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setAlternatingRowColors(True)
        layout.addWidget(self.list_widget, 1)

        # Grup ve görünürlük ayarları
        opts_box = QtWidgets.QFrame()
        opts_box.setObjectName("InfoBox")
        opts_form = QtWidgets.QFormLayout(opts_box)
        opts_form.setContentsMargins(12, 8, 12, 8)
        opts_form.setSpacing(6)

        self.group_combo = QtWidgets.QComboBox()
        self.group_combo.addItem("Grup yok (OPML klasörü varsa koru)", None)
        for g in self.db.groups():
            self.group_combo.addItem(g["name"], int(g["id"]))
        opts_form.addRow("Hedef kanal grubu:", self.group_combo)

        vis = self.db.visibility_defaults()
        t_row = QtWidgets.QHBoxLayout()
        self.cb_video = _make_tri_combo(bool(vis.video), None)
        self.cb_short = _make_tri_combo(bool(vis.short), None)
        self.cb_live = _make_tri_combo(bool(vis.live), None)
        t_row.addWidget(QtWidgets.QLabel("Video:"))
        t_row.addWidget(self.cb_video, 1)
        t_row.addWidget(QtWidgets.QLabel("Shorts:"))
        t_row.addWidget(self.cb_short, 1)
        t_row.addWidget(QtWidgets.QLabel("Canlı:"))
        t_row.addWidget(self.cb_live, 1)
        opts_form.addRow("Başlangıç tür filtresi:", t_row)
        layout.addWidget(opts_box)

        # Onay düğmeleri
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        self.btn_ok = btns.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self.btn_ok.setText("Seçili Kanalları İçe Aktar")
        self.btn_ok.setObjectName("PrimaryButton")
        self.btn_ok.setEnabled(False)
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setText("Kapat")
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def load_report(self, report: SubscriptionParseReport) -> None:
        self._candidates = report.candidates
        self._unsupported = report.unsupported
        existing_cids = {r["channel_id"] for r in self.db.channels()}
        existing_handles = {
            (r["handle"] or "").lower() for r in self.db.channels() if r["handle"]
        }
        self.list_widget.clear()

        new_count = 0
        existing_count = 0

        for idx, c in enumerate(self._candidates):
            title = c.title or c.handle or c.raw_input or c.channel_id or "?"
            cid_txt = f" ({c.channel_id})" if c.channel_id and c.channel_id != title else ""
            grp_txt = f" [{c.group_name}]" if c.group_name else ""
            already = bool(
                (c.channel_id and c.channel_id in existing_cids)
                or (c.handle and c.handle.lower() in existing_handles)
            )
            if already:
                existing_count += 1
                suffix = " — [Zaten bulunan]"
            else:
                new_count += 1
                suffix = " — [Eklenecek]"

            item = QtWidgets.QListWidgetItem(f"{title}{cid_txt}{grp_txt}{suffix}")
            item.setData(QtCore.Qt.ItemDataRole.UserRole, idx)
            item.setFlags(
                QtCore.Qt.ItemFlag.ItemIsEnabled
                | QtCore.Qt.ItemFlag.ItemIsSelectable
                | QtCore.Qt.ItemFlag.ItemIsUserCheckable
            )
            item.setCheckState(
                QtCore.Qt.CheckState.Unchecked if already else QtCore.Qt.CheckState.Checked
            )
            self.list_widget.addItem(item)

        for u in self._unsupported:
            u_item = QtWidgets.QListWidgetItem(
                f"⚠ [Desteklenmeyen — {u.reason}] {u.title} ({u.url})"
            )
            u_item.setData(QtCore.Qt.ItemDataRole.UserRole, None)
            u_item.setFlags(QtCore.Qt.ItemFlag.ItemIsEnabled)
            u_item.setForeground(QtGui.QColor(WARN_AMBER))
            self.list_widget.addItem(u_item)

        unsup_count = len(self._unsupported)
        self.summary_label.setText(
            f"<b>Aktarım Özeti:</b> Eklenecek: <b style='color:#6ee7b7'>{new_count}</b>  ·  "
            f"Zaten bulunan: <b style='color:#93c5fd'>{existing_count}</b>  ·  "
            f"Desteklenmeyen (YouTube dışı / geçersiz): <b style='color:#f59e0b'>{unsup_count}</b>"
        )
        self.preview_count_lbl.setText(
            f"<b>Bulunan Kayıtlar ({len(self._candidates)} YouTube kanalı, {unsup_count} desteklenmeyen)</b>"
        )
        self.btn_ok.setEnabled(len(self._candidates) > 0)

    def _populate_candidates(self, items: list[SubscriptionCandidate]) -> None:
        self.load_report(SubscriptionParseReport(candidates=items, unsupported=[]))

    def _set_all_checked(self, checked: bool) -> None:
        state = QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked
        for i in range(self.list_widget.count()):
            it = self.list_widget.item(i)
            if it.data(QtCore.Qt.ItemDataRole.UserRole) is not None:
                it.setCheckState(state)

    def _pick_file(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Abonelik Dosyası Seç (NewsFlash OPML / Google Takeout CSV / JSON)",
            "",
            "Desteklenen Dosyalar (*.opml *.xml *.csv *.json *.txt);;Tüm Dosyalar (*)",
        )
        if not path:
            return
        try:
            rep = parse_subscriptions_report_file(path)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Dosya Okunamadı", str(e))
            return
        self.file_status_lbl.setText(
            f"Seçilen dosya: {path} ({len(rep.candidates)} YouTube kanalı, "
            f"{len(rep.unsupported)} desteklenmeyen kayıt)"
        )
        self.load_report(rep)

    def _scan_browser(self) -> None:
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        browser = self.browser_combo.currentData() or "firefox"
        self.btn_scan_browser.setEnabled(False)
        self.browser_status_lbl.setText(f"{browser} çerezleriyle YouTube abonelikleri okunuyor…")
        self._scan_worker = _BrowserScanWorker(browser, self)
        self._scan_worker.succeeded.connect(self._on_browser_scan_ok)
        self._scan_worker.failed.connect(self._on_browser_scan_err)
        self._scan_worker.start()

    def _on_browser_scan_ok(self, items: list[SubscriptionCandidate]) -> None:
        self.btn_scan_browser.setEnabled(True)
        self.browser_status_lbl.setText(f"Tarayıcıdan {len(items)} kanal bulundu.")
        self._populate_candidates(items)

    def _on_browser_scan_err(self, err: str) -> None:
        self.btn_scan_browser.setEnabled(True)
        self.browser_status_lbl.setText(
            f"Tarayıcıdan okunamadı ({err}). Bunun yerine NewsFlash OPML veya Google Takeout CSV dosyasını kullanabilirsiniz."
        )

    def _parse_pasted(self) -> None:
        txt = self.paste_edit.toPlainText()
        rep = parse_subscriptions_report_text(txt)
        self.load_report(rep)

    def _export_opml(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Abonelikleri OPML Olarak Kaydet",
            "youtube_aboneliklerim.opml",
            "OPML Dosyası (*.opml *.xml)",
        )
        if not path:
            return
        try:
            n = export_subscriptions_opml(self.db, path)
            QtWidgets.QMessageBox.information(
                self,
                "OPML Dışa Aktarma Tamamlandı",
                f"{n} kanal grup yapısıyla birlikte <code>{path}</code> dosyasına kaydedildi.",
            )
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Dışa Aktarma Hatası", str(e))

    def _export_full_backup(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Sürümlü Tam Yedek Oluştur",
            "ytrss_tam_yedek.json",
            "JSON Yedek Dosyası (*.json)",
        )
        if not path:
            return
        try:
            payload = export_backup_file(self.db, path)
            n_ch = len(payload.get("channels") or [])
            n_vid = len(payload.get("videos") or [])
            n_wl = len(payload.get("watch_later_queue") or [])
            msg = (
                f"Yedek kaydedildi: {path} "
                f"({n_ch} kanal, {n_vid} içerik, {n_wl} Sonra İzle öğesi)"
            )
            self.backup_status_lbl.setText(msg)
            QtWidgets.QMessageBox.information(self, "Tam Yedek Oluşturuldu", msg)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Yedekleme Hatası", str(e))

    def _restore_full_backup(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Sürümlü Yedek Dosyası Seç (Birleştirerek Geri Yükle)",
            "",
            "JSON Yedek Dosyası (*.json);;Tüm Dosyalar (*)",
        )
        if not path:
            return
        try:
            res = restore_backup_file(self.db, path, merge=True)
            msg = (
                f"Yedek (v{res.version}) birleştirilerek geri yüklendi: "
                f"{res.channels_added} yeni kanal ({res.channels_merged} mevcut korundu), "
                f"{res.videos_added} yeni içerik ({res.videos_merged} mevcut birleştirildi), "
                f"Sonra İzle kuyruğu: {res.watch_later_count} öğe."
            )
            self.backup_status_lbl.setText(msg)
            self.backupRestored.emit(res)
            QtWidgets.QMessageBox.information(self, "Geri Yükleme Tamamlandı", msg)
        except Exception as e:  # noqa: BLE001
            QtWidgets.QMessageBox.warning(self, "Geri Yükleme Hatası", str(e))

    def selected_values(
        self,
    ) -> tuple[
        list[SubscriptionCandidate],
        int | None,
        tuple[int | None, int | None, int | None],
    ]:
        chosen: list[SubscriptionCandidate] = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            idx_data = item.data(QtCore.Qt.ItemDataRole.UserRole)
            if idx_data is not None and item.checkState() == QtCore.Qt.CheckState.Checked:
                chosen.append(self._candidates[int(idx_data)])
        return (
            chosen,
            self.group_combo.currentData(),
            (
                self.cb_video.currentData(),
                self.cb_short.currentData(),
                self.cb_live.currentData(),
            ),
        )


class ShortcutsHelpDialog(QtWidgets.QDialog):
    """Klavye kısayollarını gösteren kompakt yardım penceresi."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Klavye Kısayolları")
        self.setMinimumWidth(460)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QtWidgets.QLabel(
            "<b>Hızlı Gezinme ve İşlem Kısayolları</b><br>"
            "<span style='color:#9da5b4; font-size:12px;'>"
            "Not: Arama kutusu veya metin alanlarına yazarken tek tuşlu kısayollar "
            "otomatik olarak devre dışı kalır.</span>"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        box = QtWidgets.QFrame()
        box.setObjectName("InfoBox")
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(14, 12, 14, 12)
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(8)

        shortcuts = [
            ("J / ↓", "Video listesinde sonraki içeriğe git"),
            ("K / ↑", "Video listesinde önceki içeriğe git"),
            ("Enter", "Seçili videoyu varsayılan tarayıcıda (YouTube'da) aç"),
            ("/ veya Ctrl+F", "Arama kutusuna odaklan"),
            ("Escape", "Arama kutusundan çıkıp odağı video listesine döndür"),
            ("M", "Seçili videoyu Okundu / Okunmadı olarak işaretle"),
            ("S", "Seçili videoyu Yıldızla / Yıldızı kaldır"),
            ("W", "Seçili videoyu Sonra İzle kuyruğuna ekle / çıkar"),
            ("I", "Seçili videoyu İzledim / İzlenmedi olarak işaretle"),
            ("F5 / Ctrl+R", "Tüm kanalları şimdi yenile"),
            ("Ctrl+N", "Yeni YouTube kanalı ekle"),
            ("? / F1", "Bu klavye kısayolları yardım penceresini aç"),
        ]

        for row_idx, (key_txt, desc_txt) in enumerate(shortcuts):
            k_lbl = QtWidgets.QLabel(f"<code><b>{key_txt}</b></code>")
            k_lbl.setStyleSheet(
                "background-color: #252932; color: #e6e9ef; border: 1px solid #343946; "
                "border-radius: 4px; padding: 2px 8px;"
            )
            d_lbl = QtWidgets.QLabel(desc_txt)
            d_lbl.setStyleSheet(f"color: {TEXT_PRIMARY};")
            grid.addWidget(k_lbl, row_idx, 0, QtCore.Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(d_lbl, row_idx, 1)

        grid.setColumnStretch(1, 1)
        layout.addWidget(box)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Close)
        btns.button(QtWidgets.QDialogButtonBox.StandardButton.Close).setText("Kapat")
        btns.rejected.connect(self.reject)
        btns.accepted.connect(self.accept)
        layout.addWidget(btns)
