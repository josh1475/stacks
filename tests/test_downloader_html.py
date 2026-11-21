"""Anna's Archive page scraping: a Cloudflare 403 is solved with FlareSolverr, not fatal.
Plus Library Genesis first: the links come from libgen.li's keyed get.php in under a
second, and Anna's Archive (FlareSolverr, domain rotation) is only the fallback."""

import logging

import pytest

import stacks.downloader.html as html_module
from stacks.downloader.html import _get_download_links_single_domain, get_download_links
from stacks.downloader.mirrors import _filename_from_disposition, download_from_mirror

PANEL = """
<div id="md5-panel-downloads">
  <ul>
    <li class="list-disc"><a href="https://partner.example/slow_download/x/0/0">Partner Server</a> (slightly faster but with waitlist)</li>
    <li class="list-disc"><a href="https://partner.example/slow_download/y/0/0">Another Server</a> (no waitlist)</li>
  </ul>
  <ul class="js-show-external">
    <li><a href="http://libgen.li/ads.php?md5=x">libgen.li</a></li>
    <li><a href="http://something.onion/get?md5=x">tor mirror</a></li>
  </ul>
</div>
"""


class Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class Session:
    def __init__(self, resp):
        self.resp = resp
        self.called = []

    def get(self, url, timeout=None, **kwargs):
        self.called.append(url)
        return self.resp


ADS_PAGE = """
<td><b>Title:</b></td><td>Workbook for Atomic Habits</td>
<a href="get.php?md5=%s&amp;key=KEY123">GET</a>
"""

ADS_PAGE_WITHOUT_LINK = "<td><b>Title:</b></td><td>Something</td><p>No mirrors today.</p>"


class DispositionResp:
    ok = True
    headers = {'Content-Disposition': 'attachment; filename="Workbook for James Clear&_039_s '
               'Atomic Habits{Works, Companion}(2021, James Clear){106283071} libgen.li.epub"'}

    def close(self):
        pass


def make_d(session, solved=(True, {}, PANEL)):
    class D:
        pass

    d = D()
    d.logger = logging.getLogger("test")
    d.session = session
    d.flaresolverr_url = "http://flaresolverr:8191"
    d.flaresolverr_timeout = 60000
    d.prefer_title_naming = False
    d.include_hash = "none"
    d.solve_with_flaresolverr = lambda url: solved
    return d


def test_a_cloudflare_page_is_solved_and_parsed():
    d = make_d(Session(Resp(403)))
    solved_with = []
    d.solve_with_flaresolverr = lambda url: (solved_with.append(url) or (True, {}, PANEL))
    filename, links = _get_download_links_single_domain(d, "a" * 32, "annas-archive.gl")
    assert solved_with == ["https://annas-archive.gl/md5/" + "a" * 32]
    slow = [l for l in links if l["type"] == "slow_download"]
    ext = [l for l in links if l["type"] == "external_mirror"]
    assert len(slow) == 1 and "no waitlist" not in slow[0]["text"].lower()   # the waitlisted server was skipped
    assert len(ext) == 1 and ext[0]["domain"] == "libgen.li"                 # the onion mirror was skipped


def test_without_a_solver_answer_the_rotation_moves_on():
    d = make_d(Session(Resp(403)), solved=(False, {}, None))
    with pytest.raises(RuntimeError):
        _get_download_links_single_domain(d, "a" * 32, "annas-archive.gl")


def test_a_normal_page_still_parses_alone():
    d = make_d(Session(Resp(200, PANEL)))
    d.solve_with_flaresolverr = lambda url: (_ for _ in ()).throw(AssertionError("solver not needed"))
    filename, links = _get_download_links_single_domain(d, "a" * 32, "annas-archive.gl")
    assert len(links) == 2


def test_libgen_is_asked_first_and_annas_archive_never_hears_of_it(monkeypatch):
    # probed 2026-11-21: an AA attempt costs a FlareSolverr round-trip and ends in a panel-less
    # guard page, so libgen's sub-second plain-HTTP answer goes first
    d = make_d(Session(Resp(200, ADS_PAGE % ("b" * 32))))
    monkeypatch.setattr(
        html_module, "try_domains_until_success",
        lambda fn, d_, md5: (_ for _ in ()).throw(AssertionError("Anna's Archive was asked first")),
    )
    filename, links = get_download_links(d, "b" * 32)
    assert d.session.called == [f"http://libgen.li/ads.php?md5={'b' * 32}"]
    assert len(links) == 1
    link = links[0]
    assert link["type"] == "libgen_get"
    assert link["url"] == f"http://libgen.li/get.php?md5={'b' * 32}&key=KEY123"
    assert link["domain"] == "libgen.li"
    assert filename == "Workbook for Atomic Habits"          # named from the record page


def test_when_libgen_has_no_link_annas_archive_is_asked(monkeypatch):
    d = make_d(Session(Resp(200, ADS_PAGE_WITHOUT_LINK)))
    aa_links = [{"type": "slow_download", "url": "https://partner.example/slow_download/y/0/0"}]
    monkeypatch.setattr(
        html_module, "try_domains_until_success",
        lambda fn, d_, md5: ("A Panel Title", aa_links),
    )
    filename, links = get_download_links(d, "c" * 32)
    assert filename == "A Panel Title" and links == aa_links


def test_when_neither_mirror_yields_anything_the_answer_is_honest(monkeypatch):
    d = make_d(Session(Resp(200, ADS_PAGE_WITHOUT_LINK)))
    monkeypatch.setattr(
        html_module, "try_domains_until_success",
        lambda fn, d_, md5: (_ for _ in ()).throw(RuntimeError("all domains failed")),
    )
    filename, links = get_download_links(d, "c" * 32)
    assert filename == "Unknown" and links == []


def test_a_libgen_get_stream_takes_its_name_from_the_cdn_header():
    d = type("D", (), {})()
    d.logger = logging.getLogger("test")
    d.session = Session(DispositionResp())
    used = {}
    d.download_direct = lambda url, title=None, **kw: used.update(url=url, title=title) or "/books/out.epub"

    filepath = download_from_mirror(d, "http://libgen.li/get.php?md5=" + "d" * 32 + "&key=K", "libgen_get", "d" * 32)

    assert filepath == "/books/out.epub"
    assert used["title"] == "Workbook for James Clear's Atomic Habits{Works, Companion}(2021, James Clear){106283071} libgen.li.epub"


def test_a_missing_disposition_leaves_the_scraper_title_alone():
    assert _filename_from_disposition("") is None
    resp = type("R", (), {"ok": True, "headers": {}, "close": lambda self: None})()
    d = type("D", (), {})()
    d.logger = logging.getLogger("test")
    d.session = Session(resp)
    used = {}
    d.download_direct = lambda url, title=None, **kw: used.update(title=title) or "/books/out.pdf"

    download_from_mirror(d, "http://libgen.li/get.php?md5=e", "libgen_get", "e", title="A Plain Title.pdf")

    assert used["title"] == "A Plain Title.pdf"
