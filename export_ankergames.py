#!/usr/bin/env python3
"""Export the current Anchor Games catalog using the public sitemap and pages.

The exporter is deliberately conservative: it uses a global rate limiter,
honors Retry-After, retries transient failures with exponential backoff, and
stores successful detail-page metadata in a JSONL checkpoint cache.

Output follows the reference schema:

    {
        "name": "AnkerGames",
        "downloads": [
            {
                "title": "...",
                "uris": [],
                "uploadDate": "...",
                "fileSize": "...",
                "descriptionHtml": "..."
            }
        ]
    }
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import random
import re
import sys
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


SITEMAP_URLS = (
    "https://ankergames.net/sitemap_post_1.xml",
    "https://ankergames.net/sitemap_post_2.xml",
    "https://ankergames.net/sitemap_post_3.xml",
)
SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
SITEMAP_TAGS = {
    "url": f"{{{SITEMAP_NAMESPACE}}}url",
    "loc": f"{{{SITEMAP_NAMESPACE}}}loc",
    "lastmod": f"{{{SITEMAP_NAMESPACE}}}lastmod",
}
USER_AGENT = "ankergames-sitemap-exporter/3.0"
DEFAULT_WORKERS = 2
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 4
DEFAULT_REQUEST_DELAY = 0.5
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    """A request failed after the configured retry budget."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RateLimiter:
    """Coordinate request start times and global pauses across workers."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._next_allowed = 0.0
        self._blocked_until = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                start = max(self._next_allowed, self._blocked_until)
                if start <= now:
                    self._next_allowed = now + self.min_interval
                    return
                delay = start - now
            time.sleep(min(delay, 1.0))

    def pause(self, seconds: float) -> None:
        """Pause all workers after a server-wide rate-limit response."""
        with self._lock:
            self._blocked_until = max(
                self._blocked_until,
                time.monotonic() + max(0.0, seconds),
            )


class MetadataCache:
    """Append-only checkpoint cache with an atomic compacted final form."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, str]] = {}
        self._writes = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return

        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    print(
                        f"Warning: ignoring malformed cache line {line_number}.",
                        file=sys.stderr,
                    )
                    continue
                url = record.get("url")
                title = record.get("title")
                if isinstance(url, str) and isinstance(title, str):
                    self._records[url] = {
                        "url": url,
                        "lastmod": str(record.get("lastmod") or ""),
                        "title": title,
                        "fileSize": str(record.get("fileSize") or ""),
                    }

    def get(self, url: str, lastmod: str | None) -> dict[str, str] | None:
        with self._lock:
            record = self._records.get(url)
            if record is None:
                return None
            if lastmod and record["lastmod"] != lastmod:
                return None
            return dict(record)

    def put(self, record: dict[str, str]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                self._writes += 1
                if self._writes % 10 == 0:
                    os.fsync(handle.fileno())
            self._records[record["url"]] = dict(record)

    def compact(self, records: Iterable[dict[str, str]]) -> None:
        current = {record["url"]: dict(record) for record in records}
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as handle:
            for url in sorted(current, key=str.casefold):
                handle.write(
                    json.dumps(current[url], ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        with self._lock:
            self._records = current
            self._writes = 0


class PageParser(HTMLParser):
    """Collect stable fields exposed in an Anchor Games detail page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.description: str | None = None
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.visible_parts: list[str] = []
        self._in_title = False
        self._in_h1 = False
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and (attributes.get("name") or "").lower() == "description":
            self.description = attributes.get("content")
        if tag == "title":
            self._in_title = True
        elif tag == "h1":
            self._in_h1 = True
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_h1:
            self.h1_parts.append(data)
        if not self._ignored_depth:
            self.visible_parts.append(data)

    @property
    def h1(self) -> str:
        return normalize_text(" ".join(self.h1_parts))

    @property
    def page_title(self) -> str:
        return normalize_text(" ".join(self.title_parts))

    @property
    def visible_text(self) -> str:
        return normalize_text(" ".join(self.visible_parts))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, parsed.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(attempt: int, retry_after: float | None = None) -> float:
    exponential = min(60.0, 2.0**attempt)
    if retry_after is not None:
        exponential = max(exponential, retry_after)
    return exponential + random.uniform(0.0, min(2.0, exponential * 0.1))


def canonicalize_url(raw_url: str) -> str:
    """Return a stable URL suitable for deduplication."""
    parts = urlsplit(raw_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"Invalid sitemap URL: {raw_url!r}")

    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def is_game_url(url: str) -> bool:
    return urlsplit(url).path.startswith("/game/")


def lastmod_sort_key(value: str | None) -> tuple[int, float | str]:
    if not value:
        return (0, 0.0)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return (2, value)
    return (1, parsed.timestamp())


def parse_sitemap(xml_bytes: bytes) -> Iterable[tuple[str, str | None]]:
    root = ElementTree.fromstring(xml_bytes)
    for entry in root.findall(SITEMAP_TAGS["url"]):
        location = entry.findtext(SITEMAP_TAGS["loc"])
        if not location:
            continue
        url = canonicalize_url(location)
        if is_game_url(url):
            lastmod = entry.findtext(SITEMAP_TAGS["lastmod"])
            yield url, lastmod.strip() if lastmod else None


def fetch(url: str, timeout: float, retries: int, limiter: RateLimiter) -> bytes:
    """Fetch a public URL with global pacing and transient-error retries."""
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,text/html"},
    )
    for attempt in range(retries + 1):
        limiter.wait()
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            status = exc.code
            retry_after = parse_retry_after(exc.headers.get("Retry-After"))
            if status in RETRYABLE_STATUS_CODES and attempt < retries:
                delay = retry_delay(attempt, retry_after)
                print(
                    f"HTTP {status} for {url}; retrying in {delay:.1f}s.",
                    file=sys.stderr,
                )
                limiter.pause(delay)
                continue
            raise FetchError(f"HTTP {status} while downloading {url}", status) from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                delay = retry_delay(attempt)
                print(
                    f"Transient error for {url}; retrying in {delay:.1f}s.",
                    file=sys.stderr,
                )
                limiter.pause(delay)
                continue
            raise FetchError(f"Could not download {url}: {exc}") from exc
    raise FetchError(f"Could not download {url}: retry budget exhausted")


def download_games(
    sitemap_urls: Iterable[str],
    timeout: float,
    retries: int,
    limiter: RateLimiter,
) -> list[dict[str, str | None]]:
    games: dict[str, str | None] = {}
    for sitemap_url in sitemap_urls:
        print(f"Downloading {sitemap_url}...", file=sys.stderr)
        try:
            entries = list(parse_sitemap(fetch(sitemap_url, timeout, retries, limiter)))
        except (FetchError, ElementTree.ParseError, ValueError) as exc:
            raise RuntimeError(str(exc)) from exc
        for url, lastmod in entries:
            if url not in games or lastmod_sort_key(lastmod) > lastmod_sort_key(games[url]):
                games[url] = lastmod
    return [
        {"url": url, "lastmod": games[url]}
        for url in sorted(games, key=str.casefold)
    ]


def slug_to_name(url: str) -> str:
    slug = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return normalize_text(slug.replace("-", " ").title())


def extract_version(parser: PageParser) -> str | None:
    description = parser.description or ""
    match = re.search(r"\((?:v|version)\s*([^,)]+)", description, re.IGNORECASE)
    if not match:
        match = re.search(r"\((?:v|version)\s*([^,)]+)", parser.page_title, re.IGNORECASE)
    if not match:
        return None
    return re.split(r"\s+\+\s*Co-?Op\b", match.group(1), maxsplit=1, flags=re.IGNORECASE)[0].strip()


def extract_build(parser: PageParser) -> str | None:
    match = re.search(r"\bB\s+(\d+)\b", parser.visible_text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"\bBuild\s+(\d+)\b", parser.visible_text, re.IGNORECASE)
    return match.group(1) if match else None


def extract_file_size(parser: PageParser) -> str:
    description = parser.description or parser.visible_text
    match = re.search(r"\b\d+(?:[.,]\d+)?\s*(?:TB|GB|MB)\b", description, re.IGNORECASE)
    return normalize_text(match.group(0).replace(",", ".")) if match else ""


def make_title(name: str, version: str | None, build: str | None) -> str:
    if version and build:
        return f"{name} - V {version} / Build {build}"
    if version:
        return f"{name} - V {version}"
    if build:
        return f"{name} - Build {build}"
    return name


def make_download(
    url: str,
    title: str,
    lastmod: str | None,
    file_size: str,
    existing_uris: dict[str, list[str]],
) -> dict[str, object]:
    return {
        "title": title,
        "uris": existing_uris.get(url, []),
        "uploadDate": lastmod or "",
        "fileSize": file_size,
        "descriptionHtml": (
            f'<a href="{escape(url, quote=True)}">'
            "Website with instructions for launching the game</a>"
        ),
    }


def load_existing_uris(path: str | None) -> dict[str, list[str]]:
    if not path:
        return {}
    source = Path(path)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read existing JSON {source}: {exc}") from exc
    existing: dict[str, list[str]] = {}
    for item in data.get("downloads", []):
        description = item.get("descriptionHtml", "")
        match = re.search(r'href=["\']([^"\']+)', description, re.IGNORECASE)
        if not match:
            continue
        try:
            url = canonicalize_url(match.group(1))
        except ValueError:
            continue
        uris = item.get("uris", [])
        if isinstance(uris, list) and all(isinstance(uri, str) for uri in uris):
            existing[url] = uris
    return existing


def enrich_game(
    game: dict[str, str | None],
    timeout: float,
    retries: int,
    limiter: RateLimiter,
    cache: MetadataCache,
    existing_uris: dict[str, list[str]],
    allow_partial: bool,
) -> tuple[dict[str, object], dict[str, str] | None]:
    url = str(game["url"])
    lastmod = game.get("lastmod") or ""
    cached = cache.get(url, lastmod)
    if cached:
        download = make_download(
            url,
            cached["title"],
            cached["lastmod"] or lastmod,
            cached["fileSize"],
            existing_uris,
        )
        return download, cached

    name = slug_to_name(url)
    try:
        page_bytes = fetch(url, timeout, retries, limiter)
        parser = PageParser()
        parser.feed(page_bytes.decode("utf-8", errors="replace"))
        name = parser.h1 or name
        title = make_title(name, extract_version(parser), extract_build(parser))
        file_size = extract_file_size(parser)
    except FetchError as exc:
        if not allow_partial:
            raise
        print(f"Warning: using fallback metadata for {url}: {exc}", file=sys.stderr)
        title = name
        file_size = ""
        return make_download(url, title, lastmod, file_size, existing_uris), None

    cache_record = {
        "url": url,
        "lastmod": lastmod,
        "title": title,
        "fileSize": file_size,
    }
    cache.put(cache_record)
    return make_download(url, title, lastmod, file_size, existing_uris), cache_record


def build_catalog(
    games: list[dict[str, str | None]],
    workers: int,
    timeout: float,
    retries: int,
    limiter: RateLimiter,
    cache: MetadataCache,
    existing_uris: dict[str, list[str]],
    allow_partial: bool,
) -> dict[str, object]:
    print(f"Enriching {len(games)} games with {workers} worker(s)...", file=sys.stderr)
    cache_records: list[dict[str, str]] = []

    def worker(game: dict[str, str | None]) -> tuple[dict[str, object], dict[str, str] | None]:
        return enrich_game(
            game,
            timeout,
            retries,
            limiter,
            cache,
            existing_uris,
            allow_partial,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(worker, games))

    downloads: list[dict[str, object]] = []
    for download, cache_record in results:
        downloads.append(download)
        if cache_record is not None:
            cache_records.append(cache_record)
    cache.compact(cache_records)
    return {"name": "AnkerGames", "downloads": downloads}


def write_json(catalog: dict[str, object], output_path: str, to_stdout: bool) -> None:
    if to_stdout:
        json.dump(catalog, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Anchor Games as the reference name/downloads JSON schema."
    )
    parser.add_argument("--output", default="ankergames.json")
    parser.add_argument("--stdout", action="store_true")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY)
    parser.add_argument(
        "--cache",
        default=".cache/ankergames-metadata.jsonl",
        help="JSONL checkpoint/cache path (default: %(default)s).",
    )
    parser.add_argument("--existing-json")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Emit fallback metadata after exhausted retries instead of failing.",
    )
    args = parser.parse_args()

    if args.workers < 1 or args.timeout <= 0 or args.retries < 0 or args.request_delay < 0:
        parser.error("workers must be >= 1; timeout > 0; retries/delay >= 0")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")

    limiter = RateLimiter(args.request_delay)
    cache = MetadataCache(args.cache)
    try:
        games = download_games(SITEMAP_URLS, args.timeout, args.retries, limiter)
        if args.limit is not None:
            games = games[: args.limit]
        existing_uris = load_existing_uris(args.existing_json)
        catalog = build_catalog(
            games,
            args.workers,
            args.timeout,
            args.retries,
            limiter,
            cache,
            existing_uris,
            args.allow_partial,
        )
        write_json(catalog, args.output, args.stdout)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    destination = "stdout" if args.stdout else args.output
    print(f"Wrote {len(catalog['downloads'])} downloads to {destination}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
