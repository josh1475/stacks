"""The three providers against recorded answers, and the merged search."""

import json
from urllib.parse import urlencode

import pytest

from conftest import FakeResponse, fixture
from stacks.catalogue import merge, catalogue_key
from stacks.catalogue.base import BadRequest, NotFound
from stacks.catalogue.gutenberg import Gutenberg
from stacks.catalogue.librivox import LibriVox, API as LV, FIELDS
from stacks.catalogue.standardebooks import StandardEbooks, FEED

G_SEARCH = "https://gutendex.com/books/?" + urlencode({"search": "pride prejudice", "copyright": "false"})


def lv_url(**params):
    return LV + "?" + urlencode(dict(params, format="json", coverart=1))


# ---- Project Gutenberg ------------------------------------------------------------------------------------------
def test_gutenberg_search_maps_and_keeps_public_domain_only(make_http):
    p = Gutenberg(make_http({G_SEARCH: FakeResponse(200, fixture("gutendex_search.json"))}))
    items = p.search("pride  prejudice")
    assert [i.id for i in items] == ["1342", "42671"]           # the in-copyright record is left out
    it = items[0]
    assert (it.source, it.title, it.authors, it.language, it.kind) == ("gutenberg", "Pride and Prejudice", ["Austen, Jane"], "en", "ebook")
    assert it.cover == "https://www.gutenberg.org/cache/epub/1342/pg1342.cover.medium.jpg"
    assert it.link == "https://www.gutenberg.org/ebooks/1342"
    assert [(f.ext, f.url) for f in it.formats] == [("epub", "https://www.gutenberg.org/ebooks/1342.epub3.images")]
    d = it.to_dict()
    assert d["key"] == catalogue_key("gutenberg", "1342") and d["formats"] == ["epub"] and d["source_label"] == "Project Gutenberg"


def test_gutenberg_item_and_plan(make_http):
    p = Gutenberg(make_http({"https://gutendex.com/books/1342": FakeResponse(200, fixture("gutendex_item.json"))}))
    it = p.item("1342")
    assert [f.url for f in p.download_plan(it)] == ["https://www.gutenberg.org/ebooks/1342.epub3.images"]


@pytest.mark.parametrize("bad", ["", "abc", "1342; drop", "12345678", "-1", "../1"])
def test_gutenberg_ids_are_numbers(make_http, bad):
    with pytest.raises(BadRequest):
        Gutenberg(make_http({})).item(bad)


def test_gutenberg_in_copyright_item_is_not_found(make_http):
    rec = json.loads(fixture("gutendex_search.json"))["results"][2]
    p = Gutenberg(make_http({"https://gutendex.com/books/99999": FakeResponse(200, json.dumps(rec).encode())}))
    with pytest.raises(NotFound):
        p.item("99999")


# ---- Standard Ebooks --------------------------------------------------------------------------------------------
def test_standardebooks_parses_the_public_feed():
    entries = StandardEbooks.parse(fixture("standardebooks_new_releases.xml"))
    assert [e["id"] for e in entries] == ["henry-wade/the-duke-of-yorks-steps", "edgar-rice-burroughs/a-fighting-man-of-mars",
                                          "arthur-conan-doyle/sir-nigel"]
    e = entries[0]
    assert e["title"] == "The Duke of York’s Steps" and e["authors"] == ["Henry Wade"]
    assert e["epub"].startswith("https://standardebooks.org/ebooks/henry-wade/the-duke-of-yorks-steps/downloads/")
    assert e["epub"].endswith(".epub?source=feed") and "_advanced" not in e["epub"] and "kepub" not in e["epub"]
    assert e["size"] and e["cover"].startswith("https://standardebooks.org/")


def test_standardebooks_search_item_and_memory(make_http, tmp_path):
    seen = tmp_path / "se.json"
    p = StandardEbooks(make_http({FEED: [FakeResponse(200, fixture("standardebooks_new_releases.xml"))]}), seen_path=str(seen))
    found = p.search("conan doyle nigel")
    assert [i.id for i in found] == ["arthur-conan-doyle/sir-nigel"]
    assert found[0].language == "en" and found[0].kind == "ebook"
    assert p.search("wade") and not p.search("tolstoy")
    # remembered on disk: a new process can still describe and download it after it leaves the feed
    p2 = StandardEbooks(make_http({}), seen_path=str(seen))
    it = p2.item("henry-wade/the-duke-of-yorks-steps")
    assert p2.download_plan(it)[0].url.endswith("henry-wade_the-duke-of-yorks-steps.epub?source=feed")


