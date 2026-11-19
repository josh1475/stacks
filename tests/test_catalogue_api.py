"""The catalogue API, CATALOGUE_ONLY, the database migration and the download worker's dispatch."""

import json
import sqlite3
import threading

import pytest

import stacks.catalogue as catalogue
from conftest import ADMIN_KEY, DOWNLOADER_KEY
from stacks.catalogue.base import CatalogueItem, FileFormat, NotFound, catalogue_key

ADMIN = {"X-API-Key": ADMIN_KEY}
DL = {"X-API-Key": DOWNLOADER_KEY}


class FakeProvider:
    name = "gutenberg"

    def __init__(self, items):
        self.items = items

    def valid_id(self, i):
        return str(i).isdigit()

    def search(self, q, limit=20):
        return list(self.items.values())

    def item(self, i):
        if i not in self.items:
            raise NotFound("no such book")
        return self.items[i]

    def download_plan(self, it):
        return it.formats[:1]


@pytest.fixture
def fake_gutenberg(monkeypatch):
    it = CatalogueItem("gutenberg", "1342", "Pride and Prejudice", ["Austen, Jane"], language="en",
                       formats=[FileFormat("ebook", "epub", "https://www.gutenberg.org/ebooks/1342.epub3.images")])
    prov = FakeProvider({"1342": it})
    monkeypatch.setattr(catalogue, "get_provider", lambda source, http=None: prov)
    import stacks.api.catalogue as api
    monkeypatch.setattr(api, "get_provider", lambda source, http=None: prov)
    return prov


