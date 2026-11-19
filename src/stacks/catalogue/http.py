"""
The only way catalogue code talks to the network.

- HTTPS only, and only to the hosts in ALLOWED_HOSTS (plus the Internet
  Archive's numbered download nodes, which archive.org redirects to).
- Redirects are followed by hand, and every hop is checked against the same
  allowlist before it is requested.
- An honest User-Agent naming the software and where it comes from.
- Polite pacing per host (MIN_INTERVAL), and Retry-After / exponential backoff
  when a host answers 429 or 503.

Nothing here takes a URL from an API caller: providers build URLs from the
catalogues' own answers. The allowlist is the backstop.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests

from stacks.catalogue.base import Busy, CatalogueError, NotFound
from stacks.constants import VERSION

logger = logging.getLogger("catalogue")

USER_AGENT = (f"Stacks-Catalogue/{VERSION} (public-domain catalogue downloader; "
              "+https://github.com/josh1475/stacks)")

# Every host the catalogue downloader may contact. Hard-coded on purpose: there is no setting that adds to it.
ALLOWED_HOSTS = frozenset({
    "gutendex.com",           # Project Gutenberg's metadata (JSON API)
    "www.gutenberg.org",      # Project Gutenberg's files
    "gutenberg.org",
    "standardebooks.org",     # Standard Ebooks' public New Releases feed and its files
    "librivox.org",           # LibriVox's API
    "archive.org",            # LibriVox's audio files live on the Internet Archive
    "www.archive.org",        # (LibriVox's API still links www.archive.org, which redirects to archive.org)
})

# archive.org/download/... redirects to a storage node such as ia800300.us.archive.org or dn710107.ca.archive.org
# (seen 2026-11-19). Only those numbered nodes, nothing else under archive.org.
ARCHIVE_NODE = re.compile(r"^(?:ia|dn)\d{1,8}\.(?:us|ca)\.archive\.org$")

# Seconds between requests to one host (per process). Downloads and searches run in different processes; each keeps
# its own pace, and a download worker only ever fetches one file at a time.
MIN_INTERVAL = {
    "gutendex.com": 1.0,
    "www.gutenberg.org": 2.5,      # Gutenberg's robot policy: no rapid automated downloading
    "gutenberg.org": 2.5,
    "standardebooks.org": 5.0,
    "librivox.org": 1.0,
    "archive.org": 1.0,
    "www.archive.org": 1.0,
}
NODE_INTERVAL = 1.0
MAX_REDIRECTS = 6
MAX_JSON_BYTES = 8 << 20
BACKOFF_FIRST = 30.0
BACKOFF_MAX = 3600.0


class NotAllowed(CatalogueError):
    status = 400


def host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return host in ALLOWED_HOSTS or bool(ARCHIVE_NODE.fullmatch(host))


def check_url(url: str) -> str:
    """URL when it is https:// to an allowed host on the default port, else NotAllowed."""
    try:
        u = urlsplit(str(url or ""))
    except ValueError:
        raise NotAllowed("not a URL")
    if u.scheme != "https":
        raise NotAllowed(f"only https is used for catalogues (got {u.scheme or 'no scheme'})")
    if u.username or u.password:
        raise NotAllowed("URLs with credentials are refused")
    try:
        port = u.port
    except ValueError:
        raise NotAllowed("bad port")
    if port not in (None, 443):
        raise NotAllowed("only the default HTTPS port is used")
    if not host_allowed(u.hostname or ""):
        raise NotAllowed(f"{u.hostname} is not one of the catalogue hosts")
    return url


