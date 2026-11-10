"""Ana pencere: Üç sütunlu (%18 sol / %32 orta / %50 sağ) NewsFlash benzeri düzen,
arka plan eşitleme işçileri, sistem tepsisi, bildirimler ve klavye kısayolları."""
from __future__ import annotations

import logging
import threading
import time

from PySide6 import QtCore, QtGui, QtWidgets

from .. import APP_ID, APP_NAME
from ..db import Database, ListQuery, build_list_sql
from ..desktop_install import app_icon
from ..models import ContentType
from ..paths import config_dir
from ..subscriptions import ImportSummary, SubscriptionCandidate, import_candidates_to_db
from ..sync import (
    ChannelSyncResult,
    NotificationEvent,
    SyncSummary,
    add_channel,
    backfill_missing_durations,
    ensure_classifier_version,
    fetch_single_video_metadata,
    sync_channel,
    sync_channels,
)
from ..youtube.source import LiveSource, YouTubeSource
from ..youtube.urls import InvalidChannelInput, parse_channel_input, watch_url
from .detail_pane import DetailPane
from .dialogs import (
    AddChannelDialog,
    ChannelSettingsDialog,
    GlobalSettingsDialog,
    ImportSubscriptionsDialog,
    ShortcutsHelpDialog,
)
from .sidebar import SidebarPane
from .thumb_cache import ThumbnailCache
from .video_list import MiddlePane

log = logging.getLogger(__name__)


