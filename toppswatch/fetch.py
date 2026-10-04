"""HTTP fetching with polite retries, jitter and optional TLS impersonation.

If `curl_cffi` is installed the client uses it to mimic a real Chrome TLS
fingerprint, which is what you want if Topps ever puts the storefront behind a
bot-detection layer. Plain `requests` is used otherwise.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import requests

try:
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover
    curl_requests = None

log = logging.getLogger("toppswatch.fetch")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}


class RateLimited(Exception):
    """Raised on 429 / 403 so the caller can back the whole cycle off."""

    def __init__(self, status: int, retry_after: float | None = None):
        super().__init__(f"blocked with HTTP {status}")
        self.status = status
        self.retry_after = retry_after


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    elapsed: float


class Fetcher:
    def __init__(
        self,
        timeout: float = 20.0,
        retries: int = 3,
        backoff: float = 2.0,
        impersonate: str | None = None,
        proxy: str | None = None,
        extra_headers: dict | None = None,
    ):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.proxy = proxy
        self.headers = {**DEFAULT_HEADERS, **(extra_headers or {})}

        self.impersonate = impersonate
        if impersonate and curl_requests is None:
            log.warning(
                "impersonate=%s requested but curl_cffi is not installed; "
                "falling back to requests",
                impersonate,
            )
            self.impersonate = None

        if self.impersonate:
            self.session = curl_requests.Session()
        else:
            self.session = requests.Session()
        self.session.headers.update(self.headers)

    def _do_get(self, url: str):
        kwargs = {"timeout": self.timeout}
        if self.proxy:
            kwargs["proxies"] = {"http": self.proxy, "https": self.proxy}
        if self.impersonate:
            kwargs["impersonate"] = self.impersonate
        return self.session.get(url, **kwargs)

    def get(self, url: str) -> FetchResult:
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            started = time.monotonic()
            try:
                response = self._do_get(url)
                elapsed = time.monotonic() - started

                if response.status_code in (403, 429):
                    retry_after = response.headers.get("Retry-After")
                    raise RateLimited(
                        response.status_code,
                        float(retry_after) if retry_after and retry_after.isdigit() else None,
                    )

                if response.status_code >= 500:
                    raise requests.HTTPError(f"HTTP {response.status_code}")

                if response.status_code == 404:
                    log.info("404 for %s (product may have been delisted)", url)

                return FetchResult(
                    url=url,
                    status=response.status_code,
                    text=response.text,
                    elapsed=elapsed,
                )

            except RateLimited:
                raise
            except Exception as exc:  # noqa: BLE001 - retry anything transient
                last_error = exc
                if attempt < self.retries:
                    delay = self.backoff ** attempt + random.uniform(0, 1.0)
                    log.debug(
                        "fetch %s failed (%s), retry %d/%d in %.1fs",
                        url, exc, attempt, self.retries, delay,
                    )
                    time.sleep(delay)

        raise RuntimeError(f"failed to fetch {url} after {self.retries} attempts: {last_error}")

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # pragma: no cover
            pass
