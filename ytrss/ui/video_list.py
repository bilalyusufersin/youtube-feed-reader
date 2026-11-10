"""Orta sütun: Tarihe göre gruplanmış (veya Sonra İzle kuyruğunda sürükle-bırak sıralamalı),
Qt Model/View tabanlı hızlı video listesi."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets

from ..db import Database, ListQuery, build_list_sql, start_of_today
from ..models import ContentType, LiveState
from .theme import (
    ACCENT_BLUE,
    BADGE_COLORS,
    BG_CARD_HOVER,
    BG_CARD_SELECTED,
    BORDER_SUBTLE,
    STAR_GOLD,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
)
from .thumb_cache import ThumbnailCache

TR_MONTHS_LONG = [
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]
TR_MONTHS_SHORT = [
    "", "Oca", "Şub", "Mar", "Nis", "May", "Haz",
    "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara",
]


def format_duration(seconds: int | None) -> str:
    """Süreyi 12:34 veya 1:02:45 biçiminde döndürür. Bilinmiyorsa '' döner (asla 00:00 dönmez)."""
    if seconds is None:
        return ""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def thumbnail_corner_overlay(row: dict[str, Any]) -> tuple[str, str, str]:
    """Küçük resmin sağ alt köşesinde gösterilecek (metin, arka_plan_hex, metin_hex) çiftini döndürür.

    - Aktif canlı yayınlarda: 'CANLI'
    - Yaklaşan yayınlarda: 'Planlandı'
    - Süresi bilinen içeriklerde: '12:34' veya '1:02:45'
    - Süre bilinmiyorsa: ('', '', '') — 00:00 gibi yanlış bir değer gösterilmez.
    """
    eff_type = row.get("user_type") or row.get("auto_type") or "unknown"
    live_state = row.get("live_state")
    premiere_state = row.get("premiere_state")

    if (eff_type == ContentType.LIVE.value and live_state == LiveState.LIVE.value) or (
        premiere_state == LiveState.LIVE.value
    ):
        return ("CANLI", "#c62828", "#ffffff")

    if (eff_type == ContentType.LIVE.value and live_state == LiveState.SCHEDULED.value) or (
        premiere_state == LiveState.SCHEDULED.value
    ):
        return ("Planlandı", "#b45309", "#ffffff")

    dur_str = format_duration(row.get("duration"))
    if dur_str:
        return (dur_str, "#000000", "#ffffff")
    return ("", "", "")


def format_short_date(ts: int | None, today_start: int) -> str:
    if not ts:
        return ""
    lt = time.localtime(ts)
    if ts >= today_start:
        return f"Bugün {lt.tm_hour:02d}:{lt.tm_min:02d}"
    if ts >= today_start - 86400:
        return f"Dün {lt.tm_hour:02d}:{lt.tm_min:02d}"
    now_lt = time.localtime()
    if lt.tm_year == now_lt.tm_year:
        return f"{lt.tm_mday:02d} {TR_MONTHS_SHORT[lt.tm_mon]} {lt.tm_hour:02d}:{lt.tm_min:02d}"
    return f"{lt.tm_mday:02d} {TR_MONTHS_SHORT[lt.tm_mon]} {lt.tm_year}"


def date_group_label(ts: int | None, today_start: int) -> str:
    if not ts:
        return "Tarihi Bilinmeyenler"
    if ts >= today_start:
        return "Bugün"
    if ts >= today_start - 86400:
        return "Dün"
    if ts >= today_start - 6 * 86400:
        return "Bu Hafta"
    lt = time.localtime(ts)
    return f"{TR_MONTHS_LONG[lt.tm_mon]} {lt.tm_year}"


def badge_spec_for_row(row: dict[str, Any]) -> list[tuple[str, str]]:
    """[(etiket, BADGE_COLORS_anahtari), ...] döndürür."""
    eff_type = row.get("user_type") or row.get("auto_type") or "unknown"
    live_state = row.get("live_state")
    premiere_state = row.get("premiere_state")
    badges: list[tuple[str, str]] = []

    if eff_type == ContentType.LIVE.value:
        if live_state == LiveState.LIVE.value:
            badges.append(("● Şu Anda Canlı", "live_now"))
        elif live_state == LiveState.SCHEDULED.value:
            badges.append(("Canlı Yayın · Planlandı", "live_scheduled"))
        elif live_state == LiveState.COMPLETED.value:
            badges.append(("Canlı Yayın · Tamamlandı", "live_completed"))
        else:
            badges.append(("Canlı Yayın", "live_completed"))
    elif eff_type == ContentType.SHORT.value:
        badges.append(("Shorts", "short"))
    elif eff_type == ContentType.VIDEO.value:
        badges.append(("Normal Video", "video"))
    else:
        badges.append(("Belirlenemedi", "unknown"))

    if premiere_state and eff_type != ContentType.LIVE.value:
        p_map = {
            LiveState.SCHEDULED.value: "Prömiyer Planlandı",
            LiveState.LIVE.value: "Prömiyer Sürüyor",
            LiveState.COMPLETED.value: "Prömiyer",
        }
        badges.append((p_map.get(premiere_state, "Prömiyer"), "premiere"))

    if row.get("user_type"):
        badges.append(("Elle seçildi", "unknown"))

    if row.get("is_watched"):
        badges.append(("✓ İzlendi", "watched"))

    if row.get("watch_later"):
        badges.append(("⏱ Sonra İzle", "watch_later"))

    return badges


@dataclass(slots=True)
class ListEntry:
    is_header: bool
    header_title: str = ""
    video: dict[str, Any] | None = None
    queue_pos: int | None = None


ROLE_ENTRY = QtCore.Qt.ItemDataRole.UserRole


class VideoListModel(QtCore.QAbstractListModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._entries: list[ListEntry] = []
        self._video_index_map: dict[str, int] = {}
        self._url_rows: dict[str, list[int]] = {}
        self._video_count = 0
        self._watch_later_mode = False

    def is_watch_later_mode(self) -> bool:
        return self._watch_later_mode

    def rowCount(self, parent=QtCore.QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._entries)

    def video_count(self) -> int:
        return self._video_count

    def all_video_ids(self) -> list[str]:
        return [
            e.video["video_id"]
            for e in self._entries
            if not e.is_header and e.video is not None
        ]

    def data(self, index: QtCore.QModelIndex, role: int = QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._entries)):
            return None
        entry = self._entries[index.row()]
        if role == ROLE_ENTRY:
            return entry
        if role == QtCore.Qt.ItemDataRole.DisplayRole:
            return entry.header_title if entry.is_header else (entry.video or {}).get("title", "")
        return None

    def flags(self, index: QtCore.QModelIndex) -> QtCore.Qt.ItemFlag:
        if not index.isValid() or not (0 <= index.row() < len(self._entries)):
            return QtCore.Qt.ItemFlag.NoItemFlags
        entry = self._entries[index.row()]
        if entry.is_header:
            return QtCore.Qt.ItemFlag.ItemIsEnabled
        base = QtCore.Qt.ItemFlag.ItemIsEnabled | QtCore.Qt.ItemFlag.ItemIsSelectable
        if self._watch_later_mode:
            base |= QtCore.Qt.ItemFlag.ItemIsDragEnabled | QtCore.Qt.ItemFlag.ItemIsDropEnabled
        return base

    def set_rows(self, rows: list[Any], *, watch_later_mode: bool = False) -> None:
        self.beginResetModel()
        self._entries.clear()
        self._video_index_map.clear()
        self._url_rows.clear()
        self._video_count = len(rows)
        self._watch_later_mode = watch_later_mode

        today_start = start_of_today()
        last_group: str | None = None

        if watch_later_mode and rows:
            self._entries.append(
                ListEntry(
                    is_header=True,
                    header_title="SONRA İZLE KUYRUĞU (Sürükleyerek Sıralayın)",
                )
            )

        for idx_num, r in enumerate(rows, start=1):
            d = dict(r)
            raw_desc = (d.get("description") or "").strip()
            first_line = " ".join(raw_desc.splitlines()[:2]).strip()
            d["snippet"] = first_line[:180]
            d["date_str"] = format_short_date(d.get("published_at"), today_start)
            d["dur_str"] = format_duration(d.get("duration"))
            c_txt, c_bg, c_fg = thumbnail_corner_overlay(d)
            d["corner_text"] = c_txt
            d["corner_bg"] = c_bg
            d["corner_fg"] = c_fg

            if not watch_later_mode:
                grp = date_group_label(d.get("published_at"), today_start)
                if grp != last_group:
                    self._entries.append(ListEntry(is_header=True, header_title=grp))
                    last_group = grp

            row_idx = len(self._entries)
            self._entries.append(
                ListEntry(
                    is_header=False,
                    video=d,
                    queue_pos=idx_num if watch_later_mode else None,
                )
            )
            self._video_index_map[d["video_id"]] = row_idx
            t_url = d.get("thumb_url")
            if t_url:
                self._url_rows.setdefault(t_url, []).append(row_idx)

        self.endResetModel()

    def index_for_video(self, video_id: str) -> QtCore.QModelIndex:
        row = self._video_index_map.get(video_id)
        if row is None:
            return QtCore.QModelIndex()
        return self.index(row, 0)

    def notify_thumbnail_ready(self, url: str) -> None:
        for row_idx in self._url_rows.get(url, []):
            if 0 <= row_idx < len(self._entries):
                idx = self.index(row_idx, 0)
                self.dataChanged.emit(idx, idx, [QtCore.Qt.ItemDataRole.DecorationRole])

    def update_video_flags(
        self,
        video_id: str,
        *,
        is_read: int | None = None,
        is_starred: int | None = None,
        is_watched: int | None = None,
        watch_later: int | None = None,
        user_type: str | None = "KEEP",
    ) -> None:
        row_idx = self._video_index_map.get(video_id)
        if row_idx is None:
            return
        entry = self._entries[row_idx]
        if entry.video is None:
            return
        if is_read is not None:
            entry.video["is_read"] = int(is_read)
        if is_starred is not None:
            entry.video["is_starred"] = int(is_starred)
        if is_watched is not None:
            entry.video["is_watched"] = int(is_watched)
        if watch_later is not None:
            entry.video["watch_later"] = int(watch_later)
        if user_type != "KEEP":
            entry.video["user_type"] = user_type
            c_txt, c_bg, c_fg = thumbnail_corner_overlay(entry.video)
            entry.video["corner_text"] = c_txt
            entry.video["corner_bg"] = c_bg
            entry.video["corner_fg"] = c_fg
        idx = self.index(row_idx, 0)
        self.dataChanged.emit(idx, idx)


class ReorderableVideoListView(QtWidgets.QListView):
    """Sonra İzle görünümünde fareyle sürükleyerek yeniden sıralamayı destekleyen QListView."""

    orderChanged = QtCore.Signal(list)  # list[str] (yeni sıralı video_id listesi)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._drag_source_vid: str | None = None
        self._drag_press_pos: QtCore.QPoint | None = None

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        if event.button() == QtCore.Qt.MouseButton.LeftButton:
            idx = self.indexAt(event.position().toPoint())
            entry: ListEntry | None = idx.data(ROLE_ENTRY) if idx.isValid() else None
            if entry and not entry.is_header and entry.video:
                self._drag_source_vid = entry.video["video_id"]
                self._drag_press_pos = event.position().toPoint()
            else:
                self._drag_source_vid = None
                self._drag_press_pos = None
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent) -> None:
        model: VideoListModel | None = self.model()  # type: ignore[assignment]
        if (
            model is not None
            and model.is_watch_later_mode()
            and self._drag_source_vid
            and self._drag_press_pos is not None
            and (event.buttons() & QtCore.Qt.MouseButton.LeftButton)
        ):
            dist = (event.position().toPoint() - self._drag_press_pos).manhattanLength()
            if dist >= QtWidgets.QApplication.startDragDistance():
                drag = QtGui.QDrag(self)
                mime = QtCore.QMimeData()
                mime.setData("application/x-ytrss-videoid", self._drag_source_vid.encode("utf-8"))
                drag.setMimeData(mime)
                drag.exec(QtCore.Qt.DropAction.MoveAction)
                self._drag_press_pos = None
                return
        super().mouseMoveEvent(event)

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:
        model: VideoListModel | None = self.model()  # type: ignore[assignment]
        if (
            model is not None
            and model.is_watch_later_mode()
            and event.mimeData().hasFormat("application/x-ytrss-videoid")
        ):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QtGui.QDragMoveEvent) -> None:
        model: VideoListModel | None = self.model()  # type: ignore[assignment]
        if (
            model is not None
            and model.is_watch_later_mode()
            and event.mimeData().hasFormat("application/x-ytrss-videoid")
        ):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QtGui.QDropEvent) -> None:
        model: VideoListModel | None = self.model()  # type: ignore[assignment]
        if (
            model is None
            or not model.is_watch_later_mode()
            or not event.mimeData().hasFormat("application/x-ytrss-videoid")
        ):
            super().dropEvent(event)
            return

        src_vid = bytes(event.mimeData().data("application/x-ytrss-videoid")).decode("utf-8")
        current_ids = model.all_video_ids()
        if src_vid not in current_ids or len(current_ids) < 2:
            event.ignore()
            return

        drop_pos = event.position().toPoint()
        target_idx = self.indexAt(drop_pos)
        if not target_idx.isValid():
            target_pos = len(current_ids) - 1
        else:
            entry: ListEntry | None = target_idx.data(ROLE_ENTRY)
            if entry is None or entry.is_header or entry.video is None:
                target_pos = 0
            else:
                dst_vid = entry.video["video_id"]
                target_pos = current_ids.index(dst_vid) if dst_vid in current_ids else len(current_ids) - 1
                item_rect = self.visualRect(target_idx)
                if drop_pos.y() > item_rect.center().y() and target_pos < len(current_ids) - 1:
                    target_pos += 1

        new_order = [v for v in current_ids if v != src_vid]
        target_pos = max(0, min(target_pos, len(new_order)))
        new_order.insert(target_pos, src_vid)
        event.acceptProposedAction()
        if new_order != current_ids:
            self.orderChanged.emit(new_order)


class VideoItemDelegate(QtWidgets.QStyledItemDelegate):
    THUMB_W = 112
    THUMB_H = 64

    def __init__(self, thumbs: ThumbnailCache, parent=None) -> None:
        super().__init__(parent)
        self.thumbs = thumbs

    def sizeHint(self, option, index: QtCore.QModelIndex) -> QtCore.QSize:
        entry: ListEntry | None = index.data(ROLE_ENTRY)
        if entry is not None and entry.is_header:
            return QtCore.QSize(320, 30)
        return QtCore.QSize(320, 96)

    def paint(self, painter: QtGui.QPainter, option, index: QtCore.QModelIndex) -> None:
        entry: ListEntry | None = index.data(ROLE_ENTRY)
        if entry is None:
            return

        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        rect: QtCore.QRect = option.rect

        # 1. Tarih veya Kuyruk grup başlığı
        if entry.is_header:
            painter.fillRect(rect, QtGui.QColor("#191c22"))
            painter.setPen(QtGui.QColor(BORDER_SUBTLE))
            painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

            f_hdr = painter.font()
            f_hdr.setPointSizeF(max(8.5, f_hdr.pointSizeF() - 1.0))
            f_hdr.setBold(True)
            painter.setFont(f_hdr)
            painter.setPen(QtGui.QColor(TEXT_SECONDARY))
            painter.drawText(
                rect.adjusted(14, 0, -12, 0),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                entry.header_title.upper(),
            )
            painter.restore()
            return

        v = entry.video or {}
        selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.StateFlag.State_MouseOver)
        is_read = bool(v.get("is_read"))
        is_starred = bool(v.get("is_starred"))

        # Arka plan ve alt ayırıcı
        if selected:
            painter.fillRect(rect, QtGui.QColor(BG_CARD_SELECTED))
            painter.fillRect(QtCore.QRect(rect.left(), rect.top(), 3, rect.height()), QtGui.QColor(ACCENT_BLUE))
        elif hovered:
            painter.fillRect(rect, QtGui.QColor(BG_CARD_HOVER))

        painter.setPen(QtGui.QColor(BORDER_SUBTLE))
        painter.drawLine(rect.left() + 12, rect.bottom(), rect.right() - 12, rect.bottom())

        # Küçük resim kutusu (sol) — görüntü oranını korur, dikey Shorts'u esnetmez
        tx = rect.left() + 12
        ty = rect.top() + (rect.height() - self.THUMB_H) // 2
        pix = self.thumbs.get_fitted(v.get("thumb_url"), self.THUMB_W, self.THUMB_H, radius=6)
        painter.drawPixmap(tx, ty, pix)

        # Sonra İzle kuyruğunda sol üstte sıra numarası + sürükleme tutamacı
        if entry.queue_pos is not None:
            q_str = f"≡ #{entry.queue_pos}"
            f_q = painter.font()
            f_q.setPointSizeF(max(7.5, f_q.pointSizeF() - 2.0))
            f_q.setBold(True)
            painter.setFont(f_q)
            fm_q = QtGui.QFontMetrics(f_q)
            qw = fm_q.horizontalAdvance(q_str) + 8
            qh = 16
            q_rect = QtCore.QRect(tx + 4, ty + 4, qw, qh)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(18, 20, 24, 215))
            painter.drawRoundedRect(q_rect, 4, 4)
            painter.setPen(QtGui.QColor("#93c5fd"))
            painter.drawText(q_rect, QtCore.Qt.AlignmentFlag.AlignCenter, q_str)

        # Sağ alt köşe rozeti: Süre (12:34 / 1:02:45), CANLI veya Planlandı (bilinmiyorsa boş)
        corner_txt = v.get("corner_text") or ""
        if corner_txt:
            f_dur = painter.font()
            f_dur.setPointSizeF(max(7.5, f_dur.pointSizeF() - 2.0))
            f_dur.setBold(True)
            painter.setFont(f_dur)
            fm_d = QtGui.QFontMetrics(f_dur)
            dw = fm_d.horizontalAdvance(corner_txt) + 8
            dh = 16
            d_rect = QtCore.QRect(tx + self.THUMB_W - dw - 4, ty + self.THUMB_H - dh - 4, dw, dh)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            bg_c = QtGui.QColor(v.get("corner_bg") or "#000000")
            if bg_c. red() == 0 and bg_c.green() == 0 and bg_c.blue() == 0:
                bg_c.setAlpha(210)
            else:
                bg_c.setAlpha(235)
            painter.setBrush(bg_c)
            painter.drawRoundedRect(d_rect, 4, 4)
            painter.setPen(QtGui.QColor(v.get("corner_fg") or "#ffffff"))
            painter.drawText(d_rect, QtCore.Qt.AlignmentFlag.AlignCenter, corner_txt)

        # Metin alanı (sağ)
        content_left = tx + self.THUMB_W + 12
        content_right = rect.right() - 12
        content_w = max(60, content_right - content_left)
        top_y = rect.top() + 8

        # Üst satır: [okunmadı noktası] Kanal adı ... [★] Tarih/saat
        f_meta = painter.font()
        f_meta.setPointSizeF(max(8.5, f_meta.pointSizeF() - 1.2))
        painter.setFont(f_meta)
        fm_meta = QtGui.QFontMetrics(f_meta)

        date_str = v.get("date_str") or ""
        date_w = fm_meta.horizontalAdvance(date_str)
        painter.setPen(QtGui.QColor(TEXT_MUTED))
        painter.drawText(
            QtCore.QRect(content_right - date_w, top_y, date_w, 16),
            QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter,
            date_str,
        )

        right_bound = content_right - date_w - 6
        if is_starred:
            painter.setPen(QtGui.QColor(STAR_GOLD))
            painter.drawText(
                QtCore.QRect(right_bound - 14, top_y, 14, 16),
                QtCore.Qt.AlignmentFlag.AlignCenter,
                "★",
            )
            right_bound -= 16

        ch_left = content_left
        if not is_read:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(ACCENT_BLUE))
            painter.drawEllipse(QtCore.QPoint(ch_left + 4, top_y + 8), 4, 4)
            ch_left += 12

        ch_title = v.get("channel_title") or ""
        ch_elided = fm_meta.elidedText(
            ch_title, QtCore.Qt.TextElideMode.ElideRight, max(20, right_bound - ch_left)
        )
        painter.setPen(QtGui.QColor(TEXT_SECONDARY))
        painter.drawText(
            QtCore.QRect(ch_left, top_y, max(20, right_bound - ch_left), 16),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            ch_elided,
        )

        # Orta satır: Başlık
        title_y = top_y + 18
        f_title = painter.font()
        f_title.setPointSizeF(max(9.5, f_title.pointSizeF() + 1.0))
        f_title.setBold(not is_read)
        painter.setFont(f_title)
        fm_title = QtGui.QFontMetrics(f_title)
        title_elided = fm_title.elidedText(
            v.get("title") or "(Başlıksız)", QtCore.Qt.TextElideMode.ElideRight, content_w
        )
        painter.setPen(QtGui.QColor(TEXT_PRIMARY if not is_read else TEXT_SECONDARY))
        painter.drawText(
            QtCore.QRect(content_left, title_y, content_w, 20),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            title_elided,
        )

        # Kısa açıklama satırı
        snip_y = title_y + 20
        f_snip = painter.font()
        f_snip.setPointSizeF(max(8.5, f_meta.pointSizeF()))
        f_snip.setBold(False)
        painter.setFont(f_snip)
        fm_snip = QtGui.QFontMetrics(f_snip)
        snippet = fm_snip.elidedText(
            v.get("snippet") or "", QtCore.Qt.TextElideMode.ElideRight, content_w
        )
        painter.setPen(QtGui.QColor(TEXT_MUTED))
        painter.drawText(
            QtCore.QRect(content_left, snip_y, content_w, 16),
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            snippet,
        )

        # Alt satır: Tür ve durum rozetleri
        badge_y = snip_y + 19
        f_badge = painter.font()
        f_badge.setPointSizeF(max(7.5, f_meta.pointSizeF() - 0.8))
        f_badge.setBold(True)
        painter.setFont(f_badge)
        fm_badge = QtGui.QFontMetrics(f_badge)

        bx = content_left
        for b_text, b_key in badge_spec_for_row(v):
            bg_hex, fg_hex, border_hex = BADGE_COLORS.get(b_key, BADGE_COLORS["unknown"])
            bw = fm_badge.horizontalAdvance(b_text) + 12
            bh = 17
            if bx + bw > content_right:
                break
            b_rect = QtCore.QRect(bx, badge_y, bw, bh)
            painter.setPen(QtGui.QPen(QtGui.QColor(border_hex), 1))
            painter.setBrush(QtGui.QColor(bg_hex))
            painter.drawRoundedRect(b_rect, 4, 4)
            painter.setPen(QtGui.QColor(fg_hex))
            painter.drawText(b_rect, QtCore.Qt.AlignmentFlag.AlignCenter, b_text)
            bx += bw + 6

        painter.restore()


class MiddlePane(QtWidgets.QFrame):
    videoSelected = QtCore.Signal(str)                   # video_id (veya "" seçim kalkınca)
    toggleReadRequested = QtCore.Signal(str, bool)       # video_id, read
    toggleStarRequested = QtCore.Signal(str, bool)       # video_id, starred
    toggleWatchedRequested = QtCore.Signal(str, bool)    # video_id, watched
    toggleWatchLaterRequested = QtCore.Signal(str, bool) # video_id, in_queue
    watchLaterOrderChanged = QtCore.Signal(list)         # list[str]
    manualTypeRequested = QtCore.Signal(str, object)     # video_id, str | None
    recheckMetaRequested = QtCore.Signal(str)            # video_id
    markAllVisibleReadRequested = QtCore.Signal()
    missingDurationsDetected = QtCore.Signal(list)       # list[str]

    def __init__(self, db: Database, thumbs: ThumbnailCache, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.thumbs = thumbs
        self.setObjectName("MiddlePane")

        self.query = ListQuery(
            hide_watched_in_queue=bool(self.db.get_setting("watch_later_hide_watched"))
        )
        self._current_video_id: str | None = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Üst filtre başlığı (2 satırlı kompakt bar)
        header = QtWidgets.QFrame()
        header.setObjectName("ColumnHeader")
        h_lay = QtWidgets.QVBoxLayout(header)
        h_lay.setContentsMargins(10, 6, 10, 8)
        h_lay.setSpacing(6)

        row1 = QtWidgets.QHBoxLayout()
        row1.setSpacing(4)

        self.btn_all = QtWidgets.QPushButton("Tümü")
        self.btn_all.setCheckable(True)
        self.btn_all.setChecked(True)
        self.btn_unread = QtWidgets.QPushButton("Okunmamış")
        self.btn_unread.setCheckable(True)
        self.btn_starred = QtWidgets.QPushButton("Yıldızlı")
        self.btn_starred.setCheckable(True)

        self._read_group = QtWidgets.QButtonGroup(self)
        self._read_group.setExclusive(True)
        for btn, val in (
            (self.btn_all, "all"),
            (self.btn_unread, "unread"),
            (self.btn_starred, "starred"),
        ):
            self._read_group.addButton(btn)
            btn.clicked.connect(lambda _checked=False, v=val: self._set_read_filter(v))
            row1.addWidget(btn)

        self.chk_hide_watched = QtWidgets.QCheckBox("İzlenenleri gizle")
        self.chk_hide_watched.setToolTip(
            "Sonra İzle kuyruğunda 'İzlendi' olarak işaretlenen içerikleri gizle"
        )
        self.chk_hide_watched.setChecked(bool(self.db.get_setting("watch_later_hide_watched")))
        self.chk_hide_watched.toggled.connect(self._on_hide_watched_toggled)
        self.chk_hide_watched.setVisible(False)
        row1.addWidget(self.chk_hide_watched)

        row1.addStretch(1)

        self.count_label = QtWidgets.QLabel("0 içerik")
        self.count_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 12px;")
        row1.addWidget(self.count_label)

        self.btn_mark_all = QtWidgets.QPushButton("✓ Tümü Okundu")
        self.btn_mark_all.setToolTip("Listedeki görünür içeriklerin tümünü okundu işaretle")
        self.btn_mark_all.clicked.connect(self.markAllVisibleReadRequested.emit)
        row1.addWidget(self.btn_mark_all)

        h_lay.addLayout(row1)

        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)

        self.search_edit = QtWidgets.QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setPlaceholderText("Başlık, açıklama veya kanal ara… (/)")
        self.search_edit.setToolTip("Arama kutusuna odaklanmak için '/' veya Ctrl+F tuşuna basın")
        self.search_edit.textChanged.connect(self._on_search_changed)
        row2.addWidget(self.search_edit, 1)

        self.type_combo = QtWidgets.QComboBox()
        self.type_combo.setToolTip("İçerik türüne göre filtrele")
        self.type_combo.addItem("Tüm Türler", "any")
        self.type_combo.addItem("Normal Video", "video")
        self.type_combo.addItem("Shorts", "short")
        self.type_combo.addItem("Canlı Yayın", "live")
        self.type_combo.addItem("Belirlenemedi", "unknown")
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        row2.addWidget(self.type_combo)

        self.duration_combo = QtWidgets.QComboBox()
        self.duration_combo.setToolTip("Video süresine göre filtrele (aralıklar çakışmaz)")
        self.duration_combo.addItem("Tüm Süreler", "any")
        self.duration_combo.addItem("5 dakikadan kısa", "under_5")
        self.duration_combo.addItem("5–15 dakika", "5_to_15")
        self.duration_combo.addItem("15–30 dakika", "15_to_30")
        self.duration_combo.addItem("30 dakika ve üzeri", "over_30")
        self.duration_combo.addItem("Süresi bilinmeyen", "unknown")
        self.duration_combo.currentIndexChanged.connect(self._on_duration_changed)
        row2.addWidget(self.duration_combo)

        h_lay.addLayout(row2)
        layout.addWidget(header)

        # Liste görünümü + boş durum yığını
        self.stack = QtWidgets.QStackedWidget()

        self.model = VideoListModel(self)
        self.list_view = ReorderableVideoListView()
        self.list_view.setObjectName("VideoListView")
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(VideoItemDelegate(self.thumbs, self.list_view))
        self.list_view.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list_view.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setMouseTracking(True)
        self.list_view.setAcceptDrops(True)
        self.list_view.setDragEnabled(True)
        self.list_view.setDropIndicatorShown(True)
        self.list_view.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_view.selectionModel().currentChanged.connect(self._on_current_changed)
        self.list_view.doubleClicked.connect(self._on_double_clicked)
        self.list_view.customContextMenuRequested.connect(self._show_context_menu)
        self.list_view.orderChanged.connect(self._on_queue_order_changed)
        self.thumbs.thumbnailReady.connect(self.model.notify_thumbnail_ready)

        self.empty_label = QtWidgets.QLabel(
            "Bu görünümde gösterilecek içerik yok.\n\n"
            "Sol sütundan '+ Kanal Ekle' ile YouTube kanalı ekleyebilir veya "
            "kanalın tür görünürlük filtrelerini (Normal Video / Shorts / Canlı Yayın) değiştirebilirsiniz."
        )
        self.empty_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setContentsMargins(28, 28, 28, 28)
        self.empty_label.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 13px;")

        self.stack.addWidget(self.list_view)
        self.stack.addWidget(self.empty_label)
        layout.addWidget(self.stack, 1)

        # RSS penceresi bilgi şeridi (alt)
        rss_footer = QtWidgets.QFrame()
        rss_footer.setObjectName("ColumnHeader")
        rf_lay = QtWidgets.QHBoxLayout(rss_footer)
        rf_lay.setContentsMargins(10, 4, 10, 4)
        self.rss_note = QtWidgets.QLabel(
            "Bilgi: YouTube RSS akışları kanal başına sınırlı bir son içerik penceresi (~15 kayıt) sunar."
        )
        self.rss_note.setStyleSheet(f"color: {TEXT_MUTED}; font-size: 11px;")
        self.rss_note.setWordWrap(True)
        rf_lay.addWidget(self.rss_note)
        layout.addWidget(rss_footer)

    def set_node(self, node_id: str) -> None:
        self.query.node = node_id
        self.chk_hide_watched.setVisible(node_id == "watch_later")
        self.reload()

    def _on_hide_watched_toggled(self, checked: bool) -> None:
        self.query.hide_watched_in_queue = bool(checked)
        self.db.set_setting("watch_later_hide_watched", int(checked))
        self.reload(preserve_selection=True)
        self.watchLaterOrderChanged.emit([])

    def _set_read_filter(self, val: str) -> None:
        self.query.read_filter = val
        self.reload()

    def _on_search_changed(self, text: str) -> None:
        self.query.search = text
        self.reload()

    def _on_type_changed(self, _idx: int) -> None:
        self.query.type_filter = self.type_combo.currentData() or "any"
        self.reload()

    def _on_duration_changed(self, _idx: int) -> None:
        self.query.duration_filter = self.duration_combo.currentData() or "any"
        self.reload()

    def _on_queue_order_changed(self, ordered_ids: list[str]) -> None:
        if ordered_ids:
            self.db.reorder_watch_later(ordered_ids)
            self.reload(preserve_selection=True)
            self.watchLaterOrderChanged.emit(ordered_ids)

    def move_video_in_queue(self, video_id: str, new_index: int) -> None:
        """Sonra İzle kuyruğundaki bir videoyu belirtilen sıra indeksine (0-tabanlı) taşır."""
        current_ids = self.model.all_video_ids()
        if video_id not in current_ids:
            return
        reordered = [v for v in current_ids if v != video_id]
        target = max(0, min(new_index, len(reordered)))
        reordered.insert(target, video_id)
        self.db.reorder_watch_later(reordered)
        self.reload(preserve_selection=True)
        self.watchLaterOrderChanged.emit(reordered)

    def reload(self, preserve_selection: bool = True) -> None:
        prev_vid = self._current_video_id if preserve_selection else None
        vis = self.db.visibility_defaults()
        self.query.hide_watched_in_queue = bool(self.db.get_setting("watch_later_hide_watched"))
        sql, params = build_list_sql(self.query, vis)
        rows = self.db.q(sql, params)
        is_wl = self.query.node == "watch_later"
        self.model.set_rows(rows, watch_later_mode=is_wl)
        n = self.model.video_count()
        self.count_label.setText(f"{n} içerik")

        # Süresi henüz bilinmeyen görünür videoları arka plan tamamlayıcısına bildir
        missing_durs = [
            r["video_id"]
            for r in rows[:20]
            if (r["duration"] is None or int(r["duration"] or 0) <= 0)
            and (r["live_state"] not in ("live", "scheduled"))
        ]
        if missing_durs:
            self.missingDurationsDetected.emit(missing_durs)

        if n == 0:
            if is_wl:
                self.empty_label.setText(
                    "Sonra İzle kuyruğunuzda gösterilecek içerik yok.\n\n"
                    "Videoları listeden sağ tıklayarak, ayrıntı panelinden veya 'W' kısayoluyla "
                    "Sonra İzle kuyruğuna ekleyebilirsiniz."
                )
            else:
                self.empty_label.setText(
                    "Bu görünümde gösterilecek içerik yok.\n\n"
                    "Sol sütundan '+ Kanal Ekle' ile YouTube kanalı ekleyebilir veya "
                    "kanalın tür ve süre filtrelerini değiştirebilirsiniz."
                )
            self.stack.setCurrentWidget(self.empty_label)
            return

        self.stack.setCurrentWidget(self.list_view)
        if prev_vid:
            idx = self.model.index_for_video(prev_vid)
            if idx.isValid():
                self.list_view.selectionModel().setCurrentIndex(
                    idx, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect
                )

    def select_video(self, video_id: str) -> bool:
        idx = self.model.index_for_video(video_id)
        if not idx.isValid():
            return False
        self.list_view.setCurrentIndex(idx)
        self.list_view.scrollTo(idx)
        return True

    def select_first_video(self) -> str | None:
        for row in range(self.model.rowCount()):
            idx = self.model.index(row, 0)
            entry: ListEntry | None = idx.data(ROLE_ENTRY)
            if entry and not entry.is_header and entry.video:
                self.list_view.setCurrentIndex(idx)
                return entry.video["video_id"]
        return None

    def select_next_video(self) -> str | None:
        """Klavye kısayolu (J) için listedeki bir sonraki videoyu seçer."""
        total = self.model.rowCount()
        if total == 0:
            return None
        cur_idx = self.list_view.currentIndex()
        start_row = (cur_idx.row() + 1) if cur_idx.isValid() else 0
        for row in range(start_row, total):
            idx = self.model.index(row, 0)
            entry: ListEntry | None = idx.data(ROLE_ENTRY)
            if entry and not entry.is_header and entry.video:
                self.list_view.setCurrentIndex(idx)
                self.list_view.scrollTo(idx)
                return entry.video["video_id"]
        return self._current_video_id

    def select_prev_video(self) -> str | None:
        """Klavye kısayolu (K) için listedeki bir önceki videoyu seçer."""
        total = self.model.rowCount()
        if total == 0:
            return None
        cur_idx = self.list_view.currentIndex()
        if not cur_idx.isValid():
            return self.select_first_video()
        for row in range(cur_idx.row() - 1, -1, -1):
            idx = self.model.index(row, 0)
            entry: ListEntry | None = idx.data(ROLE_ENTRY)
            if entry and not entry.is_header and entry.video:
                self.list_view.setCurrentIndex(idx)
                self.list_view.scrollTo(idx)
                return entry.video["video_id"]
        return self._current_video_id

    def _on_current_changed(self, current: QtCore.QModelIndex, _previous: QtCore.QModelIndex) -> None:
        entry: ListEntry | None = current.data(ROLE_ENTRY)
        if entry is None or entry.is_header or entry.video is None:
            return
        vid = entry.video["video_id"]
        self._current_video_id = vid
        self.videoSelected.emit(vid)

    def _on_double_clicked(self, index: QtCore.QModelIndex) -> None:
        entry: ListEntry | None = index.data(ROLE_ENTRY)
        if entry and not entry.is_header and entry.video:
            vid = entry.video["video_id"]
            # YouTube'da açmak otomatik olarak "İzlendi" yapmaz!
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl(f"https://www.youtube.com/watch?v={vid}")
            )

    def _show_context_menu(self, pos: QtCore.QPoint) -> None:
        idx = self.list_view.indexAt(pos)
        if not idx.isValid():
            return
        entry: ListEntry | None = idx.data(ROLE_ENTRY)
        if entry is None or entry.is_header or entry.video is None:
            return
        v = entry.video
        vid = v["video_id"]
        is_read = bool(v.get("is_read"))
        is_starred = bool(v.get("is_starred"))
        is_watched = bool(v.get("is_watched"))
        in_wl = bool(v.get("watch_later"))

        menu = QtWidgets.QMenu(self)
        act_open = menu.addAction("YouTube'da Aç (Enter)")
        act_copy = menu.addAction("Bağlantıyı Kopyala")
        menu.addSeparator()
        act_wl = menu.addAction(
            "Sonra İzle Kuyruğundan Çıkar (W)" if in_wl else "Sonra İzle Kuyruğuna Ekle (W)"
        )
        act_watched = menu.addAction(
            "İzlenmedi İşaretle (I)" if is_watched else "İzledim İşaretle (I)"
        )
        act_read = menu.addAction("Okunmadı İşaretle (M)" if is_read else "Okundu İşaretle (M)")
        act_star = menu.addAction("Yıldızı Kaldır (S)" if is_starred else "Yıldızla (S)")

        act_up = act_down = None
        if self.query.node == "watch_later":
            menu.addSeparator()
            act_up = menu.addAction("▲ Kuyrukta Bir Üste Taşı")
            act_down = menu.addAction("▼ Kuyrukta Bir Alta Taşı")

        menu.addSeparator()
        type_menu = menu.addMenu("İçerik Türünü Elle Düzelt")
        act_t_auto = type_menu.addAction("Otomatik Sınıflandırmayı Kullan")
        type_menu.addSeparator()
        act_t_video = type_menu.addAction("Normal Video")
        act_t_short = type_menu.addAction("Shorts")
        act_t_live = type_menu.addAction("Canlı Yayın")
        act_t_unk = type_menu.addAction("Belirlenemedi")

        act_recheck = menu.addAction("Bilgileri Yenile")

        chosen = menu.exec(self.list_view.viewport().mapToGlobal(pos))
        if chosen == act_open:
            QtGui.QDesktopServices.openUrl(
                QtCore.QUrl(f"https://www.youtube.com/watch?v={vid}")
            )
        elif chosen == act_copy:
            QtWidgets.QApplication.clipboard().setText(f"https://www.youtube.com/watch?v={vid}")
        elif chosen == act_wl:
            self.toggleWatchLaterRequested.emit(vid, not in_wl)
        elif chosen == act_watched:
            self.toggleWatchedRequested.emit(vid, not is_watched)
        elif chosen == act_read:
            self.toggleReadRequested.emit(vid, not is_read)
        elif chosen == act_star:
            self.toggleStarRequested.emit(vid, not is_starred)
        elif act_up is not None and chosen == act_up:
            ids = self.model.all_video_ids()
            if vid in ids:
                cur_i = ids.index(vid)
                if cur_i > 0:
                    self.move_video_in_queue(vid, cur_i - 1)
        elif act_down is not None and chosen == act_down:
            ids = self.model.all_video_ids()
            if vid in ids:
                cur_i = ids.index(vid)
                if cur_i < len(ids) - 1:
                    self.move_video_in_queue(vid, cur_i + 1)
        elif chosen == act_t_auto:
            self.manualTypeRequested.emit(vid, None)
        elif chosen == act_t_video:
            self.manualTypeRequested.emit(vid, "video")
        elif chosen == act_t_short:
            self.manualTypeRequested.emit(vid, "short")
        elif chosen == act_t_live:
            self.manualTypeRequested.emit(vid, "live")
        elif chosen == act_t_unk:
            self.manualTypeRequested.emit(vid, "unknown")
        elif chosen == act_recheck:
            self.recheckMetaRequested.emit(vid)
