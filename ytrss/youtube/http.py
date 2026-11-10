"""Paylaşılan HTTP istemcisi: zaman aşımı, sınırlı tekrar, 429 bekleme, iptal."""
from __future__ import annotations

import logging
import random
import threading
import time
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
}


class Cancelled(Exception):
    """Kullanıcı yenilemeyi iptal etti."""


class NetError(Exception):
    """Ağ/HTTP hatası. kind: network | http | rate_limited | not_found"""

    def __init__(self, message: str, kind: str = "network", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class RateLimiter:
    """YouTube 429 döndürdüğünde tüm istekleri ortak bir süre bekletir."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0

    def penalize(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, time.monotonic() + seconds)
        log.warning("Hız sınırı: %.0f sn bekleniyor", seconds)

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._until - time.monotonic())

    def wait(self, cancel: threading.Event | None, max_wait: float = 120.0) -> None:
        rem = self.remaining()
        if rem <= 0:
            return
        if rem > max_wait:
            raise NetError(f"YouTube hız sınırı uyguladı; {int(rem)} sn sonra tekrar denenecek.",
                           kind="rate_limited", status=429)
        end = time.monotonic() + rem
        while time.monotonic() < end:
            if cancel is not None and cancel.wait(0.25):
                raise Cancelled()
            elif cancel is None:
                time.sleep(0.25)


RATE_LIMITER = RateLimiter()


def _retry_after(resp: httpx.Response) -> float:
    ra = resp.headers.get("Retry-After")
    if ra:
        try:
            return max(1.0, float(ra))
        except ValueError:
            try:
                dt = parsedate_to_datetime(ra)
                return max(1.0, dt.timestamp() - time.time())
            except Exception:  # noqa: BLE001
                pass
    return 60.0


class Http:
    def __init__(self, timeout: float = 20.0, retries: int = 2,
                 limiter: RateLimiter = RATE_LIMITER) -> None:
        self.client = httpx.Client(
            headers=DEFAULT_HEADERS,
            timeout=httpx.Timeout(timeout, connect=10.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
            follow_redirects=False,
            http2=False,
        )
        self.retries = retries
        self.limiter = limiter

    def close(self) -> None:
        self.client.close()

    def request(self, method: str, url: str, *, cancel: threading.Event | None = None,
                follow_redirects: bool = True, ok_statuses: tuple[int, ...] = (200,),
                **kw) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            self.limiter.wait(cancel)
            try:
                resp = self.client.request(method, url, follow_redirects=follow_redirects, **kw)
            except httpx.TimeoutException as e:
                last_exc = NetError("Bağlantı zaman aşımına uğradı.", "network")
                log.info("timeout %s (%s)", url, e)
            except httpx.TransportError as e:
                last_exc = NetError(f"Ağ hatası: {e.__class__.__name__}", "network")
                log.info("transport error %s: %s", url, e)
            else:
                if resp.status_code in ok_statuses:
                    return resp
                if resp.status_code == 429:
                    self.limiter.penalize(_retry_after(resp))
                    last_exc = NetError("YouTube hız sınırı (429).", "rate_limited", 429)
                    continue
                if resp.status_code in (404, 410):
                    raise NetError(f"Bulunamadı (HTTP {resp.status_code}).", "not_found",
                                   resp.status_code)
                if 500 <= resp.status_code < 600:
                    last_exc = NetError(f"Sunucu hatası (HTTP {resp.status_code}).", "http",
                                        resp.status_code)
                else:
                    raise NetError(f"Beklenmeyen yanıt (HTTP {resp.status_code}).", "http",
                                   resp.status_code)
            if attempt < self.retries:
                delay = (1.5 ** attempt) + random.uniform(0, 0.7)
                if cancel is not None:
                    if cancel.wait(delay):
                        raise Cancelled()
                else:
                    time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

