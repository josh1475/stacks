"""
Public-domain catalogues: search, look up and queue by (source, id).

    GET  /api/catalogue/search?q=&source=     merged results from the enabled catalogues, each with its sources
    GET  /api/catalogue/item?source=&id=      one item, with the files a download fetches
    POST /api/catalogue/queue                 {source, id, subfolder?}: into the queue (key md5("source:id"))
    GET  /api/catalogue/downloads?keys=       catalogue items in the queue and history (the Downloader key can't
                                              read /api/status, so it reads its own items here)

The Downloader key may use all four. No endpoint takes a URL: only a source name and the catalogue's own id.
"""

import json
import logging
import os
from urllib.parse import urlsplit

from flask import current_app, jsonify, request

from . import api_bp
from stacks.catalogue import (SOURCE_LABELS, BadRequest, Busy, CatalogueError, catalogue_key, enabled_sources,
                              get_provider, search_all)
from stacks.constants import DOWNLOAD_PATH
from stacks.security.auth import require_auth_with_permissions

logger = logging.getLogger("api")


def get_queue_ops():
    from stacks.coordinator.queue_ops import QueueOperations
    return QueueOperations()


def _error(e):
    if isinstance(e, Busy):
        resp = jsonify({"success": False, "error": str(e)})
        if e.retry_after:
            resp.headers["Retry-After"] = str(int(e.retry_after) + 1)
        return resp, 503
    return jsonify({"success": False, "error": str(e)}), getattr(e, "status", 502)


def _need_multiprocess():
    if not current_app.stacks_multiprocess:
        return jsonify({"success": False, "error": "catalogues need Stacks' normal (multi-process) mode"}), 503
    return None


def _source_on(source):
    config = current_app.stacks_config
    if source not in SOURCE_LABELS:
        raise BadRequest(f"unknown catalogue '{source}'")
    if source not in enabled_sources(config):
        raise BadRequest(f"{SOURCE_LABELS[source]} is switched off in Settings")


@api_bp.route("/api/catalogue/search", methods=["GET"])
@require_auth_with_permissions(allow_downloader=True)
def api_catalogue_search():
    q = " ".join((request.args.get("q") or "").split())[:100]
    source = request.args.get("source") or None
    if len(q) < 2:
        return jsonify({"success": True, "q": q, "results": [], "errors": {}})
    try:
        results, errors = search_all(current_app.stacks_config, q, source=source)
    except CatalogueError as e:
        return _error(e)
    return jsonify({"success": True, "q": q, "results": results, "errors": errors})


@api_bp.route("/api/catalogue/item", methods=["GET"])
@require_auth_with_permissions(allow_downloader=True)
def api_catalogue_item():
    source = request.args.get("source") or ""
    item_id = request.args.get("id") or ""
    try:
        _source_on(source)
        provider = get_provider(source)
        if not provider.valid_id(item_id):
            raise BadRequest(f"that isn't a {SOURCE_LABELS[source]} id")
        item = provider.item(item_id)
    except CatalogueError as e:
        return _error(e)
    out = item.to_dict(include_files=True)
    if current_app.stacks_multiprocess:
        row = get_queue_ops().get_download_by_md5(item.key)
        out["queue_status"] = row["status"] if row else None
    return jsonify({"success": True, "item": out})


@api_bp.route("/api/catalogue/queue", methods=["POST"])
@require_auth_with_permissions(allow_downloader=True)
def api_catalogue_queue():
    refused = _need_multiprocess()
    if refused:
        return refused
    data = request.get_json(silent=True) or {}
    source = str(data.get("source") or "")
    item_id = str(data.get("id") or "")
    try:
        _source_on(source)
        provider = get_provider(source)
        if not provider.valid_id(item_id):
            raise BadRequest(f"that isn't a {SOURCE_LABELS[source]} id")
        item = provider.item(item_id)
        plan = provider.download_plan(item)
        if not plan:
            raise CatalogueError("nothing to download for that item")
    except CatalogueError as e:
        return _error(e)

    subfolder = data.get("subfolder")
    validated_subfolder = None
    if subfolder:
        allowed = current_app.stacks_config.get("downloads", "subdirectories", default=None)
        if allowed and isinstance(allowed, list) and subfolder in allowed:
            validated_subfolder = subfolder
        else:
            logger.warning(f"Subfolder '{subfolder}' not in allowed list, reverting to default")

    meta = {
        "source": item.source, "id": item.id, "title": item.title, "authors": item.authors, "year": item.year,
        "cover": item.cover, "language": item.language, "kind": item.kind, "link": item.link,
        "files": [{"url": f.url, "ext": f.ext, "name": f.name, "size": f.size} for f in plan],
    }
    mirror = {"url": plan[0].url, "domain": urlsplit(plan[0].url).hostname, "type": "catalogue",
              "text": SOURCE_LABELS[item.source]}
    key = catalogue_key(item.source, item.id)
    success, message = get_queue_ops().add_catalogue_download(
        key, item.source, item.id, item.title, meta, mirror, subfolder=validated_subfolder,
        added_by=str(data.get("added_by") or "catalogue")[:40])
    return jsonify({"success": success, "message": message, "md5": key, "key": key, "source": item.source,
                    "id": item.id, "title": item.title, "subfolder": validated_subfolder}), (200 if success else 409)


def _public(row):
    try:
        meta = json.loads(row.get("catalogue_meta") or "{}")
    except ValueError:
        meta = {}
    progress = row.get("progress")
    if isinstance(progress, str):
        try:
            progress = json.loads(progress)
        except ValueError:
            progress = None
    relpath = None
    if row.get("filepath"):
        try:
            relpath = os.path.relpath(row["filepath"], str(DOWNLOAD_PATH))
            if relpath.startswith(".."):
                relpath = None
        except ValueError:
            relpath = None
    return {
        "key": row["md5"], "source": row.get("catalogue_source"), "id": row.get("catalogue_id"),
        "source_label": SOURCE_LABELS.get(row.get("catalogue_source"), row.get("catalogue_source")),
        "title": row.get("title") or meta.get("title"), "authors": meta.get("authors") or [], "year": meta.get("year"),
        "cover": meta.get("cover"), "language": meta.get("language"), "kind": meta.get("kind"), "link": meta.get("link"),
        "parts": len(meta.get("files") or []) or None,
        "status": row.get("status"), "progress": progress, "error": row.get("error"), "relpath": relpath,
        "added_at": row.get("added_at"), "completed_at": row.get("completed_at"),
    }


@api_bp.route("/api/catalogue/downloads", methods=["GET"])
@require_auth_with_permissions(allow_downloader=True)
def api_catalogue_downloads():
    refused = _need_multiprocess()
    if refused:
        return refused
    keys = [k for k in (request.args.get("keys") or "").split(",") if len(k) == 32 and k.isalnum()]
    rows = get_queue_ops().get_catalogue_downloads(keys=keys or None)
    return jsonify({"success": True, "items": [_public(r) for r in rows]})
