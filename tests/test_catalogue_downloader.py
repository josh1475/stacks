"""The catalogue downloader: streaming, resume, size cap, file sniffing, naming, cancel."""

import pytest

from conftest import FakeResponse
from stacks.catalogue.base import CatalogueError
from stacks.catalogue.downloader import CatalogueDownloader, Cancelled, sniff
from stacks.catalogue.http import NotAllowed

EPUB = b"PK\x03\x04" + b"\x00" * 26 + b"mimetypeapplication/epub+zip" + b"x" * 5000
MP3 = b"ID3\x04\x00" + b"m" * 3000
HTML = b"<!DOCTYPE html><html><body>Please wait</body></html>"
G = "https://www.gutenberg.org/ebooks/1342.epub3.images"


def meta(**kw):
    d = {"source": "gutenberg", "id": "1342", "title": "Pride and Prejudice", "authors": ["Austen, Jane"], "kind": "ebook"}
    d.update(kw)
    return d


def make(make_http, tmp_path, routes, progress=None, max_bytes=1 << 30):
    return CatalogueDownloader(make_http(routes), tmp_path / "download", tmp_path / "download" / "incomplete",
                               progress_callback=progress, max_bytes=max_bytes)


def test_sniff():
    assert sniff(EPUB[:4096]) == "epub"
    assert sniff(b"PK\x03\x04plainzip") == "zip"
    assert sniff(MP3) == "mp3" and sniff(b"\xff\xfb\x90\x00") == "mp3"
    assert sniff(b"%PDF-1.7") == "pdf"
    assert sniff(HTML) == "html" and sniff(b"\n  <html lang=en>") == "html"
    assert sniff(b"hello") is None


def test_downloads_an_epub_with_progress(make_http, tmp_path):
    seen = []
    d = make(make_http, tmp_path, {G: FakeResponse(200, EPUB, {"Content-Length": str(len(EPUB))})}, progress=lambda p: seen.append(p) or True)
    path = d.download("k1", meta(), [{"url": G, "ext": "epub"}])
    assert path.name == "Pride and Prejudice - Austen, Jane [gutenberg-1342].epub"
    assert path.read_bytes() == EPUB
    assert seen and seen[-1]["percent"] == 100.0 and seen[-1]["downloaded"] == len(EPUB)
    assert not (tmp_path / "download" / "incomplete" / "catalogue-k1").exists()


def test_subfolder_and_unique_names(make_http, tmp_path):
    d = make(make_http, tmp_path, {G: [FakeResponse(200, EPUB), FakeResponse(200, EPUB)]})
    a = d.download("k1", meta(), [{"url": G, "ext": "epub"}], subfolder="/Books")
    b = d.download("k2", meta(), [{"url": G, "ext": "epub"}], subfolder="/Books")
    assert a.parent.name == "Books" and b.name.endswith("(2).epub")


def test_web_page_instead_of_the_file_fails(make_http, tmp_path):
    d = make(make_http, tmp_path, {G: FakeResponse(200, HTML, {"Content-Type": "text/html"})})
    with pytest.raises(CatalogueError, match="web page"):
        d.download("k1", meta(), [{"url": G, "ext": "epub"}])
    assert not list((tmp_path / "download").glob("*.epub"))


def test_plain_zip_is_not_an_epub(make_http, tmp_path):
    d = make(make_http, tmp_path, {G: FakeResponse(200, b"PK\x03\x04" + b"z" * 400)})
    with pytest.raises(CatalogueError, match="EPUB"):
        d.download("k1", meta(), [{"url": G, "ext": "epub"}])


def test_size_cap_from_content_length(make_http, tmp_path):
    d = make(make_http, tmp_path, {G: FakeResponse(200, EPUB, {"Content-Length": str(10 << 20)})}, max_bytes=1 << 20)
    with pytest.raises(CatalogueError, match="limit"):
        d.download("k1", meta(), [{"url": G, "ext": "epub"}])