def test_standardebooks_item_outside_the_public_feed_says_why(make_http, tmp_path):
    p = StandardEbooks(make_http({FEED: FakeResponse(200, fixture("standardebooks_new_releases.xml"))}), seen_path=str(tmp_path / "s.json"))
    with pytest.raises(NotFound) as e:
        p.item("jane-austen/pride-and-prejudice")
    assert "patrons" in str(e.value)


@pytest.mark.parametrize("bad", ["", "../etc", "honeypot", "a/b/c/d/e/f", "Jane-Austen/Pride", "a//b"])
def test_standardebooks_ids(bad):
    assert not StandardEbooks(None).valid_id(bad)


@pytest.mark.parametrize("good", ["jane-austen/pride-and-prejudice", "leo-tolstoy/war-and-peace/louise-maude_aylmer-maude"])
def test_standardebooks_good_ids(good):
    assert StandardEbooks(None).valid_id(good)


# ---- LibriVox ---------------------------------------------------------------------------------------------------
def test_librivox_search(make_http):
    p = LibriVox(make_http({lv_url(title="^pride and prejudice", limit=20, fields=FIELDS): FakeResponse(200, fixture("librivox_search.json"))}))
    items = p.search("pride and prejudice")
    assert [i.id for i in items] == ["253", "254", "969"]
    it = items[0]
    assert (it.kind, it.language, it.authors, it.year) == ("audio", "en", ["Jane Austen"], 1813)
    assert it.link == "https://librivox.org/pride-and-prejudice-by-jane-austen/"
    assert it.to_dict()["formats"] == ["mp3"] and it.parts == 37


def test_librivox_search_title_then_author_surname(make_http):
    p = LibriVox(make_http({
        lv_url(title="^pride and prejudice austen", limit=20, fields=FIELDS): FakeResponse(404, b'{"error":"Audiobooks could not be found"}'),
        lv_url(title="^pride and prejudice", limit=20, fields=FIELDS): FakeResponse(200, fixture("librivox_search.json")),
    }))
    assert [i.id for i in p.search("pride and prejudice austen")] == ["253", "254", "969"]


def test_librivox_nothing_found_is_empty(make_http):
    p = LibriVox(make_http({lv_url(title="^zzzz", limit=20, fields=FIELDS): FakeResponse(404, b'{"error":"Audiobooks could not be found"}'),
                            lv_url(author="zzzz", limit=20, fields=FIELDS): FakeResponse(404, b'{"error":"Audiobooks could not be found"}')}))
    assert p.search("zzzz") == []


def test_librivox_item_lists_its_chapters(make_http):
    url = LV + "?" + urlencode({"id": 253, "format": "json", "extended": 1, "coverart": 1})
    p = LibriVox(make_http({url: FakeResponse(200, fixture("librivox_item.json"))}))
    it = p.item("253")
    plan = p.download_plan(it)
    assert len(plan) == 3 and all(f.ext == "mp3" and f.kind == "audio" for f in plan)
    assert plan[0].url == "https://www.archive.org/download/pride_and_prejudice_librivox/prideandprejudice_01-03_austen_64kb.mp3"
    assert plan[0].name and it.cover.startswith("https://")


# ---- merged search ----------------------------------------------------------------------------------------------
def test_merge_groups_one_book_across_catalogues(make_http):
    g = Gutenberg(make_http({G_SEARCH: FakeResponse(200, fixture("gutendex_search.json"))})).search("pride prejudice")
    lv = LibriVox(make_http({lv_url(title="^pride prejudice", limit=20, fields=FIELDS): FakeResponse(200, fixture("librivox_search.json"))}))
    lv_items = lv.search("pride prejudice")
    merged = merge({"gutenberg": g, "librivox": lv_items})
    assert len(merged) == 1                               # one Pride and Prejudice, by Austen
    srcs = [(s["source"], s["id"], s["kind"]) for s in merged[0]["sources"]]
    assert srcs[:2] == [("gutenberg", "1342", "ebook"), ("gutenberg", "42671", "ebook")]
    assert ("librivox", "253", "audio") in srcs
    assert all(s["source_label"] and len(s["key"]) == 32 for s in merged[0]["sources"])


def test_merge_keeps_different_books_apart():
    from stacks.catalogue.base import CatalogueItem
    a = CatalogueItem("gutenberg", "1", "Emma", ["Austen, Jane"])
    b = CatalogueItem("gutenberg", "2", "Emma", ["Someone Else"])
    c = CatalogueItem("standardebooks", "jane-austen/emma", "Emma", ["Jane Austen"])
    merged = merge({"gutenberg": [a, b], "standardebooks": [c]})
    assert [[s["id"] for s in m["sources"]] for m in merged] == [["jane-austen/emma", "1"], ["2"]]
