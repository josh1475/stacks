# Public-domain catalogues and Catalogue mode

This fork can also download from three public-domain catalogues:

| Source | What | How Stacks finds it | Where files come from |
| --- | --- | --- | --- |
| **Project Gutenberg** | Public-domain ebooks (EPUB) | [Gutendex](https://gutendex.com) (`copyright=false` only) | `www.gutenberg.org` |
| **Standard Ebooks** | Carefully produced public-domain ebooks (EPUB, CC0) | its public **New Releases** Atom feed | `standardebooks.org` |
| **LibriVox** | Public-domain audiobooks (MP3 chapters) | the [LibriVox API](https://librivox.org/api/info) | `archive.org` and its storage nodes |

Catalogue items go into the same queue as everything else. The queue is keyed on md5, so a catalogue item's key is
`md5("source:id")` (for example `md5("gutenberg:1342")`). The queue, the history (with Retry), the progress bar and
`/api/status` show them with their title and source.

## Using it

- **Web page:** Downloads tab → *Public-domain catalogues*: search, then click the catalogue you want.
- **Browser:** install `web/tamper/stacks_catalogue.user.js` (*Catalogue Script* in the page footer). It adds a
  *Download* button to a book's page on gutenberg.org, standardebooks.org and librivox.org. Use the Downloader key.
- **API:** see below. Only a catalogue name and that catalogue's own id are ever accepted; there is no way to give
  Stacks a URL to download.
- **Settings → Catalogues:** switch each catalogue on or off, and set the largest item (MB).

Ebooks land in the download folder as `Title - Author [gutenberg-1342].epub`. A LibriVox audiobook lands as a folder
`Title - Author [librivox-253]/` holding `001 - <chapter>.mp3`, `002 - ...` in order.

## Catalogue mode (`CATALOGUE_ONLY`)

Set `CATALOGUE_ONLY=true` in the container's environment (or `catalogue.only: true` in `config.yaml`; the environment
wins, and the Settings page shows which is in effect). Restart Stacks after changing it. Then:

- `POST /api/queue/add` with an md5 answers **403** `this server only downloads from public-domain catalogues`;
- the download worker only claims catalogue items and never builds the Anna's Archive downloader;
- the scraper process isn't started, the mirror-domain refresh doesn't run, and the fast-download key test is refused;
- the md5 box is hidden on the Downloads tab;
- debug mode (`FLASK_DEBUG`) refuses to start, because its legacy worker only knows Anna's Archive.

It defaults to **false**, so Stacks behaves exactly as before unless you turn it on.

Two more environment variables let a deployment keep its keys in an `.env` file instead of `config.yaml`. They are
read on every request, never written to `config.yaml` and never shown by `/api/config`:

| Variable | Sets |
| --- | --- |
| `CATALOGUE_ONLY` | `catalogue.only` (`true`/`false`) |
| `ADMIN_API_KEY` | `api.key` (32 letters, digits, `-` or `_`) |
| `DOWNLOADER_API_KEY` | `api.downloader_key` (same format) |

(The admin password still comes from `USERNAME`/`PASSWORD` on first run, or with `RESET_ADMIN=true`.)

## API

| Endpoint | Method | Session | Admin Key | DL Key | Description |
| --- | --- | --- | --- | --- | --- |
| `/api/catalogue/search?q=&source=` | GET | ✔️ | ✔️ | ✔️ | Search the enabled catalogues (or one). One result per book, with every catalogue that has it under `sources` |
| `/api/catalogue/item?source=&id=` | GET | ✔️ | ✔️ | ✔️ | One item and the files a download fetches; `queue_status` if it's already queued |
| `/api/catalogue/queue` | POST | ✔️ | ✔️ | ✔️ | `{"source": "gutenberg", "id": "1342", "subfolder": null}` → `{"success", "key", "title"}` (409 if already queued or downloaded) |
| `/api/catalogue/downloads?keys=k1,k2` | GET | ✔️ | ✔️ | ✔️ | Catalogue items in the queue and history: status, progress, error, and `relpath` (the finished file or folder, relative to the download folder). For Downloader-key clients, which can't read `/api/status` |

```bash
curl -s -H "X-API-Key: $KEY" "http://localhost:7788/api/catalogue/search?q=pride+prejudice"
curl -s -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
     -d '{"source":"librivox","id":"253"}' http://localhost:7788/api/catalogue/queue
```

Busy catalogues answer **503** with `Retry-After`.

## How Stacks treats the catalogues

Read on 2026-11-19 before writing the code:

- **Only these hosts, only HTTPS.** `gutendex.com`, `www.gutenberg.org`, `gutenberg.org`, `standardebooks.org`,
  `librivox.org`, `archive.org`, `www.archive.org` (LibriVox's API still links it; it redirects to `archive.org`), and
  the Internet Archive's numbered storage nodes (`ia<n>.us.archive.org`, `dn<n>.ca.archive.org`), which
  `archive.org/download/...` redirects to. Redirects are followed by hand and every hop is checked against the same
  list. The list is in `src/stacks/catalogue/http.py`; there's no setting that adds to it.
- **An honest User-Agent:** `Stacks-Catalogue/<version> (public-domain catalogue downloader; +<this repository>)`.
- **Pacing per host:** Gutendex and LibriVox 1 request a second, Gutenberg's files one every 2.5 s, Standard Ebooks one
  every 5 s, the Internet Archive one a second. Downloads run one at a time. A 429 or 503 holds that host for its
  `Retry-After` (or 30 s, then 1, 2, 4 min ... up to an hour); searches fail fast meanwhile instead of waiting.
- **Caching:** searches 1 hour, items 1 day, the Standard Ebooks feed 1 hour.
- **Project Gutenberg:** Gutendex documents `/books?search=` and `/books/<id>` and asks long-term users to consider
  running their own copy; Stacks makes one request per search or item and caches it. Only books Gutendex marks
  `copyright: false` (public domain in the USA) are offered. Gutenberg's robot-access policy forbids rapid automated
  downloading and crawling; Stacks fetches one book when someone asks for it, at least 2.5 s apart. (gutenberg.org was
  down for maintenance on 2026-11-19, so its policy page couldn't be re-read that day.)
- **Standard Ebooks:** its feeds page says the New Releases RSS/Atom feeds are open to everyone and the other feeds
  (OPDS, all ebooks, by author/subject) are a Patrons Circle benefit. Stacks only reads the New Releases feed (the
  latest 15), at most once an hour, and remembers every entry it has seen (`config/catalogue_standardebooks.json`), so
  search covers what has been released since Stacks started watching. An older Standard Ebooks title it hasn't seen
  says so instead of being fetched another way. The site's honeypot link is never followed; robots.txt allows the
  feed and downloads for general user agents. Downloads use the feed's own links (`?source=feed`).
- **LibriVox:** its API asks for consideration and a delay between requests, caps `limit` at 500 and may answer 429.
  A search is one request (two when the first finds nothing). Chapters are downloaded one by one from the Internet
  Archive (byte ranges supported, so an interrupted chapter resumes), rather than the whole-book zip the Archive
  builds on the fly. At most 400 chapters.

## Downloads

- Streamed to `<incomplete folder>/catalogue-<key>/`; an interrupted file resumes with a `Range` request when the
  host answers 206 (otherwise it starts that file again). Cancel-and-requeue keeps the partial; cancel-and-remove
  deletes it.
- The first bytes are checked: an EPUB must be a zip that says `application/epub+zip`, an MP3 must look like MP3. A
  web page instead of the file fails the download with a reason.
- `catalogue.max_size_mb` (default 4096) caps one item.