def _retry_after(value, now):
    """Seconds from a Retry-After header (delta-seconds or an HTTP date), or None."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - now)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


class CatalogueHTTP:
    """Allowlisted, paced HTTPS for the catalogue providers and the catalogue downloader."""

    def __init__(self, session=None, clock=time.monotonic, sleep=time.sleep, wall=time.time):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.lock = threading.Lock()
        self.last = {}            # host -> monotonic time of the last request
        self.hold = {}            # host -> monotonic time until which we don't ask it
        self.failures = {}        # host -> refusals in a row (for the backoff)
        self.cache = {}           # url -> (monotonic time, value)

    # ---- pacing --------------------------------------------------------------------------------------------------
    def _wait_turn(self, host, max_wait):
        with self.lock:
            now = self.clock()
            held = self.hold.get(host, 0) - now
            if held > 0:
                if held > max_wait:
                    raise Busy(f"{host} asked us to slow down; trying again in {int(held) + 1} s", retry_after=held)
            interval = NODE_INTERVAL if ARCHIVE_NODE.fullmatch(host) else MIN_INTERVAL.get(host, 2.0)
            start = max(now, self.last.get(host, -1e9) + interval, self.hold.get(host, 0))
            wait = start - now
            if wait > max_wait:
                raise Busy(f"{host} is busy; try again in {int(wait) + 1} s", retry_after=wait)
            self.last[host] = start
        if wait > 0:
            self.sleep(wait)

    def _refused(self, host, retry_after):
        with self.lock:
            n = self.failures.get(host, 0) + 1
            self.failures[host] = n
            wait = retry_after if retry_after is not None else min(BACKOFF_MAX, BACKOFF_FIRST * (2 ** (n - 1)))
            wait = min(BACKOFF_MAX, wait) * (1 + random.random() * 0.1)
            self.hold[host] = self.clock() + wait
        logger.warning(f"{host} refused (rate limit); holding off {int(wait)} s")
        return wait

    def _ok(self, host):
        with self.lock:
            self.failures.pop(host, None)

    # ---- requests ------------------------------------------------------------------------------------------------
    def open(self, url, headers=None, timeout=30, max_wait=3.0, stream=True):
        """A requests.Response for URL after following allowed redirects (each hop checked). The caller closes it.
        404/410 -> NotFound; 429/503 -> Busy (and the host is held off); other non-2xx -> CatalogueError."""
        check_url(url)
        hops = 0
        while True:
            host = urlsplit(url).hostname.lower()
            self._wait_turn(host, max_wait)
            try:
                r = self.session.get(url, headers=headers or {}, timeout=timeout, stream=stream, allow_redirects=False)
            except requests.RequestException as e:
                raise CatalogueError(f"{host} didn't answer ({type(e).__name__})")
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("Location")
                r.close()
                if not loc:
                    raise CatalogueError(f"{host} redirected nowhere")
                hops += 1
                if hops > MAX_REDIRECTS:
                    raise CatalogueError("too many redirects")
                url = check_url(urljoin(url, loc))        # every hop must stay on the allowlist
                continue
            if r.status_code in (429, 503):
                ra = _retry_after(r.headers.get("Retry-After"), self.wall())
                r.close()
                wait = self._refused(host, ra)
                raise Busy(f"{host} asked us to slow down; trying again in {int(wait) + 1} s", retry_after=wait)
            if r.status_code in (404, 410):
                r.close()
                raise NotFound(f"{host} has no such item")
            if r.status_code == 416:
                return r                                    # (the downloader handles a range past the end)
            if not 200 <= r.status_code < 300:
                code = r.status_code
                r.close()
                raise CatalogueError(f"{host} answered {code}")
            self._ok(host)
            return r

    def get_bytes(self, url, ttl=0, max_bytes=MAX_JSON_BYTES, accept="application/json", max_wait=3.0):
        now = self.clock()
        hit = self.cache.get(url)
        if ttl and hit and now - hit[0] < ttl:
            return hit[1]
        r = self.open(url, headers={"Accept": accept}, timeout=20, max_wait=max_wait)
        try:
            data = b""
            for chunk in r.iter_content(1 << 16):
                data += chunk
                if len(data) > max_bytes:
                    raise CatalogueError("the catalogue's answer was too big")
        finally:
            r.close()
        if ttl:
            self.cache[url] = (now, data)
            if len(self.cache) > 500:
                for k in sorted(self.cache, key=lambda k: self.cache[k][0])[:100]:
                    self.cache.pop(k, None)
        return data

    def get_json(self, url, ttl=0, max_wait=3.0):
        data = self.get_bytes(url, ttl=ttl, max_wait=max_wait)
        try:
            return json.loads(data)
        except ValueError:
            raise CatalogueError("the catalogue's answer wasn't JSON")
