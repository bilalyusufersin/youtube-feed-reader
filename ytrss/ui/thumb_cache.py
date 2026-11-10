"""İhtiyaç oldukça yüklenen (lazy), disk önbellekli ve boyut sınırlı küçük resim yöneticisi.

- Görüntü oranını korur; dikey (Shorts) küçük resimleri asla yatay çerçeveye esnetmez.
- Ağ indirmelerini arka plan iş parçacığı havuzunda yapar ve hazır olduğunda Qt sinyali üretir.
"""
from __future__ import annotations

import hashlib
import logging
import os
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from PySide6 import QtCore, QtGui

from ..paths import thumb_cache_dir
from ..youtube.http import DEFAULT_HEADERS

log = logging.getLogger(__name__)


def render_fitted_pixmap(
    src: QtGui.QPixmap | None,
    width: int,
    height: int,
    *,
    radius: int = 6,
    bg_color: QtGui.QColor = QtGui.QColor("#121418"),
) -> QtGui.QPixmap:
    """Kaynağı görüntü oranını bozmadan (`KeepAspectRatio`) hedef kutuya yerleştirir.

    Dikey Shorts görselleri veya 4:3 küçük resimler esnetilmez; yanlarda/üst-altta
    koyu mat arka plan bırakılır.
    """
    out = QtGui.QPixmap(width, height)
    out.fill(QtCore.Qt.GlobalColor.transparent)

    painter = QtGui.QPainter(out)
    painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)

    path = QtGui.QPainterPath()
    path.addRoundedRect(QtCore.QRectF(0, 0, width, height), radius, radius)
    painter.setClipPath(path)
    painter.fillRect(0, 0, width, height, bg_color)

    if src is not None and not src.isNull():
        scaled = src.scaled(
            width,
            height,
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation,
        )
        x = (width - scaled.width()) // 2
        y = (height - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)

    painter.end()
    return out


class ThumbnailCache(QtCore.QObject):
    thumbnailReady = QtCore.Signal(str)  # url

    def __init__(self, max_disk_mb: int = 200, max_mem_items: int = 256, parent=None) -> None:
        super().__init__(parent)
        self._dir: Path = thumb_cache_dir()
        self._max_disk_bytes = max(20, max_disk_mb) * 1024 * 1024
        self._max_mem_items = max_mem_items
        self._mem: OrderedDict[str, QtGui.QPixmap] = OrderedDict()
        self._fitted_mem: OrderedDict[tuple[str, int, int], QtGui.QPixmap] = OrderedDict()
        self._in_flight: set[str] = set()
        self._failed: set[str] = set()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ytrss-thumb")
        self._http = httpx.Client(
            headers=DEFAULT_HEADERS,
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=True,
        )
        self._writes_since_prune = 0

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass

    def set_max_disk_mb(self, mb: int) -> None:
        self._max_disk_bytes = max(20, int(mb)) * 1024 * 1024
        self._executor.submit(self._prune_disk)

    def _disk_file(self, url: str) -> Path:
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.img"

    def get_or_fetch(self, url: str | None) -> QtGui.QPixmap | None:
        if not url:
            return None
        if url in self._mem:
            self._mem.move_to_end(url)
            return self._mem[url]

        path = self._disk_file(url)
        if path.is_file():
            pix = QtGui.QPixmap(str(path))
            if not pix.isNull():
                self._store_mem(url, pix)
                return pix

        if url not in self._in_flight and url not in self._failed:
            self._in_flight.add(url)
            self._executor.submit(self._download, url, path)
        return None

    def get_fitted(self, url: str | None, width: int, height: int, radius: int = 6) -> QtGui.QPixmap:
        if url:
            key = (url, width, height)
            if key in self._fitted_mem:
                self._fitted_mem.move_to_end(key)
                return self._fitted_mem[key]
            raw = self.get_or_fetch(url)
            if raw is not None:
                fitted = render_fitted_pixmap(raw, width, height, radius=radius)
                self._fitted_mem[key] = fitted
                while len(self._fitted_mem) > self._max_mem_items * 2:
                    self._fitted_mem.popitem(last=False)
                return fitted
        return render_fitted_pixmap(None, width, height, radius=radius)

    def _store_mem(self, url: str, pix: QtGui.QPixmap) -> None:
        self._mem[url] = pix
        while len(self._mem) > self._max_mem_items:
            self._mem.popitem(last=False)

    def _download(self, url: str, path: Path) -> None:
        try:
            resp = self._http.get(url)
            if resp.status_code == 200 and resp.content:
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(resp.content)
                tmp.replace(path)
                self._writes_since_prune += 1
                if self._writes_since_prune >= 25:
                    self._writes_since_prune = 0
                    self._prune_disk()
                self.thumbnailReady.emit(url)
            else:
                self._failed.add(url)
        except Exception as e:  # noqa: BLE001
            log.debug("Küçük resim indirilemedi (%s): %s", url, e)
            self._failed.add(url)
        finally:
            self._in_flight.discard(url)

    def _prune_disk(self) -> None:
        try:
            files = []
            total = 0
            for entry in os.scandir(self._dir):
                if entry.is_file() and entry.name.endswith(".img"):
                    st = entry.stat()
                    files.append((st.st_mtime, st.st_size, entry.path))
                    total += st.st_size
            if total <= self._max_disk_bytes:
                return
            files.sort(key=lambda x: x[0])  # en eski önce silinir
            target = int(self._max_disk_bytes * 0.8)
            for _, size, fpath in files:
                try:
                    os.unlink(fpath)
                    total -= size
                except OSError:
                    pass
                if total <= target:
                    break
        except Exception as e:  # noqa: BLE001
            log.debug("Önbellek temizleme hatası: %s", e)

