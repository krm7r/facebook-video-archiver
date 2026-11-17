#!/usr/bin/env python3
"""Fetch Facebook video links through Floader and save the HD MP4 files."""
from __future__ import annotations

import html
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://floader.net/php/api/api.php"
VIDEO_DIR = Path("videos")
MAX_FILE_BYTES = 95 * 1024 * 1024  # Stay below GitHub's 100 MiB individual-file limit.
MAX_URLS = 30


class DownloadLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.href = None
        self.label_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.in_span = False

    def handle_starttag(self, tag: str, attrs) -> None:
        attrs = dict(attrs)
        if tag.lower() == "a":
            self.href = attrs.get("href")
            self.label_parts = []
        elif tag.lower() == "span" and self.href:
            self.in_span = True

    def handle_data(self, data: str) -> None:
        if self.href and self.in_span:
            self.label_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "span":
            self.in_span = False
        elif tag.lower() == "a" and self.href:
            self.links.append((self.href, " ".join(self.label_parts).strip()))
            self.href = None
            self.label_parts = []
            self.in_span = False


def normalize_href(value: str) -> str:
    value = html.unescape(value)
    value = value.replace(r"\u00253D", "%3D")
    value = value.replace(r"\u0026", "&").replace(r"\u003d", "=")
    return value


def valid_facebook_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and (host == "facebook.com" or host.endswith(".facebook.com") or host == "fb.watch")


def video_id(value: str, index: int) -> str:
    segments = [s for s in urlparse(value).path.split("/") if s]
    candidate = segments[-1].strip() if segments else f"video-{index}"
    candidate = re.sub(r"[^A-Za-z0-9_-]", "", candidate)
    return candidate[:80] or f"video-{index}"


def get_hd_link(session: requests.Session, facebook_url: str) -> str:
    response = session.get(API, params={"url": facebook_url}, timeout=75)
    response.raise_for_status()
    parser = DownloadLinks()
    parser.feed(response.text)
    for href, label in parser.links:
        if "HD" in label.upper():
            direct = normalize_href(href)
            host = (urlparse(direct).hostname or "").lower()
            if urlparse(direct).scheme != "https" or not host.endswith("fbcdn.net"):
                raise RuntimeError("Floader returned an unexpected download host")
            return direct
    raise RuntimeError("Floader did not return an HD link")


def save_mp4(session: requests.Session, direct_url: str, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    total = 0
    try:
        with session.get(direct_url, stream=True, timeout=(25, 120)) as response:
            response.raise_for_status()
            content_type = (response.headers.get("content-type") or "").lower()
            if content_type and not ("video" in content_type or "octet-stream" in content_type):
                raise RuntimeError(f"Unexpected response content type: {content_type}")
            with partial.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_FILE_BYTES:
                        raise RuntimeError("Video exceeds the 95 MiB safe per-file limit")
                    output.write(chunk)
        with partial.open("rb") as downloaded:
            downloaded.seek(4)
            is_mp4 = downloaded.read(4) == b"ftyp"
        if total < 1024 or not is_mp4:
            raise RuntimeError("Downloaded response does not look like an MP4")
        partial.replace(destination)
        return total
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def main() -> int:
    raw = os.environ.get("VIDEO_URLS", "")
    urls = [line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    if not urls:
        print("No Facebook URLs supplied; nothing to do.")
        return 0
    if len(urls) > MAX_URLS:
        print(f"Too many links: maximum is {MAX_URLS} per run.", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; video-archive-action/1.0)"})
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504, 522),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    failures: list[str] = []
    for index, url in enumerate(urls, start=1):
        if not valid_facebook_url(url):
            failures.append(f"line {index}: only HTTPS Facebook or fb.watch URLs are accepted")
            continue
        destination = VIDEO_DIR / f"{video_id(url, index)}.mp4"
        try:
            direct = get_hd_link(session, url)
            size = save_mp4(session, direct, destination)
            print(f"Saved {destination} ({size:,} bytes)")
        except Exception as exc:
            failures.append(f"line {index} ({url}): {exc}")
            print(f"Failed: {url}: {exc}", file=sys.stderr)

    if failures:
        print("Some downloads failed; no commit will be made by the workflow.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
