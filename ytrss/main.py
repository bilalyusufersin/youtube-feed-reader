"""Uygulama giriş noktası."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6 import QtCore, QtWidgets

from . import APP_ID, APP_NAME, __version__
from .db import Database
from .desktop_install import app_icon, install_desktop_integration
from .paths import db_path
from .subscriptions import import_candidates_to_db, parse_subscriptions_file
from .sync import add_channel, sync_channel
from .ui.main_window import MainWindow
from .ui.theme import apply_dark_theme
from .youtube.source import LiveSource


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=APP_NAME)
    p.add_argument(
        "files",
        nargs="*",
        help="İçe aktarılacak abonelik dosyası (.csv, .opml, .xml, .json)",
    )
    p.add_argument(
        "--db",
        help="Özel SQLite veritabanı yolu (varsayılan: XDG veri dizini)",
        default=None,
    )
    p.add_argument(
        "--add",
        action="append",
        default=[],
        metavar="KANAL",
        help="Başlangıçta belirtilen kanalı (@kullanıcı, /channel/UC… veya RSS) ekle ve eşitle",
    )
    p.add_argument(
        "--import-file",
        default=None,
        metavar="DOSYA",
        help="Belirtilen dosyadan (Google Takeout CSV/JSON, OPML) abonelikleri içe aktar",
    )
    p.add_argument(
        "--install-desktop",
        action="store_true",
        help="Uygulamayı KDE/Linux uygulama menüsüne ve masaüstüne kur ve çık",
    )
    p.add_argument(
        "--no-auto-sync",
        action="store_true",
        help="Açılışta otomatik arka plan yenilemesini devre dışı bırak",
    )
    p.add_argument(
        "--select-first",
        action="store_true",
        help="Açılışta listedeki ilk içeriği seçili göster",
    )
    p.add_argument(
        "--screenshot",
        default=None,
        metavar="DOSYA.png",
        help="Pencereyi açtıktan sonra belirtilen dosyaya ekran görüntüsü kaydet ve çık",
    )
    p.add_argument(
        "--screenshot-delay-ms",
        type=int,
        default=1800,
        help="--screenshot için bekleme süresi (ms)",
    )
    p.add_argument("--debug", action="store_true", help="Ayrıntılı günlük çıktısı")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.install_desktop:
        info = install_desktop_integration(create_desktop_shortcut=True)
        for k, v in info.items():
            print(f"{k}: {v}")
        return 0

    database = Database(args.db or db_path())
    source = LiveSource()

    import_targets = list(args.files or [])
    if args.import_file:
        import_targets.append(args.import_file)
    for fpath in import_targets:
        p = Path(fpath)
        if p.is_file():
            cands = parse_subscriptions_file(p)
            summary = import_candidates_to_db(database, source, cands)
            logging.info(
                "Dosyadan aktarıldı (%s): %d yeni, %d mevcut",
                p.name, summary.added, summary.skipped_existing,
            )

    if args.add:
        for ch_input in args.add:
            resolved, is_new = add_channel(database, source, ch_input)
            logging.info("Kanal hazır: %s (%s, yeni=%s)", resolved.title, resolved.channel_id, is_new)
            sync_channel(database, source, resolved.channel_id, meta_budget=8, force_tabs=True)

    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName(APP_ID)
    icon = app_icon()
    app.setWindowIcon(icon)

    # Sistem açık temada olsa bile ilk açılışta koyu tema ile başla
    apply_dark_theme(app)

    win = MainWindow(
        database,
        source=source,
        auto_start_sync=not (args.no_auto_sync or bool(args.add)),
    )
    win.setWindowIcon(icon)
    win.show()

    if args.select_first or args.screenshot:
        first_vid = win.middle.select_first_video()
        if first_vid:
            win.detail.show_video(first_vid)

    if args.screenshot:
        def _capture() -> None:
            win.repaint()
            QtWidgets.QApplication.processEvents()
            pix = win.grab()
            pix.save(args.screenshot, "PNG")
            logging.info("Ekran görüntüsü kaydedildi: %s", args.screenshot)
            win.close()
            app.quit()

        QtCore.QTimer.singleShot(args.screenshot_delay_ms, _capture)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
