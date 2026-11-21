"""
Standard Ebooks (standardebooks.org): carefully produced public-domain ebooks, released under CC0.

Read 2026-11-19 (standardebooks.org/feeds and /robots.txt): the New Releases RSS/Atom feeds are open to everyone; the
other feeds (OPDS, "All ebooks", by author/subject/collection) are a benefit of the Patrons Circle. So this provider
uses only https://standardebooks.org/feeds/atom/new-releases (the 15 latest releases), fetched at most once an hour.
Every entry it has seen is remembered on disk, so a book stays downloadable after it drops off the feed. The page
links marked "Following this link will ban your IP" (/honeypot) are never followed; robots.txt otherwise allows the
feed and the /ebooks/*/downloads/* files for general user agents. Downloads use the feed's own enclosure links
("?source=feed"), at least 5 s apart.
"""

import json
import logging
import os
import re
import threading
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from stacks.catalogue.base import (BadRequest, CatalogueItem, FileFormat, NotFound, Provider, norm_title, surname)

logger = logging.getLogger("catalogue")

FEED = "https://standardebooks.org/feeds/atom/new-releases"
FEED_TTL = 3600
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"
ID_RE = re.compile(r"[a-z0-9_-]{1,120}(?:/[a-z0-9_-]{1,120}){1,4}")   # (translators: "louise-maude_aylmer-maude")
SEEN_MAX = 5000


class StandardEbooks(Provider):
    name = "standardebooks"
    label = "Standard Ebooks"
    kind = "ebook"

    def __init__(self, http, seen_path=None):
        super().__init__(http)
        self.seen_path = seen_path
        self.lock = threading.Lock()
        self.seen = self._load()

    # ---- the remembered entries ----------------------------------------------------------------------------------
    def _load(self):
        if not self.seen_path:
            return {}
        try:
            with open(self.seen_path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self):
        if not self.seen_path:
            return
        try:
            os.makedirs(os.path.dirname(self.seen_path), exist_ok=True)
            tmp = self.seen_path + ".new"
            with open(tmp, "w") as f:
                json.dump(self.seen, f)
            os.replace(tmp, self.seen_path)
        except OSError as e:
            logger.warning(f"Standard Ebooks: couldn't remember the feed's entries ({e})")

    # ---- the feed ------------------------------------------------------------------------------------------------
    def valid_id(self, item_id):
        return bool(ID_RE.fullmatch(str(item_id or "")))

    @staticmethod
    def parse(xml_bytes):
        """[entry dict] from the Atom feed: id, title, authors, cover, summary, epub url and size."""
        root = ET.fromstring(xml_bytes)
        out = []
        for e in root.findall(f"{ATOM}entry"):
            page = (e.findtext(f"{ATOM}id") or "").strip()
            u = urlsplit(page)
            if u.scheme != "https" or u.hostname != "standardebooks.org" or not u.path.startswith("/ebooks/"):
                continue
            sid = u.path[len("/ebooks/"):].strip("/")
            if not ID_RE.fullmatch(sid):
                continue
            epub = size = None
            for link in e.findall(f"{ATOM}link"):
                href = link.get("href") or ""
                if (link.get("rel") == "enclosure" and link.get("type") == "application/epub+zip"
                        and "_advanced" not in href and href.startswith("https://standardebooks.org/ebooks/")):
                    epub = href
                    size = int(link.get("length")) if (link.get("length") or "").isdigit() else None
                    break
            if not epub:
                continue
            thumb = e.find(f"{MEDIA}thumbnail")
            cover = thumb.get("url") if thumb is not None else None
            out.append({
                "id": sid, "title": (e.findtext(f"{ATOM}title") or "").strip(),
                "authors": [(a.findtext(f"{ATOM}name") or "").strip() for a in e.findall(f"{ATOM}author") if a.findtext(f"{ATOM}name")],
                "cover": cover if cover and cover.startswith("https://standardebooks.org/") else None,
                "summary": (e.findtext(f"{ATOM}summary") or "").strip()[:1000] or None,
                "epub": epub, "size": size, "link": page,
            })
        return out

    def refresh(self):
        data = self.http.get_bytes(FEED, ttl=FEED_TTL, accept="application/atom+xml", max_bytes=4 << 20)
        try:
            entries = self.parse(data)
        except ET.ParseError:
            logger.warning("Standard Ebooks: the feed couldn't be read")
            return
        with self.lock:
            changed = False
            for x in entries:
                if self.seen.get(x["id"]) != x:
                    self.seen[x["id"]] = x
                    changed = True
            if len(self.seen) > SEEN_MAX:
                for k in list(self.seen)[:len(self.seen) - SEEN_MAX]:
                    self.seen.pop(k, None)
            if changed:
                self._save()

    def _item(self, x):
        return CatalogueItem(
            source=self.name, id=x["id"], title=x["title"], authors=list(x.get("authors") or []), year=None,
            cover=x.get("cover"), language="en", kind="ebook", link=x.get("link"), description=x.get("summary"),
            formats=[FileFormat(kind="ebook", ext="epub", url=x["epub"], label="EPUB (compatible)", size=x.get("size"))])

    def search(self, q, limit=20):
        words = norm_title(q).split()
        if not words:
            return []
        try:
            self.refresh()
        except Exception as e:          # the remembered entries still answer while the feed is away
            logger.info(f"Standard Ebooks: feed not refreshed ({e})")
        with self.lock:
            entries = list(self.seen.values())
        out = []
        for x in entries:
            hay = norm_title(x.get("title")) + " " + " ".join(norm_title(a) for a in x.get("authors") or []) \
                + " " + " ".join(surname(a) for a in x.get("authors") or [])
            if all(w in hay.split() or w in hay for w in words):
                out.append(self._item(x))
            if len(out) >= limit:
                break
        return out

    def item(self, item_id):
        if not self.valid_id(item_id):
            raise BadRequest("a Standard Ebooks id looks like author/title")
        with self.lock:
            x = self.seen.get(item_id)
        if not x:
            self.refresh()
            with self.lock:
                x = self.seen.get(item_id)
        if not x:
            raise NotFound("That Standard Ebooks title isn't in its public New Releases feed (the full catalogue feeds "
                           "are for Standard Ebooks patrons), so it can't be fetched from here")
        return self._item(x)

    def download_plan(self, item):
        return item.formats[:1]
