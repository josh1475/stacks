"""Anna's Archive page scraping: a Cloudflare 403 is solved with FlareSolverr, not fatal."""

import logging

import pytest

from stacks.downloader.html import _get_download_links_single_domain

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

    def get(self, url, timeout=None):
        self.called.append(url)
        return self.resp


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
