"""XDG uyumlu kullanıcı dizinleri.

Veri:   $XDG_DATA_HOME/ytrss   (varsayılan ~/.local/share/ytrss)
Önbellek: $XDG_CACHE_HOME/ytrss (varsayılan ~/.cache/ytrss)
Ayar (QSettings, yalnızca pencere durumu): $XDG_CONFIG_HOME/ytrss

YTRSS_HOME ortam değişkeni verilirse hepsi bu dizinin altına alınır (testler ve
deneme kurulumları için).
"""
from __future__ import annotations

import os
from pathlib import Path

from . import APP_ID


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var)
    if value and os.path.isabs(value):
        return Path(value)
    return Path.home() / fallback


def _root_override() -> Path | None:
    value = os.environ.get("YTRSS_HOME")
    return Path(value) if value else None


def data_dir() -> Path:
    root = _root_override()
    p = (root / "data") if root else _xdg("XDG_DATA_HOME", ".local/share") / APP_ID
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    root = _root_override()
    p = (root / "cache") if root else _xdg("XDG_CACHE_HOME", ".cache") / APP_ID
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    root = _root_override()
    p = (root / "config") if root else _xdg("XDG_CONFIG_HOME", ".config") / APP_ID
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return data_dir() / "ytrss.sqlite3"


def thumb_cache_dir() -> Path:
    p = cache_dir() / "thumbs"
    p.mkdir(parents=True, exist_ok=True)
    return p