class SyncWorker(QtCore.QThread):
    channelDone = QtCore.Signal(object)   # ChannelSyncResult
    syncFinished = QtCore.Signal(object)  # SyncSummary

    def __init__(self, db: Database, source: YouTubeSource,
                 channel_ids: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.source = source
        self.channel_ids = channel_ids
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            summary = sync_channels(
                self.db,
                self.source,
                self.channel_ids,
                cancel=self.cancel_event,
                max_workers=3,
                meta_budget_per_channel=5,
                on_channel_done=lambda res: self.channelDone.emit(res),
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Arka plan eşitleme hatası")
            summary = SyncSummary(
                results=[ChannelSyncResult(channel_id="", ok=False, error=str(e))]
            )
        finally:
            self.db.close_thread_connection()
        self.syncFinished.emit(summary)


class AddChannelWorker(QtCore.QThread):
    succeeded = QtCore.Signal(object, bool, object)  # ResolvedChannel, is_new, ChannelSyncResult | None
    failed = QtCore.Signal(str)

    def __init__(self, db: Database, source: YouTubeSource, user_input: str,
                 group_id: int | None,
                 prefs: tuple[int | None, int | None, int | None],
                 parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.source = source
        self.user_input = user_input
        self.group_id = group_id
        self.prefs = prefs
        self.cancel_event = threading.Event()

    def run(self) -> None:
        try:
            resolved, is_new = add_channel(
                self.db, self.source, self.user_input, group_id=self.group_id
            )
            if any(p is not None for p in self.prefs):
                self.db.set_channel_prefs(resolved.channel_id, *self.prefs)
            sync_res = sync_channel(
                self.db, self.source, resolved.channel_id,
                cancel=self.cancel_event, meta_budget=8, force_tabs=True,
            )
            self.succeeded.emit(resolved, is_new, sync_res)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))
        finally:
            self.db.close_thread_connection()


class SingleMetaWorker(QtCore.QThread):
    finishedVideo = QtCore.Signal(str, str, object)  # video_id, status_msg, NotificationEvent | None

    def __init__(self, db: Database, source: YouTubeSource, video_ids: list[str], parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.source = source
        self.video_ids = video_ids
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            for vid in self.video_ids:
                if self.cancel_event.is_set():
                    break
                try:
                    prev = self.db.video(vid)
                    was_scheduled = bool(prev and prev["live_state"] == "scheduled")
                    cls = fetch_single_video_metadata(
                        self.db, self.source, vid, cancel=self.cancel_event
                    )
                    notif: NotificationEvent | None = None
                    after = self.db.video(vid)
                    if (
                        was_scheduled
                        and after is not None
                        and (after["user_type"] or after["auto_type"]) == "live"
                        and after["live_state"] == "live"
                        and not int(after["notified_live_start"] or 0)
                    ):
                        self.db.mark_notified_live_start([vid])
                        self.db.mark_notified_new([vid])
                        ch = self.db.channel(after["channel_id"])
                        if (
                            ch is not None
                            and bool(self.db.get_setting("notifications_enabled"))
                            and bool(self.db.get_setting("notify_live_transition"))
                            and (ch["notify_enabled"] is None or int(ch["notify_enabled"]) == 1)
                        ):
                            notif = NotificationEvent(
                                video_id=vid,
                                channel_id=after["channel_id"],
                                channel_title=ch["title"] or after["channel_id"],
                                title=after["title"] or vid,
                                kind="live_started",
                                content_type="live",
                            )
                    self.finishedVideo.emit(vid, f"Bilgiler yenilendi: {cls.type.label}", notif)
                except Exception as e:  # noqa: BLE001
                    self.finishedVideo.emit(vid, f"Bilgiler alınamadı: {e}", None)
        finally:
            self.db.close_thread_connection()


class DurationBackfillWorker(QtCore.QThread):
    durationsUpdated = QtCore.Signal(list)  # list[str]

    def __init__(self, db: Database, source: YouTubeSource, video_ids: list[str], parent=None) -> None:
        super().__init__(parent)
        self.db = db
        self.source = source
        self.video_ids = video_ids
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            updated = backfill_missing_durations(
                self.db, self.source, self.video_ids, limit=3, cancel=self.cancel_event
            )
            if updated:
                self.durationsUpdated.emit(updated)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.db.close_thread_connection()


class ImportSubscriptionsWorker(QtCore.QThread):
    progress = QtCore.Signal(int, int, str)  # current, total, title
    finishedImport = QtCore.Signal(object)   # ImportSummary

    def __init__(
        self,
        db: Database,
        source: YouTubeSource,
        candidates: list[SubscriptionCandidate],
        group_id: int | None,
        prefs: tuple[int | None, int | None, int | None],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.source = source
        self.candidates = candidates
        self.group_id = group_id
        self.prefs = prefs
        self.cancel_event = threading.Event()

    def run(self) -> None:
        try:
            summary = import_candidates_to_db(
                self.db,
                self.source,
                self.candidates,
                default_group_id=self.group_id,
                prefs=self.prefs,
                cancel=self.cancel_event,
                on_progress=lambda cur, tot, name: self.progress.emit(cur, tot, name),
            )
        except Exception as e:  # noqa: BLE001
            summary = ImportSummary(
                total_found=len(self.candidates),
                failed=len(self.candidates),
                errors=[str(e)],
            )
        finally:
            self.db.close_thread_connection()
        self.finishedImport.emit(summary)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, db: Database, source: YouTubeSource | None = None,
                 auto_start_sync: bool = True) -> None:
        super().__init__()
        self.db = db
        self.source: YouTubeSource = source or LiveSource()
        self._owns_source = source is None
        self._force_quit = False
        self._last_notified_video_id: str | None = None
        self._notification_history: list[NotificationEvent] = []

        self.setWindowTitle(APP_NAME)
        self.resize(1440, 860)
        self.setMinimumSize(920, 560)

        ensure_classifier_version(self.db)

        cache_mb = int(self.db.get_setting("thumb_cache_mb") or 200)
        self.thumbs = ThumbnailCache(max_disk_mb=cache_mb, parent=self)

        # Ana üç sütunlu ayırıcı (%18 sol / %32 orta / %50 sağ)
        self.splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal, self)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)

        self.sidebar = SidebarPane(self.db, self.splitter)
        self.middle = MiddlePane(self.db, self.thumbs, self.splitter)
        self.detail = DetailPane(self.db, self.thumbs, self.splitter)

        self.sidebar.setMinimumWidth(195)
        self.middle.setMinimumWidth(300)
        self.detail.setMinimumWidth(360)

        self.splitter.addWidget(self.sidebar)
        self.splitter.addWidget(self.middle)
        self.splitter.addWidget(self.detail)
        self.splitter.setStretchFactor(0, 18)
        self.splitter.setStretchFactor(1, 32)
        self.splitter.setStretchFactor(2, 50)
        self.setCentralWidget(self.splitter)

        # Durum çubuğu
        self.status_bar = self.statusBar()
        self.status_bar.setStyleSheet(
            "QStatusBar { background-color: #14161b; color: #9da5b4; border-top: 1px solid #282c35; font-size: 12px; }"
        )
        self.status_bar.showMessage("Hazır — yerel veritabanı yüklendi.")

        # Sinyal bağlantıları
        self.sidebar.nodeSelected.connect(self._on_node_selected)
        self.sidebar.addChannelRequested.connect(self._prompt_add_channel)
        self.sidebar.importSubscriptionsRequested.connect(self._prompt_import_subscriptions)
        self.sidebar.addGroupRequested.connect(self._prompt_add_group)
        self.sidebar.channelSettingsRequested.connect(self._open_channel_settings)
        self.sidebar.refreshChannelRequested.connect(lambda cid: self.start_sync([cid]))
        self.sidebar.refreshAllRequested.connect(self._on_refresh_button)
        self.sidebar.markNodeReadRequested.connect(self._mark_node_read)
        self.sidebar.deleteChannelRequested.connect(self._confirm_delete_channel)
        self.sidebar.globalSettingsRequested.connect(self._open_global_settings)
        self.sidebar.shortcutsHelpRequested.connect(self._open_shortcuts_help)

        self.middle.videoSelected.connect(self._on_video_selected)
        self.middle.toggleReadRequested.connect(self._set_video_read)
        self.middle.toggleStarRequested.connect(self._set_video_starred)
        self.middle.toggleWatchedRequested.connect(self._set_video_watched)
        self.middle.toggleWatchLaterRequested.connect(self._set_video_watch_later)
        self.middle.watchLaterOrderChanged.connect(self._on_watch_later_order_changed)
        self.middle.manualTypeRequested.connect(self._set_video_manual_type)
        self.middle.recheckMetaRequested.connect(self._recheck_single_video)
        self.middle.markAllVisibleReadRequested.connect(self._mark_all_visible_read)
        self.middle.missingDurationsDetected.connect(self._schedule_duration_backfill)

        self.detail.toggleReadRequested.connect(self._set_video_read)
        self.detail.toggleStarRequested.connect(self._set_video_starred)
        self.detail.toggleWatchedRequested.connect(self._set_video_watched)
        self.detail.toggleWatchLaterRequested.connect(self._set_video_watch_later)
        self.detail.manualTypeRequested.connect(self._set_video_manual_type)
        self.detail.recheckMetaRequested.connect(self._recheck_single_video)
        self.detail.openChannelSettingsRequested.connect(self._open_channel_settings)

        # Klavye kısayolları (Ctrl/F tuşlu kısayollar + metin alanı duyarlı eventFilter)
        QtGui.QShortcut(QtGui.QKeySequence("F5"), self, activated=lambda: self.start_sync(None))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+R"), self, activated=lambda: self.start_sync(None))
        QtGui.QShortcut(QtGui.QKeySequence("Ctrl+N"), self, activated=self._prompt_add_channel)
        QtGui.QShortcut(
            QtGui.QKeySequence("Ctrl+F"),
            self,
            activated=self._focus_search_box,
        )
        QtGui.QShortcut(QtGui.QKeySequence("F1"), self, activated=self._open_shortcuts_help)

        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

        # İşçiler ve zamanlayıcılar
        self._sync_worker: SyncWorker | None = None
        self._add_worker: AddChannelWorker | None = None
        self._import_worker: ImportSubscriptionsWorker | None = None
        self._meta_worker: SingleMetaWorker | None = None
        self._duration_worker: DurationBackfillWorker | None = None

        # Sistem tepsisi (System Tray)
        self.tray_icon: QtWidgets.QSystemTrayIcon | None = None
        self.tray_menu: QtWidgets.QMenu | None = None
        self._setup_system_tray()

        self.auto_refresh_timer = QtCore.QTimer(self)
        self.auto_refresh_timer.timeout.connect(lambda: self.start_sync(None))
        self._apply_auto_refresh_timer()

        # Aktif ve yaklaşan canlı yayınları daha sık kontrol eden zamanlayıcı (3 dakikada bir)
        self.live_check_timer = QtCore.QTimer(self)
        self.live_check_timer.setInterval(180 * 1000)
        self.live_check_timer.timeout.connect(self._check_due_live_items)
        self.live_check_timer.start()

        # Yerel kayıtları hemen göster (ağ beklenmez)
        self._restore_layout_or_default()
        self.sidebar.reload()
        self.middle.reload(preserve_selection=False)

        if auto_start_sync and bool(self.db.get_setting("refresh_on_start")):
            if self.db.channels():
                QtCore.QTimer.singleShot(500, lambda: self.start_sync(None))

    # --- Klavye kısayolları ve odak kontrolü --------------------------------
    @staticmethod
    def _is_text_input_widget(w: QtWidgets.QWidget | None) -> bool:
        while w is not None:
            if isinstance(
                w,
                (
                    QtWidgets.QLineEdit,
                    QtWidgets.QPlainTextEdit,
                    QtWidgets.QTextEdit,
                    QtWidgets.QAbstractSpinBox,
                ),
            ):
                return True
            if isinstance(w, QtWidgets.QComboBox) and w.isEditable():
                return True
            w = w.parentWidget()
        return False

    def _focus_search_box(self) -> None:
        self.middle.search_edit.setFocus()
        self.middle.search_edit.selectAll()

    def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:
        if event.type() == QtCore.QEvent.Type.KeyPress and isinstance(event, QtGui.QKeyEvent):
            if not self.isVisible():
                return super().eventFilter(obj, event)
            active_modal = QtWidgets.QApplication.activeModalWidget()
            if active_modal is not None and active_modal is not self:
                return super().eventFilter(obj, event)

            focus_w = QtWidgets.QApplication.focusWidget()
            if self._is_text_input_widget(focus_w):
                if (
                    event.key() == QtCore.Qt.Key.Key_Escape
                    and focus_w is self.middle.search_edit
                ):
                    self.middle.list_view.setFocus()
                    return True
                # Metin alanına yazarken liste kısayolları çalışmaz!
                return super().eventFilter(obj, event)

            mods = event.modifiers()
            no_ctrl_alt = not (
                mods
                & (
                    QtCore.Qt.KeyboardModifier.ControlModifier
                    | QtCore.Qt.KeyboardModifier.AltModifier
                    | QtCore.Qt.KeyboardModifier.MetaModifier
                )
            )
            if not no_ctrl_alt:
                return super().eventFilter(obj, event)

            key = event.key()
            txt = event.text()
            vid = self.detail.current_video_id() or self.middle._current_video_id

            if key == QtCore.Qt.Key.Key_J:
                self.middle.select_next_video()
                return True
            if key == QtCore.Qt.Key.Key_K:
                self.middle.select_prev_video()
                return True
            if key in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
                if vid:
                    # YouTube'da açmak otomatik olarak "İzlendi" yapmaz
                    QtGui.QDesktopServices.openUrl(QtCore.QUrl(watch_url(vid)))
                    return True
            if txt == "/" or key == QtCore.Qt.Key.Key_Slash:
                self._focus_search_box()
                return True
            if txt == "?" or key == QtCore.Qt.Key.Key_Question:
                self._open_shortcuts_help()
                return True
            if key == QtCore.Qt.Key.Key_M and vid:
                row = self.db.video(vid)
                if row is not None:
                    self._set_video_read(vid, not bool(row["is_read"]))
                return True
            if key == QtCore.Qt.Key.Key_S and vid:
                row = self.db.video(vid)
                if row is not None:
                    self._set_video_starred(vid, not bool(row["is_starred"]))
                return True
            if key == QtCore.Qt.Key.Key_W and vid:
                row = self.db.video(vid)
                if row is not None:
                    self._set_video_watch_later(vid, not bool(row["watch_later"]))
                return True
            if key == QtCore.Qt.Key.Key_I and vid:
                row = self.db.video(vid)
                if row is not None:
                    self._set_video_watched(vid, not bool(row["is_watched"]))
                return True

        return super().eventFilter(obj, event)

    def _open_shortcuts_help(self) -> None:
        dlg = ShortcutsHelpDialog(self)
        dlg.exec()

    # --- Sistem tepsisi ve bildirimler --------------------------------------
    def _setup_system_tray(self) -> None:
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray_icon = QtWidgets.QSystemTrayIcon(app_icon(), self)
        self.tray_icon.setToolTip(APP_NAME)

        menu = QtWidgets.QMenu(self)
        self.act_tray_open = menu.addAction("Aç")
        self.act_tray_open.triggered.connect(self._restore_from_tray)
        self.act_tray_refresh = menu.addAction("Şimdi Yenile")
        self.act_tray_refresh.triggered.connect(lambda: self.start_sync(None))
        menu.addSeparator()
        self.act_tray_quit = menu.addAction("Çıkış")
        self.act_tray_quit.triggered.connect(self.quit_application)

        self.tray_menu = menu
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.messageClicked.connect(self._on_notification_clicked)
        self._apply_tray_settings()

    def _apply_tray_settings(self) -> None:
        if self.tray_icon is None:
            return
        enabled = bool(self.db.get_setting("tray_enabled"))
        self.tray_icon.setVisible(enabled)

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QtWidgets.QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QtWidgets.QSystemTrayIcon.ActivationReason.Trigger,
            QtWidgets.QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible() and not self.isMinimized():
                self.raise_()
                self.activateWindow()
            else:
                self._restore_from_tray()

    def _dispatch_notifications(self, events: list[NotificationEvent]) -> None:
        if not events:
            return
        self._notification_history.extend(events)
        self._last_notified_video_id = events[-1].video_id

        if len(events) == 1:
            ev = events[0]
            if ev.kind == "live_started":
                title = f"🔴 Canlı Yayın Başladı — {ev.channel_title}"
            elif ev.content_type == "short":
                title = f"Yeni Shorts — {ev.channel_title}"
            elif ev.content_type == "live":
                title = f"Yeni Canlı Yayın — {ev.channel_title}"
            else:
                title = f"Yeni Video — {ev.channel_title}"
            body = ev.title
        else:
            last_ev = events[-1]
            title = f"{len(events)} Yeni İçerik Bildirimi"
            body = f"{last_ev.channel_title}: {last_ev.title} (+{len(events) - 1} içerik daha)"

        if self.tray_icon is not None and self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                title,
                body,
                app_icon(),
                7000,
            )

    def _on_notification_clicked(self) -> None:
        if self._last_notified_video_id:
            self.select_and_show_video(self._last_notified_video_id)

    def select_and_show_video(self, video_id: str) -> bool:
        """Bildirime tıklandığında pencereyi öne getirir ve ilgili videoyu seçip gösterir."""
        self._restore_from_tray()
        row = self.db.video(video_id)
        if row is None:
            return False

        # Mevcut listede görünürse doğrudan seç
        if self.middle.select_video(video_id):
            self.detail.show_video(video_id)
            return True

        # Görünür değilse kanal düğümüne veya Tüm İçerikler'e geçip seçmeyi dene
        cid = row["channel_id"]
        self.sidebar.select_node(f"channel:{cid}")
        if self.middle.select_video(video_id):
            self.detail.show_video(video_id)
            return True

        self.sidebar.select_node("all")
        self.middle.select_video(video_id)
        self.detail.show_video(video_id)
        return True

    def quit_application(self) -> None:
        """Tepsi menüsündeki 'Çıkış' eylemi: uygulamayı ve arka plan yenilemelerini tamamen sonlandırır."""
        self._force_quit = True
        self.close()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.quit()

    # --- Pencere ve sütun oranları (%18 / %32 / %50) ------------------------
    def _settings_file(self) -> QtCore.QSettings:
        ini = config_dir() / f"{APP_ID}.ini"
        return QtCore.QSettings(str(ini), QtCore.QSettings.Format.IniFormat)

    def _restore_layout_or_default(self) -> None:
        qs = self._settings_file()
        geom = qs.value("window/geometry")
        if isinstance(geom, QtCore.QByteArray) and not geom.isEmpty():
            self.restoreGeometry(geom)
        sizes = qs.value("window/splitter_sizes")
        if sizes and len(sizes) == 3:
            try:
                parsed = [int(x) for x in sizes]
                if sum(parsed) > 400 and all(x >= 150 for x in parsed):
                    self.splitter.setSizes(parsed)
                    return
            except (TypeError, ValueError):
                pass
        self.reset_default_column_ratios()

    def reset_default_column_ratios(self) -> None:
        total = max(1000, self.width())
        self.splitter.setSizes([int(total * 0.18), int(total * 0.32), int(total * 0.50)])

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        qs = self._settings_file()
        qs.setValue("window/geometry", self.saveGeometry())
        qs.setValue("window/splitter_sizes", self.splitter.sizes())
        qs.sync()

        # "Pencere kapatılınca tepside çalışmaya devam et" (varsayılan kapalı)
        if (
            not self._force_quit
            and bool(self.db.get_setting("close_to_tray"))
            and bool(self.db.get_setting("tray_enabled"))
            and self.tray_icon is not None
            and self.tray_icon.isVisible()
        ):
            self.hide()
            self.status_bar.showMessage("Uygulama sistem tepsisinde çalışmaya devam ediyor.", 4000)
            event.ignore()
            return

        self.auto_refresh_timer.stop()
        self.live_check_timer.stop()

        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

        for worker in (self._sync_worker, self._duration_worker, self._meta_worker):
            if worker is not None and worker.isRunning():
                if hasattr(worker, "cancel"):
                    worker.cancel()
                worker.wait(1500)

        if self.tray_icon is not None:
            self.tray_icon.hide()

        self.thumbs.shutdown()
        if self._owns_source and hasattr(self.source, "close"):
            try:
                self.source.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)

    # --- Navigasyon ve içerik işlemleri -------------------------------------
    def _on_node_selected(self, node_id: str) -> None:
        self.middle.set_node(node_id)

    def _on_video_selected(self, video_id: str) -> None:
        if not video_id:
            self.detail.clear_selection()
            return
        # Videoyu seçmek yalnızca (ayar açıksa) okundu yapar; asla otomatik "İzlendi" yapmaz!
        if bool(self.db.get_setting("mark_read_on_select")):
            row = self.db.video(video_id)
            if row and not row["is_read"]:
                self.db.set_read([video_id], True)
                self.middle.model.update_video_flags(video_id, is_read=1)
                self.sidebar.reload()
        self.detail.show_video(video_id)

    def _set_video_read(self, video_id: str, is_read: bool) -> None:
        self.db.set_read([video_id], is_read)
        if self.middle.query.read_filter == "unread" or self.sidebar.current_node() == "unread":
            self.middle.reload(preserve_selection=True)
        else:
            self.middle.model.update_video_flags(video_id, is_read=int(is_read))
        self.sidebar.reload()
        if self.detail.current_video_id() == video_id:
            self.detail.show_video(video_id)

    def _set_video_starred(self, video_id: str, is_starred: bool) -> None:
        self.db.set_starred(video_id, is_starred)
        if self.middle.query.read_filter == "starred" or self.sidebar.current_node() == "starred":
            self.middle.reload(preserve_selection=True)
        else:
            self.middle.model.update_video_flags(video_id, is_starred=int(is_starred))
        self.sidebar.reload()
        if self.detail.current_video_id() == video_id:
            self.detail.show_video(video_id)

    def _set_video_watched(self, video_id: str, is_watched: bool) -> None:
        self.db.set_watched(video_id, is_watched)
        if (
            self.sidebar.current_node() == "watch_later"
            and bool(self.db.get_setting("watch_later_hide_watched"))
        ):
            self.middle.reload(preserve_selection=True)
        else:
            self.middle.model.update_video_flags(video_id, is_watched=int(is_watched))
        self.sidebar.reload()
        if self.detail.current_video_id() == video_id:
            self.detail.show_video(video_id)
        self.status_bar.showMessage(
            "İçerik 'İzlendi' olarak işaretlendi." if is_watched else "İçerik 'İzlenmedi' olarak işaretlendi.",
            3500,
        )

    def _set_video_watch_later(self, video_id: str, in_queue: bool) -> None:
        self.db.set_watch_later(video_id, in_queue)
        if self.sidebar.current_node() == "watch_later":
            self.middle.reload(preserve_selection=True)
        else:
            self.middle.model.update_video_flags(video_id, watch_later=int(in_queue))
        self.sidebar.reload()
        if self.detail.current_video_id() == video_id:
            self.detail.show_video(video_id)
        self.status_bar.showMessage(
            "Sonra İzle kuyruğuna eklendi." if in_queue else "Sonra İzle kuyruğundan çıkarıldı.",
            3500,
        )

    def _on_watch_later_order_changed(self, ordered_ids: list[str]) -> None:
        self.sidebar.reload()
        if ordered_ids:
            self.status_bar.showMessage("Sonra İzle kuyruk sırası güncellendi.", 3500)

    def _set_video_manual_type(self, video_id: str, user_type: str | None) -> None:
        self.db.set_user_type(video_id, user_type)
        self.middle.reload(preserve_selection=True)
        self.sidebar.reload()
        if self.detail.current_video_id() == video_id:
            self.detail.show_video(video_id)
        if user_type:
            lbl = ContentType(user_type).label
            self.status_bar.showMessage(f"İçerik türü elle '{lbl}' olarak kaydedildi.", 4000)
        else:
            self.status_bar.showMessage("Otomatik sınıflandırmaya dönüldü.", 4000)

    def _mark_all_visible_read(self) -> None:
        vids = self.middle.model.all_video_ids()
        if not vids:
            return
        self.db.set_read(vids, True)
        self.middle.reload(preserve_selection=True)
        self.sidebar.reload()
        if self.detail.current_video_id():
            self.detail.show_video(self.detail.current_video_id())

    def _mark_node_read(self, node_id: str) -> None:
        vis = self.db.visibility_defaults()
        sql, params = build_list_sql(ListQuery(node=node_id), vis)
        vids = [r["video_id"] for r in self.db.q(sql, params)]
        if vids:
            self.db.set_read(vids, True)
            self.middle.reload(preserve_selection=True)
            self.sidebar.reload()
            if self.detail.current_video_id():
                self.detail.show_video(self.detail.current_video_id())

    # --- Kanal ve grup yönetimi ---------------------------------------------
    def _prompt_add_group(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(
            self, "Yeni Kanal Grubu", "Grup adı:"
        )
        if ok and name.strip():
            try:
                self.db.add_group(name.strip())
                self.sidebar.reload()
            except Exception as e:  # noqa: BLE001
                QtWidgets.QMessageBox.warning(self, "Grup Eklenemedi", str(e))

    def _prompt_add_channel(self) -> None:
        if self._add_worker is not None and self._add_worker.isRunning():
            QtWidgets.QMessageBox.information(
                self, "İşlem Sürüyor", "Şu anda bir kanal ekleniyor; lütfen bekleyin."
            )
            return

        dlg = AddChannelDialog(self.db, self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        raw_url, group_id, prefs = dlg.values()
        try:
            parse_channel_input(raw_url)
        except InvalidChannelInput as e:
            QtWidgets.QMessageBox.warning(self, "Geçersiz Kanal Adresi", str(e))
            return

        self.status_bar.showMessage(f"Kanal çözümleniyor ve eşitleniyor: {raw_url} …")
        self.sidebar.btn_add_channel.setEnabled(False)
        self._add_worker = AddChannelWorker(self.db, self.source, raw_url, group_id, prefs, self)
        self._add_worker.succeeded.connect(self._on_add_channel_succeeded)
        self._add_worker.failed.connect(self._on_add_channel_failed)
        self._add_worker.start()

    def _on_add_channel_succeeded(self, resolved, is_new: bool, sync_res: ChannelSyncResult | None) -> None:
        self.sidebar.btn_add_channel.setEnabled(True)
        self.sidebar.select_node(f"channel:{resolved.channel_id}")
        self.middle.reload(preserve_selection=False)
        first_vid = self.middle.select_first_video()
        if first_vid:
            self.detail.show_video(first_vid)
        if not is_new:
            self.status_bar.showMessage(
                f"'{resolved.title}' ({resolved.channel_id}) zaten ekliydi; içerikler güncellendi.",
                6000,
            )
            QtWidgets.QMessageBox.information(
                self,
                "Kanal Zaten Mevcut",
                f"<b>{resolved.title}</b> (<code>{resolved.channel_id}</code>) "
                "daha önce eklenmiş. Tekrar kayıt oluşturulmadı ve mevcut kanal güncellendi.",
            )
        else:
            n_new = sync_res.new_count if sync_res else 0
            self.status_bar.showMessage(
                f"'{resolved.title}' eklendi ({n_new} içerik alındı).", 6000
            )

    def _on_add_channel_failed(self, err_msg: str) -> None:
        self.sidebar.btn_add_channel.setEnabled(True)
        self.status_bar.showMessage(f"Kanal eklenemedi: {err_msg}", 6000)
        QtWidgets.QMessageBox.warning(self, "Kanal Eklenemedi", err_msg)

    def _prompt_import_subscriptions(self) -> None:
        if self._import_worker is not None and self._import_worker.isRunning():
            QtWidgets.QMessageBox.information(
                self, "Aktarım Sürüyor", "Şu anda bir abonelik aktarımı devam ediyor."
            )
            return
        dlg = ImportSubscriptionsDialog(self.db, self)
        dlg.backupRestored.connect(self._on_backup_restored)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        chosen, group_id, prefs = dlg.selected_values()
        if not chosen:
            return
        self.sidebar.btn_import.setEnabled(False)
        self.status_bar.showMessage(f"{len(chosen)} kanal içe aktarılıyor…")
        self._import_worker = ImportSubscriptionsWorker(
            self.db, self.source, chosen, group_id, prefs, self
        )
        self._import_worker.progress.connect(
            lambda cur, tot, name: self.status_bar.showMessage(
                f"Abonelikler aktarılıyor ({cur}/{tot}): {name}"
            )
        )
        self._import_worker.finishedImport.connect(self._on_import_finished)
        self._import_worker.start()

    def _on_backup_restored(self, _res) -> None:
        self.sidebar.reload()
        self.middle.reload(preserve_selection=True)
        if self.detail.current_video_id():
            self.detail.show_video(self.detail.current_video_id())
        self.status_bar.showMessage("Sürümlü yedek başarıyla birleştirilerek geri yüklendi.", 6000)

    def _on_import_finished(self, summary: ImportSummary) -> None:
        self.sidebar.btn_import.setEnabled(True)
        self.sidebar.reload()
        self.middle.reload(preserve_selection=True)
        msg = (
            f"İçe aktarma tamamlandı: {summary.added} yeni kanal eklendi, "
            f"{summary.skipped_existing} mevcut kanal korundu"
        )
        if summary.failed:
            msg += f", {summary.failed} hatalı"
        self.status_bar.showMessage(msg + ".", 8000)
        if summary.added_channel_ids:
            self.start_sync(summary.added_channel_ids)

    def _open_channel_settings(self, channel_id: str) -> None:
        if self.db.channel(channel_id) is None:
            return
        dlg = ChannelSettingsDialog(self.db, channel_id, self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.sidebar.reload()
            self.middle.reload(preserve_selection=True)
            if self.detail.current_video_id():
                self.detail.show_video(self.detail.current_video_id())
            self.status_bar.showMessage("Kanal görünürlük ve bildirim ayarları kaydedildi.", 4000)

    def _confirm_delete_channel(self, channel_id: str) -> None:
        ch = self.db.channel(channel_id)
        if ch is None:
            return
        ans = QtWidgets.QMessageBox.question(
            self,
            "Kanalı Kaldır",
            f"<b>{ch['title']}</b> kanalını ve yerel içerik kayıtlarını kaldırmak istiyor musunuz?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if ans == QtWidgets.QMessageBox.StandardButton.Yes:
            self.db.delete_channel(channel_id)
            if self.sidebar.current_node() == f"channel:{channel_id}":
                self.sidebar.select_node("all")
            else:
                self.sidebar.reload()
                self.middle.reload(preserve_selection=False)
            self.detail.clear_selection()

    def _open_global_settings(self) -> None:
        dlg = GlobalSettingsDialog(self.db, self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._apply_auto_refresh_timer()
            self._apply_tray_settings()
            self.thumbs.set_max_disk_mb(int(self.db.get_setting("thumb_cache_mb") or 200))
            self.sidebar.reload()
            self.middle.reload(preserve_selection=True)
            self.status_bar.showMessage("Genel ayarlar güncellendi.", 4000)

    def _apply_auto_refresh_timer(self) -> None:
        mins = int(self.db.get_setting("auto_refresh_minutes") or 0)
        if mins > 0:
            self.auto_refresh_timer.start(mins * 60 * 1000)
        else:
            self.auto_refresh_timer.stop()

    # --- Eşitleme, tekil üstveri ve arka plan süre tamamlama ----------------
    def _on_refresh_button(self) -> None:
        if self._sync_worker is not None and self._sync_worker.isRunning():
            self._sync_worker.cancel()
            self.status_bar.showMessage("Yenileme durduruluyor…")
        else:
            self.start_sync(None)

    def start_sync(self, channel_ids: list[str] | None = None) -> None:
        if self._sync_worker is not None and self._sync_worker.isRunning():
            return
        all_ch = [r["channel_id"] for r in self.db.channels()]
        targets = channel_ids if channel_ids is not None else all_ch
        if not targets:
            self.status_bar.showMessage("Henüz eklenmiş kanal yok. '+ Kanal Ekle' ile başlayın.", 4000)
            return

        self.sidebar.btn_refresh.setText("✕ Durdur")
        self.status_bar.showMessage(f"{len(targets)} kanal yenileniyor…")
        self._sync_worker = SyncWorker(self.db, self.source, targets, self)
        self._sync_worker.channelDone.connect(self._on_channel_synced)
        self._sync_worker.syncFinished.connect(self._on_sync_finished)
        self._sync_worker.start()

    def _on_channel_synced(self, res: ChannelSyncResult) -> None:
        self.sidebar.reload()
        self.middle.reload(preserve_selection=True)
        if self.detail.current_video_id():
            self.detail.show_video(self.detail.current_video_id())

    def _on_sync_finished(self, summary: SyncSummary) -> None:
        self.sidebar.btn_refresh.setText("↻ Yenile")
        self.sidebar.reload()
        self.middle.reload(preserve_selection=True)
        if summary.all_notifications:
            self._dispatch_notifications(summary.all_notifications)
        if summary.cancelled:
            self.status_bar.showMessage("Yenileme kullanıcı tarafından durduruldu.", 5000)
        elif summary.error_count:
            self.status_bar.showMessage(
                f"Yenileme tamamlandı: {summary.total_new} yeni içerik, "
                f"{summary.error_count} kanalda hata (sol sütundaki ⚠ işaretine bakın).",
                8000,
            )
        else:
            self.status_bar.showMessage(
                f"Yenileme tamamlandı: {summary.total_new} yeni içerik.", 5000
            )

    def _recheck_single_video(self, video_id: str) -> None:
        if self._meta_worker is not None and self._meta_worker.isRunning():
            return
        self.status_bar.showMessage(f"Bilgiler yenileniyor ({video_id})…")
        self._meta_worker = SingleMetaWorker(self.db, self.source, [video_id], self)
        self._meta_worker.finishedVideo.connect(self._on_single_meta_done)
        self._meta_worker.start()

    def _check_due_live_items(self) -> None:
        if (self._sync_worker and self._sync_worker.isRunning()) or (
            self._meta_worker and self._meta_worker.isRunning()
        ):
            return
        now = int(time.time())
        due = self.db.due_meta_checks(now, limit=3)
        if not due:
            return
        vids = [r["video_id"] for r in due]
        self._meta_worker = SingleMetaWorker(self.db, self.source, vids, self)
        self._meta_worker.finishedVideo.connect(self._on_single_meta_done)
        self._meta_worker.start()

    def _on_single_meta_done(
        self, video_id: str, msg: str, notif: NotificationEvent | None = None
    ) -> None:
        self.sidebar.reload()
        self.middle.reload(preserve_selection=True)
        if self.detail.current_video_id() == video_id:
            self.detail.show_video(video_id)
        if notif is not None:
            self._dispatch_notifications([notif])
        self.status_bar.showMessage(msg, 4000)

    def _schedule_duration_backfill(self, video_ids: list[str]) -> None:
        """Görünür listede süresi eksik olan videoları arayüzü dondurmadan arka planda tamamlar."""
        if not isinstance(self.source, LiveSource):
            return
        if (
            (self._sync_worker and self._sync_worker.isRunning())
            or (self._meta_worker and self._meta_worker.isRunning())
            or (self._duration_worker and self._duration_worker.isRunning())
        ):
            return
        due_missing = self.db.videos_missing_duration(video_ids, limit=3)
        if not due_missing:
            return
        target_ids = [r["video_id"] for r in due_missing]
        self._duration_worker = DurationBackfillWorker(self.db, self.source, target_ids, self)
        self._duration_worker.durationsUpdated.connect(self._on_durations_backfilled)
        self._duration_worker.start()

    def _on_durations_backfilled(self, updated_ids: list[str]) -> None:
        if not updated_ids:
            return
        self.middle.reload(preserve_selection=True)
        cur = self.detail.current_video_id()
        if cur and cur in updated_ids:
            self.detail.show_video(cur)
