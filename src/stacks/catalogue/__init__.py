"""
Public-domain catalogues for Stacks: Project Gutenberg, Standard Ebooks and LibriVox.

Items are queued by (source, id), keyed in the queue as md5("source:id"), and downloaded by
stacks.catalogue.downloader from the hosts in stacks.catalogue.http.ALLOWED_HOSTS only.
"""

import logging
import threading

from stacks.catalogue.base import (SOURCE_LABELS, BadRequest, Busy, CatalogueError, CatalogueItem, NotFound,
                                   catalogue_key, norm_title, surname)

logger = logging.getLogger("catalogue")

# The order results are listed in when one book is in several catalogues: Standard Ebooks' editions are the most
# carefully typeset, then Gutenberg's, then LibriVox's recordings.
SOURCES = ("standardebooks", "gutenberg", "librivox")

_lock = threading.Lock()
_registry = {}


def catalogue_only(config) -> bool:
    """CATALOGUE_ONLY (env) or catalogue.only (config): only public-domain catalogues; no md5 downloads at all."""
    return bool(config.get("catalogue", "only", default=False))


def enabled_sources(config) -> list:
    """The sources switched on in Settings (catalogue.<source>: true, the default)."""
    return [s for s in SOURCES if config.get("catalogue", s, default=True) is not False]


def get_http():
    from stacks.catalogue.http import CatalogueHTTP
    with _lock:
        if "http" not in _registry:
            _registry["http"] = CatalogueHTTP()
        return _registry["http"]


def get_provider(source, http=None):
    """The provider for SOURCE (one per process, sharing the paced HTTP client), else BadRequest."""
    if source not in SOURCES:
        raise BadRequest(f"unknown catalogue '{source}' (one of: {', '.join(SOURCES)})")
    key = ("provider", source, id(http) if http else None)
    with _lock:
        p = _registry.get(key)
    if p:
        return p
    http = http or get_http()
    if source == "gutenberg":
        from stacks.catalogue.gutenberg import Gutenberg
        p = Gutenberg(http)
    elif source == "standardebooks":
        from stacks.catalogue.standardebooks import StandardEbooks
        from stacks.constants import CONFIG_PATH
        p = StandardEbooks(http, seen_path=str(CONFIG_PATH / "catalogue_standardebooks.json"))
    else:
        from stacks.catalogue.librivox import LibriVox
        p = LibriVox(http)
    with _lock:
        _registry[key] = p
    return p


def group_key(item: CatalogueItem):
    return (norm_title(item.title), surname(item.authors[0]) if item.authors else "")


def merge(results_by_source: dict, order=SOURCES) -> list:
    """One result per book across catalogues: {title, authors, year, cover, language, sources: [...]}, each source
    with its id, key, kind and formats, in ORDER. A book in several catalogues is listed once."""
    groups, out = {}, []
    for source in order:
        for it in results_by_source.get(source) or []:
            gk = group_key(it)
            g = groups.get(gk)
            if g is None:
                g = {"title": it.title, "authors": list(it.authors), "year": it.year, "cover": it.cover,
                     "language": it.language, "description": it.description, "sources": []}
                groups[gk] = g
                out.append(g)
            else:
                g["cover"] = g["cover"] or it.cover
                g["year"] = g["year"] or it.year
                g["description"] = g["description"] or it.description
            d = it.to_dict()
            g["sources"].append({k: d[k] for k in ("source", "source_label", "id", "key", "kind", "formats", "link", "parts")})
    return out


def search_all(config, q, source=None, limit=20, http=None):
    """(merged results, {source: error}) for Q over the enabled catalogues (or just SOURCE)."""
    sources = enabled_sources(config)
    if source:
        if source not in SOURCES:
            raise BadRequest(f"unknown catalogue '{source}'")
        if source not in sources:
            raise BadRequest(f"{SOURCE_LABELS[source]} is switched off in Settings")
        sources = [source]
    found, errors = {}, {}
    for s in sources:
        try:
            found[s] = get_provider(s, http).search(q, limit=limit)
        except CatalogueError as e:
            errors[s] = str(e)
        except Exception as e:                        # one catalogue failing doesn't hide the others
            logger.warning(f"catalogue search {s}: {e}")
            errors[s] = "the catalogue didn't answer"
    return merge(found), errors


__all__ = ["SOURCES", "SOURCE_LABELS", "BadRequest", "Busy", "CatalogueError", "NotFound", "catalogue_key",
           "catalogue_only", "enabled_sources", "get_provider", "merge", "search_all"]