def test_size_cap_while_streaming(make_http, tmp_path):
    big = EPUB + b"y" * (300 << 10)
    d = make(make_http, tmp_path, {G: FakeResponse(200, big)}, max_bytes=100 << 10)
    with pytest.raises(CatalogueError, match="limit"):
        d.download("k1", meta(), [{"url": G, "ext": "epub"}])


def test_resumes_with_range_when_the_host_supports_it(make_http, tmp_path):
    part_dir = tmp_path / "download" / "incomplete" / "catalogue-k1"
    part_dir.mkdir(parents=True)
    (part_dir / "001.part").write_bytes(EPUB[:1000])
    calls = []

    def answer(url, headers):
        calls.append(headers.get("Range"))
        return FakeResponse(206, EPUB[1000:], {"Content-Length": str(len(EPUB) - 1000)})
    d = make(make_http, tmp_path, {G: answer})
    path = d.download("k1", meta(), [{"url": G, "ext": "epub"}])
    assert calls == ["bytes=1000-"] and path.read_bytes() == EPUB


def test_starts_again_when_the_host_ignores_the_range(make_http, tmp_path):
    part_dir = tmp_path / "download" / "incomplete" / "catalogue-k1"
    part_dir.mkdir(parents=True)
    (part_dir / "001.part").write_bytes(b"garbage" * 10)
    d = make(make_http, tmp_path, {G: FakeResponse(200, EPUB, {"Content-Length": str(len(EPUB))})})
    assert d.download("k1", meta(), [{"url": G, "ext": "epub"}]).read_bytes() == EPUB


def test_audiobook_parts_land_in_a_folder_in_order(make_http, tmp_path):
    urls = [f"https://www.archive.org/download/p/ch{i}.mp3" for i in range(1, 4)]
    d = make(make_http, tmp_path, {u: FakeResponse(200, MP3) for u in urls})
    plan = [{"url": u, "ext": "mp3", "name": f"Chapters {i}: one/two"} for i, u in enumerate(urls, 1)]
    path = d.download("k9", meta(source="librivox", id="253", authors=["Jane Austen"], kind="audio"), plan)
    assert path.is_dir() and path.name == "Pride and Prejudice - Jane Austen [librivox-253]"
    assert sorted(p.name for p in path.iterdir()) == ["001 - Chapters 1 one two.mp3", "002 - Chapters 2 one two.mp3",
                                                      "003 - Chapters 3 one two.mp3"]


def test_finished_parts_are_not_fetched_again(make_http, tmp_path):
    urls = [f"https://www.archive.org/download/p/ch{i}.mp3" for i in range(1, 3)]
    part_dir = tmp_path / "download" / "incomplete" / "catalogue-k9"
    part_dir.mkdir(parents=True)
    (part_dir / "001.mp3").write_bytes(MP3)
    d = make(make_http, tmp_path, {urls[1]: FakeResponse(200, MP3)})       # (a request for ch1 would fail the test)
    path = d.download("k9", meta(kind="audio"), [{"url": u, "ext": "mp3"} for u in urls])
    assert len(list(path.iterdir())) == 2


def test_cancel_keeps_the_partial_for_a_resume(make_http, tmp_path):
    d = make(make_http, tmp_path, {G: FakeResponse(200, EPUB)}, progress=lambda p: False)
    d.clock = iter(range(0, 1000, 1)).__next__
    with pytest.raises(Cancelled):
        d.download("k1", meta(), [{"url": G, "ext": "epub"}])
    assert (tmp_path / "download" / "incomplete" / "catalogue-k1" / "001.part").exists()


def test_a_plan_url_off_the_allowlist_is_refused(make_http, tmp_path):
    d = make(make_http, tmp_path, {})
    with pytest.raises(NotAllowed):
        d.download("k1", meta(), [{"url": "https://annas-archive.gl/x.epub", "ext": "epub"}])
