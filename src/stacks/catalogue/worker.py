"""
A catalogue job in the download worker (stacks.coordinator.download_worker hands it over here).

Catalogue jobs never touch Anna's Archive's downloader: the worker sends any row with catalogue_source here before it
would build one. Completion, failure, cancel-and-requeue and cancel-and-remove are reported through the same
QueueOperations calls as every other download, so the queue, history, retry and status pages show them as usual.
"""

import json
import logging

from stacks.catalogue.base import Busy, CatalogueError
from stacks.catalogue.downloader import CatalogueDownloader, Cancelled
from stacks.constants import DOWNLOAD_PATH, PROJECT_ROOT

logger = logging.getLogger("catalogue")


def make_downloader(config, progress_callback, http=None):
    from stacks.catalogue import get_http
    incomplete = config.get("downloads", "incomplete_folder_path", default="/download/incomplete")
    max_mb = config.get("catalogue", "max_size_mb", default=4096)
    return CatalogueDownloader(http or get_http(), DOWNLOAD_PATH, PROJECT_ROOT / incomplete.lstrip("/"),
                               progress_callback=progress_callback, max_bytes=int(max_mb) << 20)


def run_catalogue_job(job, config, queue_ops, worker_id, progress_callback, cancel_flag, stop_event, log=logger,
                      downloader=None):
    """Download one catalogue job and report it. Returns "stop" when the worker is shutting down (the job is left for
    the startup cleanup to requeue), else "done". CANCEL_FLAG() is the worker's current cancel command."""
    md5 = job["md5"]
    try:
        meta = json.loads(job.get("catalogue_meta") or "{}")
    except ValueError:
        meta = {}
    plan = meta.get("files") or []
    downloader = downloader or make_downloader(config, progress_callback)
    success, filepath, error = False, None, None
    log.info(f"Catalogue download: {meta.get('source')}:{meta.get('id')} {meta.get('title')!r} ({len(plan)} file(s))")
    try:
        filepath = downloader.download(md5, meta, plan, subfolder=job.get("subfolder"))
        success = True
    except Cancelled:
        error = "cancelled"
    except Busy as e:
        error = str(e)
    except CatalogueError as e:
        error = str(e)
    except OSError as e:
        error = f"couldn't save it ({type(e).__name__})"
    except Exception as e:                 # noqa: keep the worker alive; the job fails with a reason
        log.error(f"Catalogue download error: {e}", exc_info=True)
        error = str(e) or type(e).__name__

    if stop_event.is_set():
        log.info(f"Worker stopping, leaving catalogue job {md5} for cleanup")
        return "stop"
    flag = cancel_flag()
    if flag == "cancel_requeue":
        queue_ops.requeue_download(md5, worker_id)          # partial files are kept: it resumes
        return "done"
    if flag == "cancel_remove":
        downloader.discard(md5)
        queue_ops.remove_active_download(md5, worker_id)
        return "done"
    queue_ops.complete_download(worker_id=worker_id, md5=md5, success=success,
                                filepath=str(filepath) if filepath else None, error=error)
    if success:
        log.info(f"Catalogue download complete: {meta.get('title')} -> {filepath}")
    else:
        log.warning(f"Catalogue download failed: {meta.get('title')} - {error}")
    return "done"
