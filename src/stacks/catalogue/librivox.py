"""
LibriVox (librivox.org): public-domain audiobooks read by volunteers; the audio is hosted by the Internet Archive.

API read 2026-11-19 (librivox.org/api/info): GET /api/feed/audiobooks with title= (prefix with ^), author=, id=,
format=json, extended=1 (adds "sections": every chapter's MP3, "listen_url"), coverart=1, limit (at most 500, default
50). "Please be considerate, and add a delay between your requests"; it may answer 429. A search here is one request
(two when the first finds nothing); an item is one; both are cached.

Downloads: the chapter MP3s one by one (archive.org/download/... redirects to a numbered storage node that supports
byte ranges, so an interrupted chapter resumes). That is more reliable than the whole-book zip, which archive.org
builds on the fly (/compress/...) without a length or ranges.
"""

import html
import re
from urllib.parse import urlencode

from stacks.catalogue.base import BadRequest, CatalogueItem, FileFormat, NotFound, Provider, surname

API = "https://librivox.org/api/feed/audiobooks/"
CACHE_SEARCH = 3600
CACHE_ITEM = 86400
FIELDS = "{id,title,description,language,copyright_year,num_sections,url_librivox,authors,totaltimesecs}"
LANG = {"english": "en", "french": "fr", "german": "de", "spanish": "es", "italian": "it", "dutch": "nl",
        "portuguese": "pt", "russian": "ru", "chinese": "zh", "japanese": "ja", "latin": "la", "greek": "el"}
MAX_PARTS = 400


def _https(url):
    url = str(url or "").strip()
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    return url if url.startswith("https://") else None


class LibriVox(Provider):
    name = "librivox"
    label = "LibriVox"
    kind = "audio"

    def valid_id(self, item_id):
        s = str(item_id or "")
        return s.isdigit() and 0 < len(s) <= 8

    def _item(self, b):
        if not isinstance(b, dict) or not str(b.get("id") or "").isdigit() or not b.get("title"):
            return None
        authors = []
        for a in b.get("authors") or []:
            if isinstance(a, dict):
                n = " ".join(x for x in (a.get("first_name"), a.get("last_name")) if x).strip()
                if n:
                    authors.append(n)
        year = str(b.get("copyright_year") or "")
        files = []
        for i, s in enumerate(b.get("sections") or []):
            url = _https((s or {}).get("listen_url"))
            if url:
                files.append(FileFormat(kind="audio", ext="mp3", url=url,
                                        name=str((s or {}).get("title") or f"Part {i + 1}")[:120].strip()))
        desc = html.unescape(re.sub(r"<[^>]+>", "", str(b.get("description") or ""))).strip()
        cover = _https(b.get("coverart_jpg")) or _https(b.get("coverart_thumbnail"))
        try:
            parts = int(b.get("num_sections") or len(files) or 1)
        except (TypeError, ValueError):
            parts = len(files) or 1
        return CatalogueItem(
            source=self.name, id=str(b["id"]), title=html.unescape(str(b["title"])).strip(), authors=authors,
            year=int(year) if year.isdigit() and 0 < int(year) < 3000 else None,
            cover=cover, language=LANG.get(str(b.get("language") or "").lower(), b.get("language") or None),
            formats=files or [FileFormat(kind="audio", ext="mp3", url="", label="MP3 chapters")],
            kind="audio", link=_https(b.get("url_librivox")), description=desc[:1000] or None, parts=parts)

    def _query(self, params):
        try:
            d = self.http.get_json(API + "?" + urlencode(dict(params, format="json", coverart=1)), ttl=CACHE_SEARCH)
        except NotFound:                  # LibriVox answers 404 {"error": "Audiobooks could not be found"}
            return []
        return [x for x in (d.get("books") or []) if isinstance(x, dict)] if isinstance(d, dict) else []

    def search(self, q, limit=20):
        q = " ".join(str(q or "").split())[:100]
        if len(q) < 2:
            return []
        books = self._query({"title": "^" + q, "limit": limit, "fields": FIELDS})
        words = q.split()
        if not books and len(words) >= 2:
            # "pride and prejudice austen": the title without the last word, kept when the author's surname matches
            last = words[-1].lower()
            books = [b for b in self._query({"title": "^" + " ".join(words[:-1]), "limit": limit, "fields": FIELDS})
                     if any(surname(f"{a.get('first_name', '')} {a.get('last_name', '')}") == last
                            for a in b.get("authors") or [] if isinstance(a, dict))]
        if not books and len(words) == 1:
            books = self._query({"author": q, "limit": limit, "fields": FIELDS})
        out = [it for it in (self._item(b) for b in books) if it]
        for it in out:                    # search answers list the item; its chapters come with item()
            it.formats = [FileFormat(kind="audio", ext="mp3", url="", label="MP3 chapters")]
        return out[:limit]

    def item(self, item_id):
        if not self.valid_id(item_id):
            raise BadRequest("a LibriVox id is a number")
        try:
            d = self.http.get_json(API + "?" + urlencode({"id": int(item_id), "format": "json", "extended": 1, "coverart": 1}),
                                   ttl=CACHE_ITEM)
        except NotFound:
            raise NotFound("LibriVox has no such audiobook")
        books = d.get("books") if isinstance(d, dict) else None
        it = self._item(books[0]) if books else None
        if not it or not any(f.url for f in it.formats):
            raise NotFound("LibriVox lists no audio files for that audiobook yet")
        if len(it.formats) > MAX_PARTS:
            raise BadRequest(f"that audiobook has {len(it.formats)} parts; at most {MAX_PARTS} are fetched")
        return it

    def download_plan(self, item):
        return [f for f in item.formats if f.url]
