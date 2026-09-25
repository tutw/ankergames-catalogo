#!/usr/bin/env python3
"""Export the current Anchor Games catalog using the public sitemap and pages.

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

The public sitemap supplies canonical game URLs and lastmod dates. Each game
page is then queried for its display version, Steam build (when exposed), and
file size. Download URIs are not present in the public sitemap/pages, so they
remain empty unless --existing-json supplies a previous value for the same URL.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import time
from datetime import datetime
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
USER_AGENT = "ankergames-sitemap-exporter/2.0"
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 2


class PageParser(HTMLParser):
    """Collect the stable fields exposed in an Anchor Games detail page."""

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


def canonicalize_url(raw_url: str) -> str:
    """Return a stable URL suitable for deduplication."""
    parts = urlsplit(raw_url.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"Invalid sitemap URL: {raw_url!r}")

    # Fragments never identify a different game. Keep the path case-sensitive.
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def is_game_url(url: str) -> bool:
    """Keep game detail URLs and ignore any future non-game sitemap entries."""
    return urlsplit(url).path.startswith("/game/")


def lastmod_sort_key(value: str | None) -> tuple[int, float | str]:
    """Order ISO dates chronologically, with missing/non-ISO values last."""
    if not value:
        return (0, 0.0)

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return (2, value)

    return (1, parsed.timestamp())


def parse_sitemap(xml_bytes: bytes) -> Iterable[tuple[str, str | None]]:
    """Yield (canonical game URL, lastmod) pairs from one sitemap."""
    root = ElementTree.fromstring(xml_bytes)
    for entry in root.findall(SITEMAP_TAGS["url"]):
        location = entry.findtext(SITEMAP_TAGS["loc"])
        if not location:
            continue

        url = canonicalize_url(location)
        if not is_game_url(url):
            continue

        lastmod = entry.findtext(SITEMAP_TAGS["lastmod"])
        yield url, lastmod.strip() if lastmod else None


def fetch(url: str, timeout: float, retries: int) -> bytes:
    """Fetch a public URL with a small retry budget."""
    request = Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,text/html"},
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.5 * (2**attempt))
    raise RuntimeError(f"Could not download {url}: {last_error}")


def download_games(
    sitemap_urls: Iterable[str],
    timeout: float,
    retries: int,
) -> list[dict[str, str | None]]:
    """Download all sitemaps and return a deduplicated, URL-sorted list."""
    games: dict[str, str | None] = {}

    for sitemap_url in sitemap_urls:
        print(f"Downloading {sitemap_url}...", file=sys.stderr)
        try:
            xml_bytes = fetch(sitemap_url, timeout, retries)
            entries = list(parse_sitemap(xml_bytes))
        except (RuntimeError, ElementTree.ParseError, ValueError) as exc:
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
    """Extract the displayed version from the page description or title."""
    description = parser.description or ""
    match = re.search(r"\((?:v|version)\s*([^,)]+)", description, re.IGNORECASE)
    if not match:
        match = re.search(r"\((?:v|version)\s*([^,)]+)", parser.page_title, re.IGNORECASE)
    if not match:
        return None
    return re.split(r"\s+\+\s*Co-?Op\b", match.group(1), maxsplit=1, flags=re.IGNORECASE)[0].strip()


def extract_build(parser: PageParser) -> str | None:
    """Extract a Steam build from the first visible version badge."""
    match = re.search(r"\bB\s+(\d+)\b", parser.visible_text, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"\bBuild\s+(\d+)\b", parser.visible_text, re.IGNORECASE)
    return match.group(1) if match else None


def extract_file_size(parser: PageParser) -> str:
    """Extract the first file size advertised by the page description."""
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


def load_existing_uris(path: str | None) -> dict[str, list[str]]:
    """Load URI arrays from a previous reference-format JSON, if supplied."""
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
    existing_uris: dict[str, list[str]],
) -> dict[str, object]:
    """Fetch one detail page and map it to the reference download record."""
    url = str(game["url"])
    name = slug_to_name(url)
    version: str | None = None
    build: str | None = None
    file_size = ""

    try:
        page_bytes = fetch(url, timeout, retries)
        parser = PageParser()
        parser.feed(page_bytes.decode("utf-8", errors="replace"))
        name = parser.h1 or name
        version = extract_version(parser)
        build = extract_build(parser)
        file_size = extract_file_size(parser)
    except RuntimeError as exc:
        print(f"Warning: using fallback metadata for {url}: {exc}", file=sys.stderr)

    return {
        "title": make_title(name, version, build),
        "uris": existing_uris.get(url, []),
        "uploadDate": game.get("lastmod") or "",
        "fileSize": file_size,
        "descriptionHtml": (
            f'<a href="{escape(url, quote=True)}">'
            "Website with instructions for launching the game</a>"
        ),
    }


def build_catalog(
    games: list[dict[str, str | None]],
    workers: int,
    timeout: float,
    retries: int,
    existing_uris: dict[str, list[str]],
) -> dict[str, object]:
    print(f"Enriching {len(games)} games with {workers} workers...", file=sys.stderr)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        records = list(
            executor.map(
                lambda game: enrich_game(game, timeout, retries, existing_uris),
                games,
            )
        )
    return {"name": "AnkerGames", "downloads": records}


def write_json(catalog: dict[str, object], output_path: str, to_stdout: bool) -> None:
    """Write the stable, reference-compatible JSON representation."""
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
    parser.add_argument(
        "--output",
        default="ankergames.json",
        help="Output JSON path (default: %(default)s).",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write JSON to stdout instead of a file.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Concurrent detail-page requests (default: %(default)s).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Per-request timeout in seconds (default: %(default)s).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=DEFAULT_RETRIES,
        help="Retries per failed request (default: %(default)s).",
    )
    parser.add_argument(
        "--existing-json",
        help="Optional previous reference-format JSON used to preserve known uris.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Only enrich the first N sitemap URLs; useful for a quick test.",
    )
    args = parser.parse_args()

    if args.workers < 1 or args.timeout <= 0 or args.retries < 0:
        parser.error("--workers must be >= 1, --timeout must be > 0, and --retries must be >= 0")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")

    try:
        games = download_games(SITEMAP_URLS, args.timeout, args.retries)
        if args.limit is not None:
            games = games[: args.limit]
        existing_uris = load_existing_uris(args.existing_json)
        catalog = build_catalog(games, args.workers, args.timeout, args.retries, existing_uris)
        write_json(catalog, args.output, args.stdout)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    destination = "stdout" if args.stdout else args.output
    print(f"Wrote {len(catalog['downloads'])} downloads to {destination}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
