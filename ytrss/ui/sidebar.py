"""Sol sütun: Akıllı görünümler, kanal grupları, kanal listesi, görünür okunmamış sayaçları ve hata işaretleri."""
from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from ..db import Database, unread_counts
from .theme import (
    ACCENT_BLUE,
    BG_CARD_SELECTED,
    ERR_RED,
    STAR_GOLD,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    WARN_AMBER,
)

ROLE_NODE_ID = QtCore.Qt.ItemDataRole.UserRole
ROLE_COUNT = QtCore.Qt.ItemDataRole.UserRole + 1
ROLE_ERROR = QtCore.Qt.ItemDataRole.UserRole + 2
ROLE_WARNING = QtCore.Qt.ItemDataRole.UserRole + 3
ROLE_FILTER_HINT = QtCore.Qt.ItemDataRole.UserRole + 4
ROLE_IS_SECTION = QtCore.Qt.ItemDataRole.UserRole + 5
ROLE_BADGE_COLOR = QtCore.Qt.ItemDataRole.UserRole + 6


class SidebarDelegate(QtWidgets.QStyledItemDelegate):
    """Sol kenar çubuğu satırlarını (rozet, hata işareti ve okunmamış sayacı ile) çizer."""

    def sizeHint(self, option, index) -> QtCore.QSize:
        if index.data(ROLE_IS_SECTION):
            return QtCore.QSize(180, 28)
        return QtCore.QSize(180, 32)

    def paint(self, painter: QtGui.QPainter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)

        rect: QtCore.QRect = option.rect
        is_section = bool(index.data(ROLE_IS_SECTION))
        text = index.data(QtCore.Qt.ItemDataRole.DisplayRole) or ""

        if is_section:
            font = painter.font()
            font.setPointSizeF(max(8.5, font.pointSizeF() - 1.5))
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QtGui.QColor(TEXT_MUTED))
            painter.drawText(
                rect.adjusted(10, 6, -8, -2),
                QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                text.upper(),
            )
            painter.restore()
            return

        selected = bool(option.state & QtWidgets.QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QtWidgets.QStyle.StateFlag.State_MouseOver)

        bg_rect = rect.adjusted(4, 1, -4, -1)
        if selected:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(BG_CARD_SELECTED))
            painter.drawRoundedRect(bg_rect, 6, 6)
            # Sol vurgu çizgisi
            bar = QtCore.QRect(bg_rect.left(), bg_rect.top() + 5, 3, bg_rect.height() - 10)
            painter.setBrush(QtGui.QColor(ACCENT_BLUE))
            painter.drawRoundedRect(bar, 1.5, 1.5)
        elif hovered:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor("#22262e"))
            painter.drawRoundedRect(bg_rect, 6, 6)

        count = int(index.data(ROLE_COUNT) or 0)
        err = index.data(ROLE_ERROR)
        warn = index.data(ROLE_WARNING)
        f_hint = index.data(ROLE_FILTER_HINT) or ""
        dot_color = index.data(ROLE_BADGE_COLOR)

        right_edge = bg_rect.right() - 8

        # 1. Sağda görünür okunmamış / öğe sayacı hapı
        if count > 0:
            c_str = str(count) if count < 1000 else "999+"
            f_small = painter.font()
            f_small.setPointSizeF(max(8.5, f_small.pointSizeF() - 1.0))
            f_small.setBold(True)
            painter.setFont(f_small)
            fm = QtGui.QFontMetrics(f_small)
            pill_w = max(22, fm.horizontalAdvance(c_str) + 12)
            pill_h = 18
            pill_rect = QtCore.QRect(
                right_edge - pill_w,
                bg_rect.center().y() - pill_h // 2,
                pill_w,
                pill_h,
            )
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor("#323948" if selected else "#262b36"))
            painter.drawRoundedRect(pill_rect, 9, 9)
            painter.setPen(QtGui.QColor(TEXT_PRIMARY if selected else TEXT_SECONDARY))
            painter.drawText(pill_rect, QtCore.Qt.AlignmentFlag.AlignCenter, c_str)
            right_edge = pill_rect.left() - 6

        # 2. Kanal hata / uyarı işareti
        if err or warn:
            f_warn = painter.font()
            f_warn.setBold(True)
            painter.setFont(f_warn)
            painter.setPen(QtGui.QColor(ERR_RED if err else WARN_AMBER))
            w_rect = QtCore.QRect(right_edge - 16, bg_rect.top(), 16, bg_rect.height())
            painter.drawText(w_rect, QtCore.Qt.AlignmentFlag.AlignCenter, "⚠")
            right_edge = w_rect.left() - 4

        # 3. Özel kanal filtresi kısaltması (ör. V·S)
        if f_hint:
            f_tiny = painter.font()
            f_tiny.setPointSizeF(max(7.5, f_tiny.pointSizeF() - 2.0))
            painter.setFont(f_tiny)
            fm_t = QtGui.QFontMetrics(f_tiny)
            hw = fm_t.horizontalAdvance(f_hint) + 6
            h_rect = QtCore.QRect(right_edge - hw, bg_rect.top(), hw, bg_rect.height())
            painter.setPen(QtGui.QColor(TEXT_MUTED))
            painter.drawText(h_rect, QtCore.Qt.AlignmentFlag.AlignCenter, f_hint)
            right_edge = h_rect.left() - 4

        # 4. Sol simge / renk noktası
        left_edge = bg_rect.left() + 10
        if dot_color:
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(dot_color))
            painter.drawEllipse(QtCore.QPoint(left_edge + 5, bg_rect.center().y()), 4, 4)
            left_edge += 16

        # 5. Başlık metni
        f_main = painter.font()
        f_main.setBold(selected or count > 0)
        painter.setFont(f_main)
        painter.setPen(QtGui.QColor(TEXT_PRIMARY if (selected or count > 0) else TEXT_SECONDARY))
        text_rect = QtCore.QRect(left_edge, bg_rect.top(), max(20, right_edge - left_edge), bg_rect.height())
        elided = QtGui.QFontMetrics(f_main).elidedText(
            text, QtCore.Qt.TextElideMode.ElideRight, text_rect.width()
        )
        painter.drawText(
            text_rect,
            QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
            elided,
        )
        painter.restore()


