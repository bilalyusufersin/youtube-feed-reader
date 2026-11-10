#!/usr/bin/env python3
"""Install the Linux source bundle into the current user's application directory."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv


def main() -> int:
    parser = argparse.ArgumentParser(description="YouTube Akış Okuyucu — Linux kurulumu")
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    parser.add_argument("--prefix", type=Path, default=data_home / "youtube-feed-reader", help="Kurulum klasörü")
    parser.add_argument("--no-desktop", action="store_true", help="Menü/masaüstü kısayolu oluşturma")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("Bu sürüm yalnızca Linux için hazırlanmıştır.")
    if sys.version_info < (3, 10):
        parser.error("Python 3.10 veya daha yeni bir sürüm gereklidir.")
    if os.geteuid() == 0:
        parser.error("Bu kullanıcı kurulumunu sudo olmadan çalıştırın.")

    source = Path(__file__).resolve().parents[1]
    target = args.prefix.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    print(f"Kurulum klasörü: {target}", flush=True)
    if target != source:
        for relative in ["main.py", "requirements-runtime.txt", "README.md", "INSTALL_LINUX.md"]:
            shutil.copy2(source / relative, target / relative)
        shutil.copytree(source / "ytrss", target / "ytrss", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(source / "docs", target / "docs", dirs_exist_ok=True)

    environment = target / ".venv"
    print("Python ortamı hazırlanıyor…", flush=True)
    # Existing system Qt packages can be reused; missing dependencies stay in the venv.
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
    python = environment / "bin/python"
    print("Uygulama bağımlılıkları yükleniyor (internet bağlantısı gerekir)…", flush=True)
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(target / "requirements-runtime.txt")], check=True)
    if not args.no_desktop:
        print("Uygulama menüsü ve masaüstü kısayolu hazırlanıyor…", flush=True)
        subprocess.run([str(python), str(target / "main.py"), "--install-desktop"], check=True)
    (target / "installation.json").write_text(json.dumps({"source": str(source), "installation": str(target)}, indent=2))
    print("Kurulum tamamlandı." if args.no_desktop else "Kurulum tamamlandı. Menüde: YouTube Akış Okuyucu", flush=True)
    print(f'Elle çalıştırma: "{python}" "{target / "main.py"}"', flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
