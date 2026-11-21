"""The allowlist, the redirect checks and the polite pacing of stacks.catalogue.http."""

import pytest

from conftest import FakeResponse
from stacks.catalogue.base import Busy, NotFound
from stacks.catalogue.http import NotAllowed, check_url, host_allowed, USER_AGENT


@pytest.mark.parametrize("host", ["gutendex.com", "www.gutenberg.org", "gutenberg.org", "standardebooks.org",
                                  "librivox.org", "archive.org", "www.archive.org", "ia800300.us.archive.org",
                                  "dn710107.ca.archive.org", "IA801.US.ARCHIVE.ORG"])
def test_allowed_hosts(host):
    assert host_allowed(host)


@pytest.mark.parametrize("host", ["annas-archive.gl", "libgen.rs", "z-library.sk", "evil.com", "gutenberg.org.evil.com",
                                  "evilgutenberg.org", "scholar.archive.org", "web.archive.org", "ia800300.archive.org",
                                  "x.us.archive.org", "localhost", "127.0.0.1", ""])
def test_other_hosts_refused(host):
    assert not host_allowed(host)


@pytest.mark.parametrize("url", ["http://www.gutenberg.org/ebooks/1.epub", "ftp://gutenberg.org/x",
                                 "https://www.gutenberg.org:8443/x", "https://user:pw@www.gutenberg.org/x",
                                 "https://annas-archive.gl/md5/abc", "file:///etc/passwd", "not a url"])
def test_check_url_refuses(url):
    with pytest.raises(NotAllowed):
        check_url(url)


def test_check_url_accepts_https_allowed_host():
    assert check_url("https://www.gutenberg.org/ebooks/1342.epub3.images")


def test_honest_user_agent(make_http):
    http = make_http({"https://gutendex.com/books/1": FakeResponse(200, b"{}")})
    assert "Stacks-Catalogue" in http.session.headers["User-Agent"] == USER_AGENT
    assert "+https://" in USER_AGENT


def test_redirect_to_another_host_is_refused(make_http):
    http = make_http({"https://www.gutenberg.org/ebooks/1.epub3.images":
                      FakeResponse(302, headers={"Location": "https://annas-archive.gl/fast"})})
    with pytest.raises(NotAllowed):
        http.open("https://www.gutenberg.org/ebooks/1.epub3.images")
    assert [u for u, _ in http.session.calls] == ["https://www.gutenberg.org/ebooks/1.epub3.images"]


def test_redirect_downgrade_to_http_is_refused(make_http):
    http = make_http({"https://archive.org/download/x/y.mp3":
                      FakeResponse(302, headers={"Location": "http://ia800300.us.archive.org/y.mp3"})})
    with pytest.raises(NotAllowed):
        http.open("https://archive.org/download/x/y.mp3")


def test_each_allowed_hop_is_followed(make_http):
    http = make_http({
        "https://www.archive.org/download/x/y.mp3": FakeResponse(302, headers={"Location": "https://archive.org/download/x/y.mp3"}),
        "https://archive.org/download/x/y.mp3": FakeResponse(302, headers={"Location": "https://dn710107.ca.archive.org/0/items/x/y.mp3"}),
        "https://dn710107.ca.archive.org/0/items/x/y.mp3": FakeResponse(200, b"ID3data"),
    })
    r = http.open("https://www.archive.org/download/x/y.mp3")
    assert r.body == b"ID3data"
    assert len(http.session.calls) == 3


def test_relative_redirect_is_resolved_and_checked(make_http):
    http = make_http({
        "https://www.gutenberg.org/ebooks/1342.epub3.images": FakeResponse(302, headers={"Location": "/cache/epub/1342/pg1342-images-3.epub"}),
        "https://www.gutenberg.org/cache/epub/1342/pg1342-images-3.epub": FakeResponse(200, b"PK"),
    })
    assert http.open("https://www.gutenberg.org/ebooks/1342.epub3.images").status_code == 200


def test_429_with_retry_after_holds_the_host(make_http, clock):
    http = make_http({"https://librivox.org/api/feed/audiobooks/?id=1":
                      FakeResponse(429, headers={"Retry-After": "120"})})
    with pytest.raises(Busy) as e:
        http.get_json("https://librivox.org/api/feed/audiobooks/?id=1")
    assert e.value.retry_after >= 120
    # while held, nothing is sent: an interactive call fails fast instead of waiting two minutes
    with pytest.raises(Busy):
        http.get_json("https://librivox.org/api/feed/audiobooks/?id=1")
    assert len(http.session.calls) == 1


def test_503_without_retry_after_backs_off_exponentially(make_http, clock):
    url = "https://gutendex.com/books/1"
    http = make_http({url: [FakeResponse(503), FakeResponse(503)]})
    with pytest.raises(Busy) as first:
        http.get_json(url)
    clock.t += 10_000
    with pytest.raises(Busy) as second:
        http.get_json(url)
    assert second.value.retry_after > first.value.retry_after * 1.5


def test_requests_to_one_host_are_paced(make_http, clock):
    urls = [f"https://www.gutenberg.org/x{i}" for i in range(3)]
    http = make_http({u: FakeResponse(200, b"{}") for u in urls})
    for u in urls:
        http.open(u, max_wait=10)
    assert sum(clock.slept) >= 5.0          # 2.5 s apart for gutenberg.org


def test_404_is_not_found(make_http):
    http = make_http({"https://gutendex.com/books/7": FakeResponse(404, b'{"detail":"Not found."}')})
    with pytest.raises(NotFound):
        http.get_json("https://gutendex.com/books/7")


def test_json_answers_are_cached(make_http):
    url = "https://gutendex.com/books/1"
    http = make_http({url: [FakeResponse(200, b'{"id": 1}')]})
    assert http.get_json(url, ttl=60) == {"id": 1}
    assert http.get_json(url, ttl=60) == {"id": 1}
    assert len(http.session.calls) == 1
