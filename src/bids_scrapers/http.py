"""A deliberately slow, well-behaved HTTP client shared by all collectors.

Rules it enforces:
  * one request at a time, with a random pause between requests,
  * a longer break every N requests,
  * retries with exponential backoff on network errors / 5xx, honouring Retry-After on 429/503,
  * hard stop (Blocked) on 401/403 or when the server asks us to wait too long,
    so a run never keeps knocking on a door that has been closed.
"""

from __future__ import annotations

import email.utils
import logging
import random
import time

import requests

from .config import HttpConfig

log = logging.getLogger(__name__)


class Blocked(RuntimeError):
    """The server refused us. Stop the run and investigate; do not retry automatically."""


class GaveUp(RuntimeError):
    """Too many consecutive failures for one request."""


def _retry_after_seconds(resp: requests.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


class PoliteClient:
    def __init__(self, cfg: HttpConfig, user_agent: str, *, accept: str = "*/*"):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": accept,
                "Accept-Language": "cs,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self.n_requests = 0
        self._last_request = 0.0

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _pace(self) -> None:
        if self.n_requests and self.cfg.long_pause_every and self.n_requests % self.cfg.long_pause_every == 0:
            pause = random.uniform(self.cfg.long_pause_min, self.cfg.long_pause_max)
            log.info("Taking a %.0f s break after %d requests", pause, self.n_requests)
            time.sleep(pause)
        delay = random.uniform(self.cfg.min_delay, self.cfg.max_delay)
        wait = self._last_request + delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def _respect_rate_limit(self, resp: requests.Response) -> None:
        """If the server announces a quota (X-RateLimit-*) that is nearly used up, wait for the reset
        instead of running into 429s."""
        remaining = resp.headers.get("X-RateLimit-Remaining", "")
        if not remaining.isdigit() or int(remaining) > 2:
            return
        reset = resp.headers.get("X-RateLimit-Reset", "")
        wait = 60.0
        if reset.isdigit():
            value = float(reset)
            wait = value - time.time() if value > 1e9 else value  # epoch timestamp or seconds
        wait = min(max(wait, 5.0), self.cfg.max_retry_after)
        log.info("Rate limit nearly used up (%s left); pausing %.0f s", remaining, wait)
        time.sleep(wait)

    def get(
        self,
        url: str,
        *,
        params: dict | str | None = None,
        headers: dict | None = None,
        stream: bool = False,
    ) -> requests.Response:
        """GET with pacing and retries. Returns any final response that is not a retryable error
        (callers check e.g. 200/304/404 themselves). Raises Blocked or GaveUp."""
        last_error: str = ""
        for attempt in range(self.cfg.max_retries + 1):
            self._pace()
            self._last_request = time.monotonic()
            try:
                resp = self.session.get(url, params=params, headers=headers, timeout=self.cfg.timeout, stream=stream)
            except (requests.ConnectionError, requests.Timeout) as exc:
                self.n_requests += 1
                last_error = f"{type(exc).__name__}: {exc}"
                backoff = min(600.0, 15.0 * 2**attempt) * random.uniform(0.8, 1.2)
                log.warning("%s -> %s; retry %d in %.0f s", url, last_error, attempt + 1, backoff)
                time.sleep(backoff)
                continue
            self.n_requests += 1

            if resp.status_code in (401, 403):
                resp.close()
                raise Blocked(f"HTTP {resp.status_code} for {resp.url}")
            if resp.status_code == 429 or resp.status_code >= 500:
                resp.close()
                last_error = f"HTTP {resp.status_code}"
                wait = _retry_after_seconds(resp)
                if wait is None:
                    wait = min(600.0, 30.0 * 2**attempt) * random.uniform(0.8, 1.2)
                if resp.status_code == 429 and wait > self.cfg.max_retry_after:
                    raise Blocked(f"HTTP 429 with Retry-After {wait:.0f} s for {resp.url}")
                wait = min(wait, self.cfg.max_retry_after)
                log.warning("%s -> %s; retry %d in %.0f s", resp.url, last_error, attempt + 1, wait)
                time.sleep(wait)
                continue
            self._respect_rate_limit(resp)
            return resp
        raise GaveUp(f"{url}: giving up after {self.cfg.max_retries + 1} attempts ({last_error})")
