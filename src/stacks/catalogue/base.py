"""
Provider interface for the public-domain catalogues.

A provider knows one catalogue (Project Gutenberg, Standard Ebooks, LibriVox):
how to search it, how to describe one item, and which file(s) to download for
it. Providers never take a URL from the caller: every URL they produce is built
from the catalogue's own answers, and the HTTP layer refuses anything outside
the hard-coded host allowlist (see stacks.catalogue.http).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Optional


class CatalogueError(Exception):
    """A catalogue request that failed, in words for the API."""

    status = 502


class NotFound(CatalogueError):
    status = 404


class BadRequest(CatalogueError):
    status = 400


class Busy(CatalogueError):
    """The catalogue asked us to slow down (429/503 + Retry-After), or we are still holding off."""

    status = 503

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class FileFormat:
    """One downloadable file (or one part of a multi-file item)."""
    kind: str                     # "ebook" or "audio"
    ext: str                      # "epub", "mp3", "pdf"
    url: str
    label: Optional[str] = None
    size: Optional[int] = None
    name: Optional[str] = None    # a part's title (an audiobook's chapter)


@dataclass
class CatalogueItem:
    source: str
    id: str
    title: str
    authors: list = field(default_factory=list)
    year: Optional[int] = None
    cover: Optional[str] = None
    language: Optional[str] = None
    formats: list = field(default_factory=list)    # list[FileFormat]
    kind: str = "ebook"                            # "ebook" or "audio"
    link: Optional[str] = None                     # the item's own page at the catalogue
    description: Optional[str] = None
    parts: int = 1                                 # how many files the download is

    @property
    def key(self) -> str:
        return catalogue_key(self.source, self.id)

    def to_dict(self, include_files=False) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["source_label"] = SOURCE_LABELS.get(self.source, self.source)
        files = d.pop("formats")
        d["formats"] = sorted({f["ext"] for f in files})
        if include_files:
            d["files"] = files
        return d


SOURCE_LABELS = {
    "gutenberg": "Project Gutenberg",
    "standardebooks": "Standard Ebooks",
    "librivox": "LibriVox",
}


def catalogue_key(source: str, item_id: str) -> str:
    """The queue's key for a catalogue item: md5 of "source:id" (the queue is keyed on md5)."""
    return hashlib.md5(f"{source}:{item_id}".encode("utf-8")).hexdigest()


def norm_title(s: str) -> str:
    """A title to compare across catalogues: lower case, no subtitle, no punctuation, no leading article."""
    s = str(s or "").replace("’", "'").replace("&", " and ")
    s = re.sub(r"\s*\((?:version|v\.)\s*\d+[^)]*\)", "", s, flags=re.I)
    s = re.split(r"\s*[:;(\[]", s, maxsplit=1)[0].lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^(the|a|an) ", "", " ".join(s.split()))
    return s


def surname(author: str) -> str:
    """The surname of "Austen, Jane" or "Jane Austen"."""
    a = str(author or "").strip()
    if not a:
        return ""
    if "," in a:
        return a.split(",")[0].strip().lower()
    return a.split()[-1].lower()


class Provider:
    """One catalogue. Subclasses set `name`, `label`, `kind` and implement the calls below."""

    name = ""
    label = ""
    kind = "ebook"

    def __init__(self, http):
        self.http = http

    def valid_id(self, item_id: str) -> bool:
        raise NotImplementedError

    def search(self, q: str, limit: int = 20) -> list:
        """list[CatalogueItem] for a free-text query (title and/or author)."""
        raise NotImplementedError

    def item(self, item_id: str) -> CatalogueItem:
        """One item, with its downloadable files in `formats`."""
        raise NotImplementedError

    def download_plan(self, item: CatalogueItem) -> list:
        """The file(s) to fetch for ITEM as list[FileFormat]: one for a book, every part in order for an audiobook."""
        raise NotImplementedError