def rows(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM downloads")]
    finally:
        conn.close()


# ---- CATALOGUE_ONLY ---------------------------------------------------------------------------------------------
def test_md5_queue_is_refused_in_catalogue_only_mode(app, monkeypatch, fresh_db):
    monkeypatch.setenv("CATALOGUE_ONLY", "true")
    r = app.test_client().post("/api/queue/add", json={"md5": "d6e1dc51a50726f00ec438af21952a45"}, headers=ADMIN)
    assert r.status_code == 403
    assert r.get_json()["error"] == "this server only downloads from public-domain catalogues"
    assert rows(fresh_db) == []


def test_md5_queue_still_works_by_default(app, fresh_db):
    r = app.test_client().post("/api/queue/add", json={"md5": "d6e1dc51a50726f00ec438af21952a45"}, headers=DL)
    assert r.status_code == 200 and r.get_json()["success"]
    assert rows(fresh_db)[0]["status"] == "pending_scrape"


def test_key_test_is_refused_in_catalogue_only_mode(app, monkeypatch):
    monkeypatch.setenv("CATALOGUE_ONLY", "1")
    r = app.test_client().post("/api/config/test_key", json={"key": "x"}, headers=ADMIN)
    assert r.status_code == 403


def test_env_overrides_config(config_file, monkeypatch):
    from stacks.config.config import Config
    c = Config(str(config_file))
    assert c.get("catalogue", "only") is False
    monkeypatch.setenv("CATALOGUE_ONLY", "true")
    assert c.get("catalogue", "only") is True
    monkeypatch.setenv("DOWNLOADER_API_KEY", "E" * 32)
    assert c.get("api", "downloader_key") == "E" * 32
    monkeypatch.setenv("DOWNLOADER_API_KEY", "too-short")          # an invalid key is ignored, not used
    assert c.get("api", "downloader_key") == DOWNLOADER_KEY
    assert "E" * 32 not in config_file.read_text()                  # never written to the file


def test_config_page_shows_the_mode_in_effect(app, monkeypatch):
    monkeypatch.setenv("CATALOGUE_ONLY", "true")
    cfg = app.test_client().get("/api/config", headers=ADMIN).get_json()
    assert cfg["catalogue"]["only"] is True and cfg["catalogue"]["only_from_env"] is True
    assert cfg["catalogue"]["gutenberg"] is True


def test_debug_mode_refuses_catalogue_only(config_file, monkeypatch):
    monkeypatch.setenv("CATALOGUE_ONLY", "true")
    from stacks.server.webserver import create_app
    with pytest.raises(RuntimeError):
        create_app(str(config_file), debug_mode=True)


# ---- the catalogue endpoints ------------------------------------------------------------------------------------
def test_downloader_key_can_search_and_queue(app, fake_gutenberg, fresh_db, monkeypatch):
    monkeypatch.setenv("CATALOGUE_ONLY", "true")
    c = app.test_client()
    r = c.get("/api/catalogue/search?q=pride&source=gutenberg", headers=DL)
    assert r.status_code == 200
    res = r.get_json()["results"]
    assert res[0]["title"] == "Pride and Prejudice" and res[0]["sources"][0]["source_label"] == "Project Gutenberg"

    r = c.post("/api/catalogue/queue", json={"source": "gutenberg", "id": "1342"}, headers=DL)
    assert r.status_code == 200, r.get_json()
    key = catalogue_key("gutenberg", "1342")
    assert r.get_json()["key"] == key
    row = rows(fresh_db)[0]
    assert row["md5"] == key and row["status"] == "queued" and row["catalogue_source"] == "gutenberg"
    assert row["catalogue_id"] == "1342" and row["title"] == "Pride and Prejudice"
    meta = json.loads(row["catalogue_meta"])
    assert meta["files"][0]["url"] == "https://www.gutenberg.org/ebooks/1342.epub3.images"
    assert json.loads(row["mirrors"])[0]["domain"] == "www.gutenberg.org"

    again = c.post("/api/catalogue/queue", json={"source": "gutenberg", "id": "1342"}, headers=DL)
    assert again.status_code == 409 and "Already in queue" in again.get_json()["message"]

    d = c.get(f"/api/catalogue/downloads?keys={key}", headers=DL).get_json()["items"]
    assert d[0]["status"] == "queued" and d[0]["source_label"] == "Project Gutenberg" and d[0]["title"] == "Pride and Prejudice"

    # the existing status page shows it with its source and title (admin only, as before)
    assert c.get("/api/status", headers=DL).status_code == 403
    st = c.get("/api/status", headers=ADMIN).get_json()
    assert st["queue"][0]["title"] == "Pride and Prejudice" and st["queue"][0]["catalogue_source"] == "gutenberg"


def test_queue_never_takes_a_url(app, fake_gutenberg, fresh_db):
    c = app.test_client()
    for body in ({"source": "https://annas-archive.gl", "id": "1342"}, {"source": "gutenberg", "id": "https://evil/x.epub"},
                 {"source": "opds", "id": "1"}, {"source": "gutenberg"}):
        r = c.post("/api/catalogue/queue", json=body, headers=DL)
        assert r.status_code == 400, body
    assert rows(fresh_db) == []


def test_unknown_item_is_404(app, fake_gutenberg):
    r = app.test_client().post("/api/catalogue/queue", json={"source": "gutenberg", "id": "7"}, headers=DL)
    assert r.status_code == 404


def test_switched_off_source_is_refused(app, fake_gutenberg, config_file):
    app.stacks_config.set("catalogue", "gutenberg", value=False)
    c = app.test_client()
    assert c.post("/api/catalogue/queue", json={"source": "gutenberg", "id": "1342"}, headers=DL).status_code == 400
    assert c.get("/api/catalogue/search?q=pride&source=gutenberg", headers=DL).status_code == 400


def test_catalogue_needs_a_key(app):
    assert app.test_client().get("/api/catalogue/search?q=pride").status_code == 401


def test_retry_of_a_failed_catalogue_item_goes_back_to_queued(fresh_db):
    from stacks.coordinator.queue_ops import QueueOperations
    ops = QueueOperations()
    key = catalogue_key("librivox", "253")
    ops.add_catalogue_download(key, "librivox", "253", "Pride and Prejudice", {"files": []},
                               {"url": "https://www.archive.org/download/x/1.mp3", "domain": "www.archive.org", "type": "catalogue"})
    conn = sqlite3.connect(fresh_db)
    conn.execute("UPDATE downloads SET status='failed' WHERE md5=?", (key,))
    conn.commit()
    conn.close()
    assert ops.retry_failed(key)[0]
    assert rows(fresh_db)[0]["status"] == "queued"


# ---- migration --------------------------------------------------------------------------------------------------
def test_migration_adds_catalogue_columns_to_an_old_database(tmp_path, monkeypatch):
    from stacks.coordinator import database
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE downloads (id INTEGER PRIMARY KEY AUTOINCREMENT, md5 TEXT NOT NULL UNIQUE, title TEXT,
                    source TEXT, subfolder TEXT, status TEXT NOT NULL DEFAULT 'pending_scrape', added_at TEXT NOT NULL,
                    completed_at TEXT, filename TEXT, mirrors TEXT, assigned_worker TEXT, assigned_mirror TEXT,
                    success INTEGER, filepath TEXT, error TEXT, used_fast_download INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("INSERT INTO downloads (md5, status, added_at) VALUES ('d6e1dc51a50726f00ec438af21952a45', 'completed', 'x')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(database, "DATABASE_PATH", db)
    database.init_database()
    database.init_database()                                       # safe to run again
    conn = sqlite3.connect(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(downloads)")}
    old = conn.execute("SELECT catalogue_source, catalogue_id, catalogue_meta, status FROM downloads").fetchone()
    idx = {r[1] for r in conn.execute("PRAGMA index_list(downloads)")}
    conn.close()
    assert {"catalogue_source", "catalogue_id", "catalogue_meta", "command", "progress"} <= cols
    assert old == (None, None, None, "completed")                  # existing rows are untouched
    assert "idx_downloads_catalogue" in idx


# ---- dispatch ---------------------------------------------------------------------------------------------------
def _queue_two(ops):
    key = catalogue_key("gutenberg", "1342")
    ops.add_catalogue_download(key, "gutenberg", "1342", "Pride and Prejudice",
                               {"source": "gutenberg", "id": "1342", "title": "Pride and Prejudice", "authors": [], "kind": "ebook",
                                "files": [{"url": "https://www.gutenberg.org/ebooks/1342.epub3.images", "ext": "epub"}]},
                               {"url": "https://www.gutenberg.org/ebooks/1342.epub3.images", "domain": "www.gutenberg.org", "type": "catalogue"})
    ops.add_download("d6e1dc51a50726f00ec438af21952a45")
    conn = __import__("stacks.coordinator.database", fromlist=["x"]).get_connection()
    conn.execute("""UPDATE downloads SET status='queued', mirrors=? WHERE md5='d6e1dc51a50726f00ec438af21952a45'""",
                 (json.dumps([{"url": "https://mirror.example/x", "domain": "mirror.example"}]),))
    conn.commit()
    conn.close()
    return key


def test_catalogue_only_claims_only_catalogue_items(fresh_db):
    from stacks.coordinator.queue_ops import QueueOperations
    ops = QueueOperations()
    key = _queue_two(ops)
    job = ops.claim_download_job("w1", catalogue_only=True)
    assert job["md5"] == key and job["assigned_mirror"]["domain"] == "www.gutenberg.org"
    assert ops.claim_download_job("w2", catalogue_only=True) is None    # the md5 item is never handed out


def test_worker_sends_catalogue_jobs_to_the_catalogue_downloader(fresh_db, config_file, monkeypatch, tmp_path):
    """The real worker loop: a catalogue job is downloaded by the catalogue downloader and completed, and Anna's
    Archive's downloader is never built."""
    import stacks.coordinator.download_worker as dw
    import stacks.catalogue.worker as cw
    from stacks.coordinator.queue_ops import QueueOperations

    monkeypatch.setenv("CATALOGUE_ONLY", "true")
    ops = QueueOperations()
    key = _queue_two(ops)
    stop = threading.Event()

    def no_aa(*a, **k):
        raise AssertionError("Anna's Archive's downloader must not be built for a catalogue job")
    monkeypatch.setattr(dw, "create_downloader", no_aa)

    class FakeDownloader:
        def download(self, k, meta, plan, subfolder=None):
            assert k == key and plan[0]["ext"] == "epub"
            return tmp_path / "Pride and Prejudice [gutenberg-1342].epub"

        def discard(self, k):
            pass
    monkeypatch.setattr(cw, "make_downloader", lambda config, cb, http=None: FakeDownloader())

    real_complete = QueueOperations.complete_download

    def complete(self, *a, **k):
        out = real_complete(self, *a, **k)
        stop.set()
        return out
    monkeypatch.setattr(QueueOperations, "complete_download", complete)
    monkeypatch.setattr(dw.signal, "signal", lambda *a: None)

    t = threading.Thread(target=dw.download_worker_process, args=("download-0", config_file, stop), daemon=True)
    t.start()
    t.join(timeout=20)
    assert not t.is_alive()
    by_key = {r["md5"]: r for r in rows(fresh_db)}
    assert by_key[key]["status"] == "completed" and by_key[key]["filepath"].endswith(".epub")
    assert by_key["d6e1dc51a50726f00ec438af21952a45"]["status"] == "queued"     # never touched
