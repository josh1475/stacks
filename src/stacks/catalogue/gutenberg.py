"""
Project Gutenberg, through Gutendex (gutendex.com, a JSON API over Gutenberg's catalogue).

Docs read 2026-11-19 (gutendex.com): /books?search=<words> searches titles and authors; copyright=false keeps books
in the public domain in the USA; /books/<id> is one book; "formats" maps MIME types to gutenberg.org URLs. Gutendex
asks heavy users to run their own copy; we make one request per search or item, cache answers, and pace requests.
Files come from www.gutenberg.org, whose robot policy forbids rapid automated downloading: one file at a time, at
least 2.5 s apart (see http.MIN_INTERVAL), and only when someone asked for that book.
"""

from urllib.parse import urlencode

from stacks.catalogue.base import BadRequest, CatalogueItem, FileFormat, NotFound, Provider

API = "https://gutendex.com/books"
CACHE_SEARCH = 3600
CACHE_ITEM = 86400

# MIME types we fetch, best first.
FORMATS = (
    ("application/epub+zip", "epub", "EPUB"),
)
LANG = {"en": "en", "fr": "fr", "de": "de", "es": "es", "it": "it", "pt": "pt", "nl": "nl", "fi": "fi"}


class Gutenberg(Provider):
    name = "gutenberg"
    label = "Project Gutenberg"
    kind = "ebook"

    def valid_id(self, item_id):
        s = str(item_id or "")
        return s.isdigit() and 0 < len(s) <= 7

    def _item(self, b):
        if not isinstance(b, dict) or not b.get("id") or not b.get("title"):
            return None
        if b.get("copyright") is not False:            # only books Gutenberg lists as public domain in the USA
            return None
        fmts = b.get("formats") or {}
        files = []
        for mime, ext, label in FORMATS:
            url = fmts.get(mime)
            if isinstance(url, str) and url.startswith("https://"):
                files.append(FileFormat(kind="ebook", ext=ext, url=url, label=label))
        if not files:
            return None
        authors = [a.get("name") for a in b.get("authors") or [] if isinstance(a, dict) and a.get("name")]
        langs = [str(x) for x in b.get("languages") or []]
        summary = next((s for s in b.get("summaries") or [] if isinstance(s, str)), None)
        cover = fmts.get("image/jpeg")
        return CatalogueItem(
            source=self.name, id=str(b["id"]), title=str(b["title"]).strip(), authors=authors, year=None,
            cover=cover if isinstance(cover, str) and cover.startswith("https://") else None,
            language=langs[0] if langs else None, formats=files, kind="ebook",
            link=f"https://www.gutenberg.org/ebooks/{int(b['id'])}",
            description=(summary or "")[:1000] or None)

    def search(self, q, limit=20):
        q = " ".join(str(q or "").split())[:100]
        if len(q) < 2:
            return []
        d = self.http.get_json(f"{API}/?" + urlencode({"search": q, "copyright": "false"}), ttl=CACHE_SEARCH)
        out = []
        for b in d.get("results") or []:
            it = self._item(b)
            if it:
                out.append(it)
            if len(out) >= limit:
                break
        return out

    def item(self, item_id):
        if not self.valid_id(item_id):
            raise BadRequest("a Project Gutenberg id is a number")
        it = self._item(self.http.get_json(f"{API}/{int(item_id)}", ttl=CACHE_ITEM))
        if not it:
            raise NotFound("Project Gutenberg has no free EPUB of that book")
        return it

    def download_plan(self, item):
        return [f for f in item.formats if f.kind == "ebook"][:1]
