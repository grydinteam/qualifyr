"""Polite HTTP fetcher: per-host delay, retries with backoff, timeouts, robots.txt.
Static HTTP is the default path; a browser-backed Fetcher can implement the same
interface for JS-heavy sites later."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from gtm_engine.config.schema import EngineSettings

log = logging.getLogger(__name__)

_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when this IP is in a non-public range (loopback, private, link-local, reserved,
    multicast, unspecified)."""
    # Unwrap IPv6-mapped IPv4 (e.g. ::ffff:127.0.0.1) and check the embedded IPv4 against the
    # ranges below. The mapped form's own is_loopback/is_private are False on Python < 3.13, so
    # without this unwrap `::ffff:127.0.0.1` sails through the guard and httpx connects to
    # loopback – an SSRF bypass of the direct-literal check.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _is_blocked_ip_literal(hostname: str) -> bool:
    """True when the host is an IP literal in a non-public range. Domain names return False
    (they are resolved and checked by HttpFetcher._host_is_blocked, which can await DNS)."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return _ip_is_blocked(ip)
_META_CHARSET_RE = re.compile(rb"<meta[^>]+charset=[\"']?\s*([a-zA-Z0-9_-]+)", re.I)
_XML_DECL_RE = re.compile(rb"<\?xml[^>]+encoding=[\"']([a-zA-Z0-9_-]+)", re.I)


def decode_body(raw: bytes, content_type: str) -> str:
    """Header charset -> <meta charset> / XML declaration -> UTF-8 BOM -> utf-8 -> cp1252.
    Sites in Pakistan often omit the header and only declare the charset in HTML, which
    httpx alone would decode as UTF-8 and garble."""
    candidates: list[str] = []
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        candidates.append(m.group(1))
    head = raw[:4096]
    for pat in (_META_CHARSET_RE, _XML_DECL_RE):
        mm = pat.search(head)
        if mm:
            candidates.append(mm.group(1).decode("ascii", "ignore"))
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    candidates += ["utf-8", "cp1252"]
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    text: str
    content_type: str
    error: str | None = None
    truncated: bool = False      # body hit max_response_bytes and was cut

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status_code < 300

    @property
    def is_html(self) -> bool:
        return "html" in self.content_type or (self.ok and self.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")))


class Fetcher(Protocol):
    async def get(self, url: str, **kwargs) -> FetchResult: ...
    async def close(self) -> None: ...


class HttpFetcher:
    def __init__(self, settings: EngineSettings):
        self.settings = settings
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=httpx.Timeout(settings.request_timeout_s),
            follow_redirects=True,
            max_redirects=5,
        )
        self._last_hit: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._sem = asyncio.Semaphore(settings.concurrency)
        self._host_failures: dict[str, int] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _host_is_blocked(self, hostname: str) -> bool:
        """True when a request to this host must be refused (SSRF guard).

        An IP literal is checked directly. A domain is resolved and blocked if ANY of its
        addresses is in a non-public range, which closes the DNS-based bypass where a
        public-looking hostname resolves to 127.0.0.1 / 169.254.169.254 / 10.x. DNS is resolved
        off the event loop via the loop's resolver; a resolution failure fails closed (blocked),
        since a host we cannot verify is a host we will not fetch.

        Residual: this does not pin the resolved address, so a determined attacker rebinding DNS
        between this lookup and httpx's own could still slip through. Pinning the connection to a
        vetted IP would close that and is the next step if this becomes a real threat model."""
        if not hostname:
            return True
        try:
            return _ip_is_blocked(ipaddress.ip_address(hostname))
        except ValueError:
            pass  # not a literal: resolve it
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(hostname, None)
        except (socket.gaierror, OSError) as exc:
            log.warning("dns resolution failed for %s (%s); blocking", hostname, exc)
            return True
        for info in infos:
            try:
                if _ip_is_blocked(ipaddress.ip_address(info[4][0])):
                    return True
            except ValueError:
                continue
        return False

    async def __aenter__(self) -> "HttpFetcher":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # -- politeness ---------------------------------------------------------

    def _lock_for(self, host: str) -> asyncio.Lock:
        if host not in self._host_locks:
            self._host_locks[host] = asyncio.Lock()
        return self._host_locks[host]

    async def _throttle(self, host: str, delay: float | None = None) -> None:
        delay = self.settings.per_host_delay_s if delay is None else delay
        last = self._last_hit.get(host, 0.0)
        wait = last + delay - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_hit[host] = time.monotonic()

    async def _allowed(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            self._robots[base] = await self._load_robots(base)
        rp = self._robots[base]
        if rp is None:
            return True
        return rp.can_fetch(self.settings.user_agent, url)

    async def _load_robots(self, base: str) -> RobotFileParser | None:
        try:
            resp = await self._client.get(f"{base}/robots.txt")
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or "text" not in resp.headers.get("content-type", "text/plain"):
            return None
        rp = RobotFileParser()
        rp.parse(resp.text.splitlines())
        return rp

    async def _read_capped(self, url: str, headers: dict[str, str] | None):
        """Stream the body, stopping at max_response_bytes so one huge page cannot
        exhaust memory. Returns (response, raw_bytes, truncated)."""
        cap = self.settings.max_response_bytes
        chunks: list[bytes] = []
        size = 0
        truncated = False
        async with self._client.stream("GET", url, headers=headers) as resp:
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > cap:
                truncated = True
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= cap:
                    truncated = True
                    break
            return resp, b"".join(chunks)[:cap], truncated

    # -- fetch --------------------------------------------------------------

    async def get(self, url: str, *, delay: float | None = None, api: bool = False,
                  headers: dict[str, str] | None = None) -> FetchResult:
        """`api=True` marks a programmatic endpoint (Overpass, search): robots.txt governs
        crawlers on websites, not API clients, so the check is skipped there."""
        host = urlparse(url).netloc.lower()
        if self.settings.block_private_hosts and await self._host_is_blocked(urlparse(url).hostname or ""):
            log.warning("blocked request to non-public host: %s", url)
            return FetchResult(url, url, 0, "", "", error="blocked_private_host")
        if self._host_failures.get(host, 0) >= self.settings.host_failure_limit:
            return FetchResult(url, url, 0, "", "", error="host_unavailable")
        if not api and not await self._allowed(url):
            log.info("robots.txt disallows %s", url)
            return FetchResult(url, url, 0, "", "", error="robots_disallowed")

        attempt = 0
        async with self._sem:
            while True:
                truncated = False
                async with self._lock_for(host):
                    await self._throttle(host, delay)
                    try:
                        resp, raw, truncated = await self._read_capped(url, headers)
                    except httpx.TimeoutException:
                        err = "timeout"
                        resp = None
                    except httpx.HTTPError as exc:
                        err = f"http_error:{type(exc).__name__}"
                        resp = None
                    else:
                        err = None
                if resp is not None and resp.status_code not in _RETRYABLE:
                    if 200 <= resp.status_code < 400:
                        self._host_failures.pop(host, None)
                    elif resp.status_code in (401, 403, 429):
                        # The site is refusing us. We do not disguise the client to get around
                        # that; the host is simply recorded as blocked.
                        self._host_failures[host] = self._host_failures.get(host, 0) + 1
                        return FetchResult(url, str(resp.url), resp.status_code, "", "", error="blocked")
                    ctype = resp.headers.get("content-type", "")
                    textual = ("text" in ctype or "json" in ctype or "xml" in ctype
                               or (not ctype and raw[:64].lstrip().lower().startswith((b"<!doctype", b"<html", b"{"))))
                    body = decode_body(raw, ctype) if textual else ""
                    return FetchResult(url, str(resp.url), resp.status_code, body, ctype, truncated=truncated)
                attempt += 1
                if attempt > self.settings.max_retries:
                    status = resp.status_code if resp is not None else 0
                    self._host_failures[host] = self._host_failures.get(host, 0) + 1
                    return FetchResult(url, url, status, "", "", error=err or f"status_{status}")
                backoff = min(2.0 ** attempt, 20.0)
                log.debug("retry %s in %.1fs (%s)", url, backoff, err or resp.status_code)
                await asyncio.sleep(backoff)
