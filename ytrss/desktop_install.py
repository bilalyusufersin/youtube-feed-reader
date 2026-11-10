"""Fedora KDE/Wayland masaüstü uygulaması kurulum aracısı.

Uygulamayı terminal açmadan doğrudan KDE Uygulama Menüsü (KRunner / Kickoff),
görev çubuğu ve Masaüstü kısayolundan çalıştırılabilir bağımsız bir masaüstü
uygulaması olarak kurar:
- `~/.local/bin/ytrss` başlatıcı betiği
- `~/.local/share/applications/ytrss.desktop` uygulama tanımı
- `~/.local/share/icons/hicolor/.../apps/ytrss.(svg|png)` uygulama simgeleri
- `~/Masaüstü/YouTube Akış Okuyucu.desktop` masaüstü kısayolu
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import APP_ID, APP_NAME

ASSET_SVG = Path(__file__).resolve().parent / "assets" / "ytrss.svg"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def app_icon() -> QtGui.QIcon:
    if ASSET_SVG.is_file():
        return QtGui.QIcon(str(ASSET_SVG))
    return QtGui.QIcon.fromTheme(APP_ID)


def _render_icon_png(svg_path: Path, size: int, dest: Path) -> None:
    app = QtWidgets.QApplication.instance()
    created_app = False
    if app is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QtWidgets.QApplication([])
        created_app = True

    icon = QtGui.QIcon(str(svg_path))
    pix = icon.pixmap(QtCore.QSize(size, size))
    dest.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(dest), "PNG")
    if created_app:
        app.quit()


def install_desktop_integration(*, create_desktop_shortcut: bool = True) -> dict[str, str]:
    home = Path.home()
    bin_dir = home / ".local" / "bin"
    apps_dir = home / ".local" / "share" / "applications"
    icons_root = home / ".local" / "share" / "icons" / "hicolor"

    bin_dir.mkdir(parents=True, exist_ok=True)
    apps_dir.mkdir(parents=True, exist_ok=True)

    # 1. Simgeleri (SVG + PNG boyutları) kur
    svg_dest = icons_root / "scalable" / "apps" / f"{APP_ID}.svg"
    svg_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ASSET_SVG, svg_dest)

    for sz in (48, 64, 128, 256):
        png_dest = icons_root / f"{sz}x{sz}" / "apps" / f"{APP_ID}.png"
        _render_icon_png(ASSET_SVG, sz, png_dest)

    # 2. Bağımsız çalıştırıcı betiği (~/.local/bin/ytrss)
    venv_python = PROJECT_ROOT / ".venv" / "bin" / "python"
    python_exec = str(venv_python if venv_python.is_file() else Path("/usr/bin/python3"))
    main_script = str(PROJECT_ROOT / "main.py")

    launcher_path = bin_dir / APP_ID
    launcher_content = f"""#!/bin/sh
# {APP_NAME} bağımsız masaüstü başlatıcısı
export PYTHONPATH="{PROJECT_ROOT}${{PYTHONPATH:+:$PYTHONPATH}}"
exec "{python_exec}" "{main_script}" "$@"
"""
    launcher_path.write_text(launcher_content, encoding="utf-8")
    launcher_path.chmod(
        launcher_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    # 3. Freedesktop .desktop dosyası
    desktop_content = f"""[Desktop Entry]
Version=1.0
Type=Application
Name={APP_NAME}
GenericName=YouTube RSS ve Kanal Takipçisi
Comment=YouTube kanallarını normal video, Shorts ve canlı yayın ayrımıyla takip edin
Exec={launcher_path} %U
TryExec={launcher_path}
Icon={APP_ID}
Terminal=false
Categories=Network;News;Feed;AudioVideo;Qt;
Keywords=YouTube;RSS;Atom;Shorts;Canlı;Kanal;NewsFlash;
StartupNotify=true
StartupWMClass={APP_ID}
MimeType=text/x-opml;application/xml;
"""
    desktop_file = apps_dir / f"{APP_ID}.desktop"
    desktop_file.write_text(desktop_content, encoding="utf-8")
    desktop_file.chmod(
        desktop_file.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    # 4. Masaüstü kısayolu (~/Masaüstü)
    shortcut_path_str = ""
    if create_desktop_shortcut:
        for d_name in ("Masaüstü", "Desktop"):
            d_dir = home / d_name
            if d_dir.is_dir():
                sc_path = d_dir / f"{APP_NAME}.desktop"
                sc_path.write_text(desktop_content, encoding="utf-8")
                sc_path.chmod(
                    sc_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
                )
                shortcut_path_str = str(sc_path)
                break

    # 5. KDE Plasma ve XDG önbelleklerini yenile
    for cmd in (
        ["update-desktop-database", str(apps_dir)],
        ["gtk-update-icon-cache", "-f", "-t", str(icons_root)],
        ["kbuildsycoca6", "--noincremental"],
    ):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except Exception:  # noqa: BLE001
                pass

    return {
        "launcher": str(launcher_path),
        "desktop_file": str(desktop_file),
        "icon_svg": str(svg_dest),
        "desktop_shortcut": shortcut_path_str,
    }