class SidebarPane(QtWidgets.QFrame):
    nodeSelected = QtCore.Signal(str)            # node id (all, today, unread, starred, watch_later, unknown, group:X, channel:UC...)
    addChannelRequested = QtCore.Signal()
    importSubscriptionsRequested = QtCore.Signal()
    addGroupRequested = QtCore.Signal()
    channelSettingsRequested = QtCore.Signal(str)
    refreshChannelRequested = QtCore.Signal(str)
    refreshAllRequested = QtCore.Signal()
    markNodeReadRequested = QtCore.Signal(str)
    deleteChannelRequested = QtCore.Signal(str)
    globalSettingsRequested = QtCore.Signal()
    shortcutsHelpRequested = QtCore.Signal()

    def __init__(self, db: Database, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.setObjectName("SidebarPane")
        self._current_node = "all"
        self._building = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Üst başlık çubuğu
        header = QtWidgets.QFrame()
        header.setObjectName("ColumnHeader")
        header.setFixedHeight(44)
        h_lay = QtWidgets.QHBoxLayout(header)
        h_lay.setContentsMargins(12, 6, 8, 6)
        h_lay.setSpacing(4)

        title_lbl = QtWidgets.QLabel("<b>YouTube Akışları</b>")
        h_lay.addWidget(title_lbl, 1)

        self.btn_refresh = QtWidgets.QPushButton("↻ Yenile")
        self.btn_refresh.setToolTip("Tüm kanalları şimdi yenile (F5 / Ctrl+R)")
        self.btn_refresh.clicked.connect(self.refreshAllRequested.emit)
        h_lay.addWidget(self.btn_refresh)

        self.btn_help = QtWidgets.QPushButton("?")
        self.btn_help.setObjectName("FlatIconButton")
        self.btn_help.setToolTip("Klavye kısayolları yardımı (? / F1)")
        self.btn_help.clicked.connect(self.shortcutsHelpRequested.emit)
        h_lay.addWidget(self.btn_help)

        self.btn_settings = QtWidgets.QPushButton("⚙")
        self.btn_settings.setObjectName("FlatIconButton")
        self.btn_settings.setToolTip("Genel ayarlar, bildirimler, yedekleme ve sistem tepsisi")
        self.btn_settings.clicked.connect(self.globalSettingsRequested.emit)
        h_lay.addWidget(self.btn_settings)

        layout.addWidget(header)

        # Ağaç görünümü
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setObjectName("SidebarTree")
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(14)
        self.tree.setRootIsDecorated(True)
        self.tree.setMouseTracking(True)
        self.tree.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        t_pal = self.tree.palette()
        t_pal.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor("#17191e"))
        t_pal.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(TEXT_PRIMARY))
        self.tree.setPalette(t_pal)
        self.tree.setItemDelegate(SidebarDelegate(self.tree))
        self.tree.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.itemExpanded.connect(self._on_group_expanded)
        self.tree.itemCollapsed.connect(self._on_group_collapsed)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.tree, 1)

        # Alt eylem çubuğu (2x2 kompakt ızgara — dar sütunda bile metinler kesilmez)
        footer = QtWidgets.QFrame()
        footer.setObjectName("ColumnHeader")
        f_lay = QtWidgets.QGridLayout(footer)
        f_lay.setContentsMargins(8, 6, 8, 6)
        f_lay.setHorizontalSpacing(6)
        f_lay.setVerticalSpacing(5)

        self.btn_add_channel = QtWidgets.QPushButton("+ Kanal Ekle")
        self.btn_add_channel.setToolTip(
            "Yeni YouTube kanalı ekle (@kullanıcı, /channel/UC… veya RSS) [Kısayol: Ctrl+N]"
        )
        self.btn_add_channel.clicked.connect(self.addChannelRequested.emit)
        f_lay.addWidget(self.btn_add_channel, 0, 0)

        self.btn_import = QtWidgets.QPushButton("⇄ Abonelik Aktar")
        self.btn_import.setToolTip(
            "Toplu kanal aktarımı (NewsFlash OPML, Google Takeout CSV/JSON, Tarayıcı) ve Yedekleme"
        )
        self.btn_import.clicked.connect(self.importSubscriptionsRequested.emit)
        f_lay.addWidget(self.btn_import, 0, 1)

        self.btn_add_group = QtWidgets.QPushButton("+ Yeni Grup")
        self.btn_add_group.setToolTip("Yeni kanal grubu oluştur")
        self.btn_add_group.clicked.connect(self.addGroupRequested.emit)
        f_lay.addWidget(self.btn_add_group, 1, 0)

        self.btn_ch_prefs = QtWidgets.QPushButton("Kanal Filtresi")
        self.btn_ch_prefs.setToolTip("Seçili kanalın tür görünürlük ve bildirim ayarlarını düzenle")
        self.btn_ch_prefs.setEnabled(False)
        self.btn_ch_prefs.clicked.connect(self._open_selected_channel_settings)
        f_lay.addWidget(self.btn_ch_prefs, 1, 1)

        layout.addWidget(footer)

    def current_node(self) -> str:
        return self._current_node

    def select_node(self, node_id: str) -> None:
        self._current_node = node_id
        self.reload()
        self.nodeSelected.emit(node_id)

    def _channel_filter_hint(self, ch) -> str:
        parts = []
        if ch["show_video"] is not None:
            parts.append("V+" if ch["show_video"] else "V-")
        if ch["show_short"] is not None:
            parts.append("S+" if ch["show_short"] else "S-")
        if ch["show_live"] is not None:
            parts.append("C+" if ch["show_live"] else "C-")
        return " ".join(parts)

    def _channel_tooltip(self, ch, vis) -> str:
        eff_v = ch["show_video"] if ch["show_video"] is not None else vis.video
        eff_s = ch["show_short"] if ch["show_short"] is not None else vis.short
        eff_l = ch["show_live"] if ch["show_live"] is not None else vis.live
        lines = [
            f"<b>{ch['title']}</b>",
            f"Kanal kimliği: <code>{ch['channel_id']}</code>",
            f"Görünür türler: Normal={'Açık' if eff_v else 'Kapalı'}, "
            f"Shorts={'Açık' if eff_s else 'Kapalı'}, "
            f"Canlı={'Açık' if eff_l else 'Kapalı'}",
        ]
        if ch["last_error"]:
            lines.append(f"<span style='color:#ff7070'>Son hata: {ch['last_error']}</span>")
        elif ch["last_warning"]:
            lines.append(f"<span style='color:#f59e0b'>Uyarı: {ch['last_warning']}</span>")
        return "<br>".join(lines)

    def reload(self) -> None:
        self._building = True
        try:
            vis = self.db.visibility_defaults()
            counts = unread_counts(self.db, vis)
            self.tree.clear()

            # 1. Akıllı görünümler
            smart_items = [
                ("all", "Tüm İçerikler", ACCENT_BLUE),
                ("today", "Bugün", "#38bdf8"),
                ("unread", "Okunmamışlar", "#60a5fa"),
                ("starred", "Yıldızlılar", STAR_GOLD),
                ("watch_later", "Sonra İzle", "#34d399"),
                ("unknown", "Türü Belirlenemedi", WARN_AMBER),
            ]
            target_item: QtWidgets.QTreeWidgetItem | None = None

            for nid, label, color in smart_items:
                item = QtWidgets.QTreeWidgetItem(self.tree, [label])
                item.setData(0, ROLE_NODE_ID, nid)
                item.setData(0, ROLE_COUNT, counts.get(nid, 0))
                item.setData(0, ROLE_BADGE_COLOR, color)
                if nid == "watch_later":
                    item.setToolTip(
                        0,
                        "Yerel izleme kuyruğu — videoları sürükleyerek sıralayabilirsiniz (Kısayol: W)",
                    )
                elif nid == "unknown":
                    item.setToolTip(
                        0,
                        "Yeterli veya tutarlı kanıt toplanamadığı için zorla normal video "
                        "olarak işaretlenmeyen içerikler",
                    )
                if nid == self._current_node:
                    target_item = item

            # 2. Kanal grupları ve kanallar başlığı
            sec = QtWidgets.QTreeWidgetItem(self.tree, ["Kanallar"])
            sec.setFlags(QtCore.Qt.ItemFlag.NoItemFlags)
            sec.setData(0, ROLE_IS_SECTION, True)

            groups = self.db.groups()
            channels = self.db.channels()

            group_items: dict[int, QtWidgets.QTreeWidgetItem] = {}
            for g in groups:
                gid = int(g["id"])
                nid = f"group:{gid}"
                g_item = QtWidgets.QTreeWidgetItem(self.tree, [g["name"]])
                g_item.setData(0, ROLE_NODE_ID, nid)
                g_item.setData(0, ROLE_COUNT, counts.get(nid, 0))
                g_item.setData(0, ROLE_BADGE_COLOR, "#8b95a7")
                group_items[gid] = g_item
                if nid == self._current_node:
                    target_item = g_item

            for ch in channels:
                cid = ch["channel_id"]
                nid = f"channel:{cid}"
                parent = group_items.get(ch["group_id"]) if ch["group_id"] in group_items else self.tree
                c_item = QtWidgets.QTreeWidgetItem(parent, [ch["title"]])
                c_item.setData(0, ROLE_NODE_ID, nid)
                c_item.setData(0, ROLE_COUNT, counts.get(nid, 0))
                c_item.setData(0, ROLE_ERROR, ch["last_error"])
                c_item.setData(0, ROLE_WARNING, ch["last_warning"])
                c_item.setData(0, ROLE_FILTER_HINT, self._channel_filter_hint(ch))
                c_item.setData(
                    0, ROLE_BADGE_COLOR,
                    ERR_RED if ch["last_error"] else (WARN_AMBER if ch["last_warning"] else "#525b6c"),
                )
                c_item.setToolTip(0, self._channel_tooltip(ch, vis))
                if nid == self._current_node:
                    target_item = c_item

            for g in groups:
                gid = int(g["id"])
                if gid in group_items:
                    group_items[gid].setExpanded(bool(g["expanded"]))

            if target_item is not None:
                self.tree.setCurrentItem(target_item)
            elif self.tree.topLevelItemCount() > 0:
                first = self.tree.topLevelItem(0)
                self.tree.setCurrentItem(first)
                self._current_node = "all"

            self.btn_ch_prefs.setEnabled(self._current_node.startswith("channel:"))
        finally:
            self._building = False

    def _on_selection_changed(self) -> None:
        if self._building:
            return
        items = self.tree.selectedItems()
        if not items:
            return
        nid = items[0].data(0, ROLE_NODE_ID)
        if not nid:
            return
        self._current_node = nid
        self.btn_ch_prefs.setEnabled(nid.startswith("channel:"))
        self.nodeSelected.emit(nid)

    def _on_item_double_clicked(self, item: QtWidgets.QTreeWidgetItem, _col: int) -> None:
        nid = item.data(0, ROLE_NODE_ID) or ""
        if nid.startswith("channel:"):
            self.channelSettingsRequested.emit(nid.split(":", 1)[1])

    def _open_selected_channel_settings(self) -> None:
        if self._current_node.startswith("channel:"):
            self.channelSettingsRequested.emit(self._current_node.split(":", 1)[1])

    def _on_group_expanded(self, item: QtWidgets.QTreeWidgetItem) -> None:
        if self._building:
            return
        nid = item.data(0, ROLE_NODE_ID) or ""
        if nid.startswith("group:"):
            self.db.set_group_expanded(int(nid.split(":", 1)[1]), True)

    def _on_group_collapsed(self, item: QtWidgets.QTreeWidgetItem) -> None:
        if self._building:
            return
        nid = item.data(0, ROLE_NODE_ID) or ""
        if nid.startswith("group:"):
            self.db.set_group_expanded(int(nid.split(":", 1)[1]), False)

    def _show_context_menu(self, pos: QtCore.QPoint) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        nid = item.data(0, ROLE_NODE_ID) or ""
        if not nid:
            return

        menu = QtWidgets.QMenu(self)
        if nid.startswith("channel:"):
            cid = nid.split(":", 1)[1]
            act_prefs = menu.addAction("Kanal Ayarları ve Tür Filtreleri…")
            act_refresh = menu.addAction("Bu Kanalı Yenile")
            act_read = menu.addAction("Kanaldaki Görünür İçerikleri Okundu İşaretle")
            menu.addSeparator()
            act_open = menu.addAction("YouTube'da Kanalı Aç")
            menu.addSeparator()
            act_del = menu.addAction("Kanalı Kaldır…")

            chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if chosen == act_prefs:
                self.channelSettingsRequested.emit(cid)
            elif chosen == act_refresh:
                self.refreshChannelRequested.emit(cid)
            elif chosen == act_read:
                self.markNodeReadRequested.emit(nid)
            elif chosen == act_open:
                QtGui.QDesktopServices.openUrl(
                    QtCore.QUrl(f"https://www.youtube.com/channel/{cid}")
                )
            elif chosen == act_del:
                self.deleteChannelRequested.emit(cid)
        elif nid.startswith("group:"):
            gid = int(nid.split(":", 1)[1])
            act_read = menu.addAction("Gruptaki Görünür İçerikleri Okundu İşaretle")
            act_rename = menu.addAction("Grubu Yeniden Adlandır…")
            act_del = menu.addAction("Grubu Sil")
            chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if chosen == act_read:
                self.markNodeReadRequested.emit(nid)
            elif chosen == act_rename:
                name, ok = QtWidgets.QInputDialog.getText(
                    self, "Grubu Yeniden Adlandır", "Yeni grup adı:", text=item.text(0)
                )
                if ok and name.strip():
                    self.db.rename_group(gid, name.strip())
                    self.reload()
            elif chosen == act_del:
                self.db.delete_group(gid)
                if self._current_node == nid:
                    self._current_node = "all"
                self.reload()
                self.nodeSelected.emit(self._current_node)
        else:
            act_read = menu.addAction("Bu Görünümdeki İçerikleri Okundu İşaretle")
            chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
            if chosen == act_read:
                self.markNodeReadRequested.emit(nid)
