"""Sağ sütun: Seçili içeriğin ayrıntıları, geniş görsel önizlemesi, elle tür düzeltme,
katlanabilir teknik ayrıntılar ve eylem düğmeleri."""
from __future__ import annotations

import html
import re
from PySide6 import QtCore, QtGui, QtWidgets

from ..db import Database
from ..models import CONTENT_TYPE_LABELS, ContentType, LiveState
from ..youtube.urls import watch_url
from .dialogs import format_timestamp_tr
from .theme import (
    BADGE_COLORS,
    BORDER_SUBTLE,
    STAR_GOLD,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from .thumb_cache import ThumbnailCache, render_fitted_pixmap
from .video_list import badge_spec_for_row, format_duration, thumbnail_corner_overlay

_URL_RE = re.compile(r"(https?://[^\s<>\"']+)")


def _linkify_description(text: str) -> str:
    escaped = html.escape(text or "")
    linked = _URL_RE.sub(r'<a href="\1" style="color:#60a5fa;">\1</a>', escaped)
    return linked.replace("\n", "<br>")


class AspectPreviewWidget(QtWidgets.QWidget):
    """Geniş önizleme görselini görüntü oranını koruyarak çizer (dikey Shorts esnetilmez)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pixmap: QtGui.QPixmap | None = None
        self._corner_text: str = ""
        self._corner_bg: str = "#000000"
        self._corner_fg: str = "#ffffff"
        self.setMinimumHeight(250)
        self.setMaximumHeight(440)
        self.setSizePolicy(
            QtWidgets.QSizePolicy.Policy.Expanding,
            QtWidgets.QSizePolicy.Policy.Preferred,
        )

    def set_pixmap(self, pix: QtGui.QPixmap | None) -> None:
        self._pixmap = pix
        self._update_dynamic_height()
        self.update()

    def set_corner_overlay(self, text: str, bg: str = "#000000", fg: str = "#ffffff") -> None:
        self._corner_text = text or ""
        self._corner_bg = bg or "#000000"
        self._corner_fg = fg or "#ffffff"
        self.update()

    def _update_dynamic_height(self) -> None:
        w = max(320, self.width())
        target_h = max(250, min(430, int(w * 0.54)))
        if abs(self.height() - target_h) > 6:
            self.setFixedHeight(target_h)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        self._update_dynamic_height()
        super().resizeEvent(event)

    def paintEvent(self, _event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        w = max(40, self.width())
        h = max(40, self.height())
        fitted = render_fitted_pixmap(self._pixmap, w, h, radius=10)
        painter.drawPixmap(0, 0, fitted)

        if self._corner_text:
            f_dur = painter.font()
            f_dur.setPointSizeF(max(9.0, f_dur.pointSizeF()))
            f_dur.setBold(True)
            painter.setFont(f_dur)
            fm = QtGui.QFontMetrics(f_dur)
            bw = fm.horizontalAdvance(self._corner_text) + 14
            bh = 24
            b_rect = QtCore.QRect(w - bw - 12, h - bh - 10, bw, bh)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            bg_c = QtGui.QColor(self._corner_bg)
            if bg_c.red() == 0 and bg_c.green() == 0 and bg_c.blue() == 0:
                bg_c.setAlpha(210)
            else:
                bg_c.setAlpha(235)
            painter.setBrush(bg_c)
            painter.drawRoundedRect(b_rect, 6, 6)
            painter.setPen(QtGui.QColor(self._corner_fg))
            painter.drawText(b_rect, QtCore.Qt.AlignmentFlag.AlignCenter, self._corner_text)

        painter.end()


class DetailPane(QtWidgets.QFrame):
    toggleReadRequested = QtCore.Signal(str, bool)        # video_id, read
    toggleStarRequested = QtCore.Signal(str, bool)        # video_id, starred
    toggleWatchedRequested = QtCore.Signal(str, bool)     # video_id, watched
    toggleWatchLaterRequested = QtCore.Signal(str, bool)  # video_id, in_queue
    manualTypeRequested = QtCore.Signal(str, object)      # video_id, str | None
    recheckMetaRequested = QtCore.Signal(str)             # video_id
    openChannelSettingsRequested = QtCore.Signal(str)     # channel_id

    def __init__(self, db: Database, thumbs: ThumbnailCache, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.thumbs = thumbs
        self.setObjectName("DetailPane")
        self._video_id: str | None = None
        self._thumb_url: str | None = None
        self._updating_ui = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Üst eylem çubuğu (dar pencerelerde de rahat sığan kompakt düğmeler)
        self.header = QtWidgets.QFrame()
        self.header.setObjectName("ColumnHeader")
        self.header.setFixedHeight(44)
        h_lay = QtWidgets.QHBoxLayout(self.header)
        h_lay.setContentsMargins(10, 6, 10, 6)
        h_lay.setSpacing(6)

        self.btn_open_yt = QtWidgets.QPushButton("▶ YouTube'da Aç")
        self.btn_open_yt.setObjectName("PrimaryButton")
        self.btn_open_yt.setToolTip("Videoyu varsayılan web tarayıcısında aç (Kısayol: Enter)")
        self.btn_open_yt.clicked.connect(self._open_in_browser)
        h_lay.addWidget(self.btn_open_yt)

        self.btn_copy = QtWidgets.QPushButton("Bağlantıyı Kopyala")
        self.btn_copy.setToolTip("Video bağlantısını panoya kopyala")
        self.btn_copy.clicked.connect(self._copy_link)
        h_lay.addWidget(self.btn_copy)

        self.btn_watch_later = QtWidgets.QPushButton("⏱ Sonra İzle")
        self.btn_watch_later.setToolTip("Sonra İzle kuyruğuna ekle veya çıkar (Kısayol: W)")
        self.btn_watch_later.clicked.connect(self._toggle_watch_later)
        h_lay.addWidget(self.btn_watch_later)

        self.btn_watched = QtWidgets.QPushButton("İzledim")
        self.btn_watched.setToolTip(
            "İzlendi / İzlenmedi durumunu değiştir — okundu ve yıldızlı durumlarından bağımsızdır (Kısayol: I)"
        )
        self.btn_watched.clicked.connect(self._toggle_watched)
        h_lay.addWidget(self.btn_watched)

        self.btn_star = QtWidgets.QPushButton("☆ Yıldızla")
        self.btn_star.setToolTip("Yıldızla / Yıldızı kaldır (Kısayol: S)")
        self.btn_star.clicked.connect(self._toggle_star)
        h_lay.addWidget(self.btn_star)

        self.btn_read = QtWidgets.QPushButton("Okundu")
        self.btn_read.setToolTip("Okundu / Okunmadı olarak işaretle (Kısayol: M)")
        self.btn_read.clicked.connect(self._toggle_read)
        h_lay.addWidget(self.btn_read)

        h_lay.addStretch(1)

        self.btn_recheck = QtWidgets.QPushButton("Bilgileri yenile")
        self.btn_recheck.setToolTip(
            "yt-dlp ile tekil video üstverisini, süreyi ve canlı yayın durumunu güncelle "
            "(elle seçilen türü değiştirmez)"
        )
        self.btn_recheck.clicked.connect(self._recheck_meta)
        h_lay.addWidget(self.btn_recheck)

        layout.addWidget(self.header)

        # Boş durum / içerik yığını
        self.stack = QtWidgets.QStackedWidget()

        # 1. Boş durum sayfası
        empty_page = QtWidgets.QWidget()
        e_lay = QtWidgets.QVBoxLayout(empty_page)
        e_lay.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        e_lay.setSpacing(10)

        e_icon = QtWidgets.QLabel("▶")
        e_icon.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        e_icon.setStyleSheet(
            "font-size: 40px; color: #363c4a; background-color: #1c1f26; "
            "border: 1px solid #282c35; border-radius: 32px; min-width: 64px; min-height: 64px;"
        )
        e_lay.addWidget(e_icon, 0, QtCore.Qt.AlignmentFlag.AlignCenter)

        e_title = QtWidgets.QLabel("İçerik Seçilmedi")
        e_title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        e_title.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 15px; font-weight: 600;")
        e_lay.addWidget(e_title)

        e_sub = QtWidgets.QLabel(
            "Ayrıntıları, görsel önizlemesini ve açıklamayı görmek için\n"
            "orta sütundaki listeden bir içerik seçin (J / K tuşlarıyla gezinebilirsiniz)."
        )
        e_sub.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        e_sub.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12.5px;")
        e_lay.addWidget(e_sub)

        self.stack.addWidget(empty_page)

        # 2. İçerik ayrıntı sayfası (kaydırılabilir)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        c_lay = QtWidgets.QVBoxLayout(content)
        c_lay.setContentsMargins(20, 16, 20, 24)
        c_lay.setSpacing(12)

        self.preview = AspectPreviewWidget()
        c_lay.addWidget(self.preview)

        # Rozet satırı + Elle tür düzeltme (her zaman doğrudan erişilebilir)
        top_bar_row = QtWidgets.QHBoxLayout()
        top_bar_row.setContentsMargins(0, 0, 0, 0)
        top_bar_row.setSpacing(8)

        self.badges_widget = QtWidgets.QWidget(content)
        self.badges_layout = QtWidgets.QHBoxLayout(self.badges_widget)
        self.badges_layout.setContentsMargins(0, 0, 0, 0)
        self.badges_layout.setSpacing(6)
        top_bar_row.addWidget(self.badges_widget, 1)

        override_lbl = QtWidgets.QLabel("Tür:")
        override_lbl.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        top_bar_row.addWidget(override_lbl)

        self.override_combo = QtWidgets.QComboBox()
        self.override_combo.setMinimumWidth(190)
        self.override_combo.setToolTip(
            "İçerik türünü elle düzeltin; yenilemeler bu tercihi ezmez"
        )
        self.override_combo.currentIndexChanged.connect(self._on_override_changed)
        top_bar_row.addWidget(self.override_combo)

        c_lay.addLayout(top_bar_row)

        # Başlık
        self.title_label = QtWidgets.QLabel()
        self.title_label.setWordWrap(True)
        self.title_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.title_label.setStyleSheet(f"font-size: 18px; font-weight: 700; color: {TEXT_PRIMARY};")
        c_lay.addWidget(self.title_label)

        # Kanal adı + tarih + süre + görüntülenme satırı
        meta_row = QtWidgets.QHBoxLayout()
        meta_row.setSpacing(10)
        self.channel_btn = QtWidgets.QPushButton()
        self.channel_btn.setObjectName("FlatIconButton")
        self.channel_btn.setStyleSheet("color: #7cc4fa; font-weight: 600; padding: 2px 6px;")
        self.channel_btn.setToolTip("Kanalın tür görünürlük ve bildirim ayarlarını aç")
        self.channel_btn.clicked.connect(self._open_channel_settings)
        meta_row.addWidget(self.channel_btn)

        self.meta_info_label = QtWidgets.QLabel()
        self.meta_info_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12.5px;")
        self.meta_info_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        meta_row.addWidget(self.meta_info_label, 1)
        c_lay.addLayout(meta_row)

        # Açıklama başlığı ve metni
        desc_hdr = QtWidgets.QLabel("<b>Açıklama</b>")
        desc_hdr.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 13px;")
        c_lay.addWidget(desc_hdr)

        self.desc_label = QtWidgets.QLabel()
        self.desc_label.setWordWrap(True)
        self.desc_label.setOpenExternalLinks(True)
        self.desc_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.desc_label.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextBrowserInteraction
            | QtCore.Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.desc_label.setStyleSheet(
            f"color: {TEXT_PRIMARY}; font-size: 13px; line-height: 1.45; "
            f"background-color: #191c22; border: 1px solid {BORDER_SUBTLE}; "
            f"border-radius: 8px; padding: 12px;"
        )
        c_lay.addWidget(self.desc_label)

        # Varsayılan olarak kapalı "Teknik ayrıntılar" bölümü
        tech_container = QtWidgets.QVBoxLayout()
        tech_container.setSpacing(6)

        self.btn_tech_toggle = QtWidgets.QPushButton("▸ Teknik ayrıntılar")
        self.btn_tech_toggle.setObjectName("FlatIconButton")
        self.btn_tech_toggle.setCheckable(True)
        self.btn_tech_toggle.setChecked(False)
        self.btn_tech_toggle.setStyleSheet(
            f"color: {TEXT_MUTED}; font-size: 12px; font-weight: 600; text-align: left; padding: 4px 6px;"
        )
        self.btn_tech_toggle.toggled.connect(self._on_tech_toggled)
        tech_container.addWidget(self.btn_tech_toggle, 0, QtCore.Qt.AlignmentFlag.AlignLeft)

        self.tech_box = QtWidgets.QFrame()
        self.tech_box.setObjectName("InfoBox")
        self.tech_box.setVisible(False)
        tech_lay = QtWidgets.QVBoxLayout(self.tech_box)
        tech_lay.setContentsMargins(14, 10, 14, 10)
        tech_lay.setSpacing(6)

        self.cls_details_label = QtWidgets.QLabel()
        self.cls_details_label.setWordWrap(True)
        self.cls_details_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cls_details_label.setStyleSheet(f"color: {TEXT_SECONDARY}; font-size: 12px;")
        tech_lay.addWidget(self.cls_details_label)

        tech_container.addWidget(self.tech_box)
        c_lay.addLayout(tech_container)

        c_lay.addStretch(1)

        scroll.setWidget(content)
        self.stack.addWidget(scroll)
        layout.addWidget(self.stack, 1)

        self.thumbs.thumbnailReady.connect(self._on_thumb_ready)
        self.clear_selection()

    def _on_tech_toggled(self, checked: bool) -> None:
        self.tech_box.setVisible(checked)
        self.btn_tech_toggle.setText(
            "▾ Teknik ayrıntılar" if checked else "▸ Teknik ayrıntılar"
        )

    def clear_selection(self) -> None:
        self._video_id = None
        self._thumb_url = None
        self.header.setEnabled(False)
        self.stack.setCurrentIndex(0)

    def current_video_id(self) -> str | None:
        return self._video_id

    def show_video(self, video_id: str | None) -> None:
        if not video_id:
            self.clear_selection()
            return
        row = self.db.video(video_id)
        if row is None:
            self.clear_selection()
            return

        self._updating_ui = True
        try:
            self._video_id = video_id
            d = dict(row)
            self._thumb_url = d.get("thumb_url")
            self.header.setEnabled(True)
            self.stack.setCurrentIndex(1)

            # Görsel ve sağ alt köşe süre/canlı etiketi
            pix = self.thumbs.get_or_fetch(self._thumb_url)
            self.preview.set_pixmap(pix)
            c_txt, c_bg, c_fg = thumbnail_corner_overlay(d)
            self.preview.set_corner_overlay(c_txt, c_bg, c_fg)

            # Rozetler
            while self.badges_layout.count():
                item = self.badges_layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.hide()
                    w.setParent(None)
                    w.deleteLater()

            for b_text, b_key in badge_spec_for_row(d):
                bg_hex, fg_hex, border_hex = BADGE_COLORS.get(b_key, BADGE_COLORS["unknown"])
                lbl = QtWidgets.QLabel(b_text, self.badges_widget)
                lbl.setStyleSheet(
                    f"background-color: {bg_hex}; color: {fg_hex}; "
                    f"border: 1px solid {border_hex}; border-radius: 5px; "
                    f"padding: 3px 9px; font-weight: 600; font-size: 11.5px;"
                )
                self.badges_layout.addWidget(lbl)

            dur_str = format_duration(d.get("duration"))
            eff_type = d.get("user_type") or d.get("auto_type") or "unknown"
            live_st = d.get("live_state")
            if eff_type == ContentType.LIVE.value and live_st == LiveState.LIVE.value:
                dlbl = QtWidgets.QLabel("Durum: CANLI", self.badges_widget)
                dlbl.setStyleSheet(
                    "background-color: #471a1c; color: #ff7070; border-radius: 5px; "
                    "padding: 3px 8px; font-weight: 600; font-size: 11.5px;"
                )
                self.badges_layout.addWidget(dlbl)
            elif eff_type == ContentType.LIVE.value and live_st == LiveState.SCHEDULED.value:
                dlbl = QtWidgets.QLabel("Durum: Planlandı", self.badges_widget)
                dlbl.setStyleSheet(
                    "background-color: #382e18; color: #ffd166; border-radius: 5px; "
                    "padding: 3px 8px; font-weight: 600; font-size: 11.5px;"
                )
                self.badges_layout.addWidget(dlbl)
            elif dur_str:
                dlbl = QtWidgets.QLabel(f"Süre: {dur_str}", self.badges_widget)
                dlbl.setStyleSheet(
                    "background-color: #22262f; color: #d1d5db; border-radius: 5px; "
                    "padding: 3px 8px; font-size: 11.5px;"
                )
                self.badges_layout.addWidget(dlbl)

            self.badges_layout.addStretch(1)

            # Başlık ve meta (ID teknik ayrıntılara taşındı, süre burada da yer alır)
            self.title_label.setText(d.get("title") or "(Başlıksız)")
            self.channel_btn.setText(d.get("channel_title") or d.get("channel_id") or "")
            self._channel_id = d.get("channel_id")

            meta_parts = [f"Yayın: {format_timestamp_tr(d.get('published_at'))}"]
            if dur_str:
                meta_parts.append(f"Süre: {dur_str}")
            elif eff_type == ContentType.LIVE.value and live_st == LiveState.LIVE.value:
                meta_parts.append("CANLI")
            elif eff_type == ContentType.LIVE.value and live_st == LiveState.SCHEDULED.value:
                meta_parts.append("Planlandı")
            if d.get("scheduled_at") and d.get("live_state") == "scheduled":
                meta_parts.append(f"Planlanan başlangıç: {format_timestamp_tr(d.get('scheduled_at'))}")
            if d.get("views") is not None:
                meta_parts.append(f"{int(d['views']):,} görüntülenme".replace(",", "."))
            self.meta_info_label.setText("  ·  ".join(meta_parts))

            # Eylem düğmeleri durumları
            in_wl = bool(d.get("watch_later"))
            self.btn_watch_later.setText("⏱ Kuyrukta ✓" if in_wl else "⏱ Sonra İzle")
            self.btn_watch_later.setStyleSheet("color: #93c5fd;" if in_wl else "")

            is_watched = bool(d.get("is_watched"))
            self.btn_watched.setText("İzlendi ✓" if is_watched else "İzledim")
            self.btn_watched.setStyleSheet("color: #6ee7b7;" if is_watched else "")

            starred = bool(d.get("is_starred"))
            self.btn_star.setText("★ Yıldızlı" if starred else "☆ Yıldızla")
            self.btn_star.setStyleSheet(f"color: {STAR_GOLD};" if starred else "")

            is_read = bool(d.get("is_read"))
            self.btn_read.setText("Okundu ✓" if is_read else "Okunmadı")

            # Elle tür düzeltme açılır listesi
            auto_t = d.get("auto_type") or "unknown"
            auto_label = CONTENT_TYPE_LABELS.get(ContentType(auto_t), auto_t)
            self.override_combo.clear()
            self.override_combo.addItem(f"Otomatik ({auto_label})", None)
            self.override_combo.addItem("Normal Video (elle sabitlendi)", "video")
            self.override_combo.addItem("Shorts (elle sabitlendi)", "short")
            self.override_combo.addItem("Canlı Yayın (elle sabitlendi)", "live")
            self.override_combo.addItem("Belirlenemedi (elle sabitlendi)", "unknown")

            user_t = d.get("user_type")
            idx_map = {None: 0, "video": 1, "short": 2, "live": 3, "unknown": 4}
            self.override_combo.setCurrentIndex(idx_map.get(user_t, 0))

            # Teknik ayrıntılar (varsayılan olarak kapalı katlanabilir kutu içinde)
            ev = self.db.evidence(video_id)
            ev_keys = ", ".join(sorted(ev.keys())) if ev else "henüz sinyal yok"
            cls_lines = [
                f"<b>Video kimliği:</b> <code>{html.escape(video_id)}</code>",
                f"<b>Sınıflandırma açıklaması:</b> {html.escape(d.get('class_reason') or 'Henüz sınıflandırılmadı.')}",
                f"<b>Kaynak:</b> <code>{html.escape(d.get('class_source') or '—')}</code>  "
                f"·  <b>Kayıtlı sinyaller:</b> <code>{html.escape(ev_keys)}</code>",
                f"<b>Kontrol zamanı:</b> {format_timestamp_tr(d.get('classified_at'))}  "
                f"·  <b>Yöntem sürümü:</b> v{d.get('classifier_version') or 1}",
            ]
            if user_t:
                u_lbl = CONTENT_TYPE_LABELS.get(ContentType(user_t), user_t)
                cls_lines.append(
                    f"<span style='color:#ffd166'><b>Not:</b> Tür kullanıcı tarafından "
                    f"<b>{u_lbl}</b> olarak sabitlendi; yenileme bu tercihi değiştirmez.</span>"
                )
            self.cls_details_label.setText("<br>".join(cls_lines))

            # Açıklama
            desc = (d.get("description") or "").strip()
            if desc:
                self.desc_label.setText(_linkify_description(desc))
            else:
                self.desc_label.setText(
                    f"<span style='color:{TEXT_MUTED}'>Bu içerik için açıklama metni bulunmuyor.</span>"
                )
        finally:
            self._updating_ui = False

    def _on_thumb_ready(self, url: str) -> None:
        if url and url == self._thumb_url and self._video_id:
            pix = self.thumbs.get_or_fetch(url)
            self.preview.set_pixmap(pix)

    def _open_in_browser(self) -> None:
        if self._video_id:
            # YouTube'da açmak otomatik olarak "İzlendi" yapmaz!
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(watch_url(self._video_id)))

    def _copy_link(self) -> None:
        if self._video_id:
            QtWidgets.QApplication.clipboard().setText(watch_url(self._video_id))
            self.btn_copy.setText("Kopyalandı ✓")
            QtCore.QTimer.singleShot(1400, lambda: self.btn_copy.setText("Bağlantıyı Kopyala"))

    def _toggle_watch_later(self) -> None:
        if not self._video_id:
            return
        row = self.db.video(self._video_id)
        if row:
            self.toggleWatchLaterRequested.emit(self._video_id, not bool(row["watch_later"]))

    def _toggle_watched(self) -> None:
        if not self._video_id:
            return
        row = self.db.video(self._video_id)
        if row:
            self.toggleWatchedRequested.emit(self._video_id, not bool(row["is_watched"]))

    def _toggle_star(self) -> None:
        if not self._video_id:
            return
        row = self.db.video(self._video_id)
        if row:
            self.toggleStarRequested.emit(self._video_id, not bool(row["is_starred"]))

    def _toggle_read(self) -> None:
        if not self._video_id:
            return
        row = self.db.video(self._video_id)
        if row:
            self.toggleReadRequested.emit(self._video_id, not bool(row["is_read"]))

    def _on_override_changed(self, _idx: int) -> None:
        if self._updating_ui or not self._video_id:
            return
        val = self.override_combo.currentData()
        self.manualTypeRequested.emit(self._video_id, val)

    def _recheck_meta(self) -> None:
        if self._video_id:
            self.recheckMetaRequested.emit(self._video_id)

    def _open_channel_settings(self) -> None:
        if getattr(self, "_channel_id", None):
            self.openChannelSettingsRequested.emit(self._channel_id)
