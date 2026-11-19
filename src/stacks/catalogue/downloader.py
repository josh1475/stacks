"""
The catalogue downloader: fetches a catalogue item's file(s) through the allowlisted HTTP client.

- Streams to <incomplete>/<key>/NNN.part, and resumes with a Range request when a part is already there and the host
  answers 206 (otherwise it starts that part again).
- Reports progress through the worker's progress callback (which also carries cancel requests: it returns False).
- Stops at a size cap per item.
- Checks each file's first bytes: an EPUB must be a zip whose first entry says application/epub+zip, an MP3 must
  look like MP3 (ID3 tag or an MPEG frame), a PDF must start %PDF, a zip must be a zip. A web page instead of the
  file fails the download.
- Ebooks land as "<Title> - <Author> [source-id].epub"; an audiobook's chapters land in a folder of the same name
  as "001 - <chapter>.mp3", in order.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path

from stacks.catalogue.base import CatalogueError
from stacks.catalogue.http import check_url

logger = logging.getLogger("catalogue")

CHUNK = 1 << 16


class Cancelled(Exception):
    """The worker asked us to stop (cancel, requeue or shutdown)."""


def sniff(head: bytes):
    """The type of a file from its first bytes: epub, zip, pdf, mp3, html or None."""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "epub" if b"mimetypeapplication/epub+zip" in head[:200] else "zip"
    if head.startswith(b"ID3"):
        return "mp3"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0 and head[1] & 0x06 != 0:
        return "mp3"
    low = head[:512].lstrip().lower()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html") or b"<html" in low:
        return "html"
    return None


def accepts(expected: str, got) -> bool:
    if expected == "epub":
        return got == "epub"
    if expected == "zip":
        return got in ("zip", "epub")
    return got == expected


def clean_name(s: str, n=120) -> str:
    s = re.sub(r"[\x00-\x1f/\\:*?\"<>|]+", " ", str(s or ""))
    s = " ".join(s.split()).strip(" .")
    return s[:n].rstrip(" .") or "Untitled"


def item_name(meta: dict) -> str:
    """'Pride and Prejudice - Jane Austen [gutenberg-1342]'"""
    title = clean_name(meta.get("title"), 100)
    authors = meta.get("authors") or []
    who = clean_name(authors[0], 60) if authors else ""
    tag = f"[{meta.get('source')}-{re.sub(r'[^A-Za-z0-9-]+', '-', str(meta.get('id')))[:60]}]"
    return f"{title} - {who} {tag}" if who else f"{title} {tag}"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = (path.stem, path.suffix) if path.suffix else (path.name, "")
    for i in range(2, 1000):
        p = path.with_name(f"{stem} ({i}){suffix}")
        if not p.exists():
            return p
    raise CatalogueError("too many files with that name")


class CatalogueDownloader:
    def __init__(self, http, output_dir: Path, incomplete_dir: Path, progress_callback=None,
                 max_bytes=4 << 30, clock=time.monotonic):
        self.http = http
        self.output_dir = Path(output_dir)
        self.incomplete_dir = Path(incomplete_dir)
        self.progress_callback = progress_callback
        self.max_bytes = max_bytes
        self.clock = clock

    def partial_dir(self, key):
        return self.incomplete_dir / f"catalogue-{key}"

    def discard(self, key):
        shutil.rmtree(self.partial_dir(key), ignore_errors=True)

    # ---- one file --------------------------------------------------------------------------------------------------
    def _fetch(self, url, expected_ext, part: Path, state):
        check_url(url)
        have = part.stat().st_size if part.exists() else 0
        headers = {"Accept": "*/*"}
        if have:
            headers["Range"] = f"bytes={have}-"
        r = self.http.open(url, headers=headers, timeout=60, max_wait=300)
        try:
            if r.status_code == 416:                     # what we have is the whole file, or something odd: start again
                r.close()
                part.unlink(missing_ok=True)
                have = 0
                r = self.http.open(url, headers={"Accept": "*/*"}, timeout=60, max_wait=300)
            if have and r.status_code != 206:
                have = 0                                 # the host ignored the range: start this file again
            length = int(r.headers.get("Content-Length") or 0)
            total = have + length if length else None
            if total and state["bytes"] + total - have > self.max_bytes:
                raise CatalogueError(f"it's bigger than the {self.max_bytes >> 20} MB limit")
            mode = "ab" if have else "wb"
            first = not have
            got = have
            with open(part, mode) as f:
                for chunk in r.iter_content(CHUNK):
                    if not chunk:
                        continue
                    if first:
                        kind = sniff(chunk[:4096])
                        if not accepts(expected_ext, kind):
                            ctype = (r.headers.get("Content-Type") or "").split(";")[0]
                            raise CatalogueError(
                                "the catalogue sent a web page instead of the file" if kind == "html" or "html" in ctype
                                else f"that isn't a {expected_ext.upper()} file ({ctype or 'unknown type'})")
                        first = False
                    f.write(chunk)
                    got += len(chunk)
                    state["bytes"] += len(chunk)
                    if state["bytes"] > self.max_bytes:
                        raise CatalogueError(f"it's bigger than the {self.max_bytes >> 20} MB limit")
                    self._report(state, got, total)
            if total and got < total:
                raise CatalogueError("the download was cut short")
            if got == 0:
                raise CatalogueError("the catalogue sent an empty file")
            if have:                                     # a resumed file: check its start too
                with open(part, "rb") as f:
                    if not accepts(expected_ext, sniff(f.read(4096))):
                        raise CatalogueError(f"that isn't a {expected_ext.upper()} file")
            return got
        finally:
            r.close()

    def _report(self, state, got, total, force=False):
        now = self.clock()
        if not force and now - state["last"] < 0.5:
            return
        state["last"] = now
        n = state["parts"]
        frac = (got / total) if total else 0.0
        pct = min(100.0, (state["done"] + frac) / n * 100.0)
        elapsed = max(0.001, now - state["start"])
        est = int(state["bytes"] / pct * 100) if pct > 0 else 0
        if self.progress_callback:
            ok = self.progress_callback({"percent": round(pct, 1), "downloaded": state["bytes"],
                                         "total_size": total if n == 1 and total else est,
                                         "speed": state["bytes"] / elapsed, "part": state["done"] + 1, "parts": n})
            if ok is False:
                raise Cancelled()

    # ---- an item -------------------------------------------------------------------------------------------------
    def download(self, key: str, meta: dict, plan: list, subfolder: str = None):
        """Fetch every file in PLAN ([{url, ext, name}]); returns the final path (a file, or a folder of parts).
        Raises Cancelled when the worker asks to stop (partials are kept for a resume), CatalogueError otherwise."""
        if not plan:
            raise CatalogueError("nothing to download")
        work = self.partial_dir(key)
        work.mkdir(parents=True, exist_ok=True)
        state = {"bytes": 0, "done": 0, "parts": len(plan), "last": 0.0, "start": self.clock()}
        parts = []
        for i, f in enumerate(plan):
            ext = re.sub(r"[^a-z0-9]", "", str(f.get("ext") or "").lower()) or "bin"
            part = work / f"{i + 1:03d}.part"
            done = work / f"{i + 1:03d}.{ext}"
            if not done.exists():
                self._fetch(f["url"], ext, part, state)
                os.replace(part, done)
            else:
                state["bytes"] += done.stat().st_size
            state["done"] = i + 1
            parts.append((done, f))
            self._report(state, 0, None, force=True)

        dest_root = self.output_dir / subfolder.strip("/") if subfolder else self.output_dir
        dest_root.mkdir(parents=True, exist_ok=True)
        name = item_name(meta)
        if len(parts) == 1 and meta.get("kind") != "audio":
            src, f = parts[0]
            final = _unique(dest_root / f"{name}.{src.suffix.lstrip('.')}")
            shutil.move(str(src), str(final))
        else:
            final = _unique(dest_root / name)
            tmp = dest_root / f".{final.name}.moving"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            for i, (src, f) in enumerate(parts, 1):
                title = clean_name(f.get("name") or f"Part {i}", 80)
                shutil.move(str(src), str(tmp / f"{i:03d} - {title}{src.suffix}"))
            os.replace(tmp, final)
        self.discard(key)
        return final
