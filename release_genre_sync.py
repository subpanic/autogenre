#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import sqlite3
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from mutagen import File as MutagenFile


SUPPORTED_EXTENSIONS = {
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".oga",
    ".opus",
    ".wav",
    ".aiff",
    ".aif",
}


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def normalize_text(text: str) -> str:
    return normalize_space(text).casefold()


def canonical_genre(tag: str) -> str:
    return normalize_text(tag)


def escape_mb_query(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def split_genre_values(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if not value:
            continue
        # Many files store multiple values in one string.
        parts = re.split(r"[;,]", value)
        for part in parts:
            clean = normalize_space(part)
            if clean:
                result.append(clean)
    return result


def ordered_unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        clean = normalize_space(value)
        if not clean:
            continue
        key = canonical_genre(clean)
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def merge_tags(*groups: Iterable[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        merged.extend(group)
    return ordered_unique(merged)


@dataclass(slots=True)
class TrackInfo:
    path: Path
    album: str
    artists: list[str]
    genres: list[str]


@dataclass(slots=True)
class Release:
    folder: Path
    album: str
    tracks: list[TrackInfo]

    @property
    def artists(self) -> list[str]:
        merged: list[str] = []
        for track in self.tracks:
            merged.extend(track.artists)
        return ordered_unique(merged)

    @property
    def release_id(self) -> str:
        base = f"{self.folder}|{normalize_text(self.album)}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()

    def fingerprint(self) -> str:
        hasher = hashlib.sha1()
        for track in sorted(self.tracks, key=lambda t: str(t.path)):
            stat = track.path.stat()
            hasher.update(str(track.path).encode("utf-8"))
            hasher.update(str(stat.st_mtime_ns).encode("utf-8"))
            hasher.update(str(stat.st_size).encode("utf-8"))
        return hasher.hexdigest()


@dataclass(slots=True)
class MusicBrainzCandidate:
    release_id: str
    title: str
    artist: str
    date: str
    country: str
    mb_score: int
    rank_score: int


@dataclass(slots=True)
class BandcampCandidate:
    url: str
    title: str
    artist: str
    score: int


class ReleaseDatabase:
    def __init__(self, db_path: Path) -> None:
        self.path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS releases (
                release_id TEXT PRIMARY KEY,
                folder TEXT NOT NULL,
                album TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                genres_json TEXT,
                notes TEXT
            )
            """
        )
        self.conn.commit()

    def is_completed(self, release_id: str, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT status, fingerprint FROM releases WHERE release_id = ?",
            (release_id,),
        ).fetchone()
        if not row:
            return False
        status, previous_fingerprint = row
        return status == "ok" and previous_fingerprint == fingerprint

    def mark(
        self,
        release: Release,
        fingerprint: str,
        status: str,
        genres: Sequence[str] | None = None,
        notes: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        genres_json = json.dumps(list(genres)) if genres is not None else None
        self.conn.execute(
            """
            INSERT INTO releases (
                release_id,
                folder,
                album,
                fingerprint,
                status,
                processed_at,
                genres_json,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(release_id) DO UPDATE SET
                folder = excluded.folder,
                album = excluded.album,
                fingerprint = excluded.fingerprint,
                status = excluded.status,
                processed_at = excluded.processed_at,
                genres_json = excluded.genres_json,
                notes = excluded.notes
            """,
            (
                release.release_id,
                str(release.folder),
                release.album,
                fingerprint,
                status,
                now,
                genres_json,
                notes,
            ),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


class MusicBrainzClient:
    BASE_URL = "https://musicbrainz.org/ws/2"

    def __init__(self, contact: str | None = None) -> None:
        contact = (contact or "").strip()
        user_agent = "release-genre-sync/0.1"
        if contact:
            user_agent += f" ({contact})"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": user_agent,
            }
        )
        self._last_request_monotonic = 0.0

    def _rate_limit(self) -> None:
        now = time.monotonic()
        delta = now - self._last_request_monotonic
        if delta < 1.0:
            time.sleep(1.0 - delta)

    def _request_json(self, path: str, params: dict[str, str]) -> dict | None:
        self._rate_limit()
        try:
            response = self.session.get(
                f"{self.BASE_URL}{path}",
                params=params,
                timeout=20,
            )
            self._last_request_monotonic = time.monotonic()
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            logging.warning("MusicBrainz request failed (%s): %s", path, exc)
            return None

    def search_releases(
        self, album: str, artists: Sequence[str], limit: int = 10
    ) -> list[MusicBrainzCandidate]:
        normalized_album = normalize_text(album)
        artist_candidates = ordered_unique(artists)
        query_candidates: list[str] = []
        safe_album = escape_mb_query(album)
        if artist_candidates:
            safe_artist = escape_mb_query(artist_candidates[0])
            query_candidates.append(
                f'release:"{safe_album}" AND artist:"{safe_artist}"'
            )
        query_candidates.append(f'release:"{safe_album}"')

        seen_release_ids: set[str] = set()
        candidates: list[MusicBrainzCandidate] = []
        normalized_artists = {normalize_text(a) for a in artist_candidates}

        for query in query_candidates:
            payload = self._request_json(
                "/release/",
                {"query": query, "fmt": "json", "limit": str(limit)},
            )
            if not payload:
                continue
            for release in payload.get("releases", []):
                release_id = str(release.get("id", "")).strip()
                if not release_id or release_id in seen_release_ids:
                    continue
                seen_release_ids.add(release_id)

                score = int(release.get("score", 0))
                candidate_album = normalize_text(str(release.get("title", "")))
                adjusted_score = score
                if candidate_album == normalized_album:
                    adjusted_score += 30
                elif normalized_album in candidate_album:
                    adjusted_score += 10

                artist_names = self._artist_credit_names(release.get("artist-credit", []))
                candidate_artists = {normalize_text(name) for name in artist_names}
                if normalized_artists and candidate_artists.intersection(
                    normalized_artists
                ):
                    adjusted_score += 20

                candidates.append(
                    MusicBrainzCandidate(
                        release_id=release_id,
                        title=str(release.get("title", "")).strip(),
                        artist=", ".join(artist_names),
                        date=str(release.get("date", "")).strip(),
                        country=str(release.get("country", "")).strip(),
                        mb_score=score,
                        rank_score=adjusted_score,
                    )
                )

        candidates.sort(
            key=lambda c: (c.rank_score, c.mb_score, c.title, c.artist),
            reverse=True,
        )
        return candidates[:limit]

    @staticmethod
    def _artist_credit_names(artist_credit: Sequence[object]) -> list[str]:
        names: list[str] = []
        for item in artist_credit:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            if not name and isinstance(item.get("artist"), dict):
                name = str(item["artist"].get("name", "")).strip()
            if name:
                names.append(name)
        return ordered_unique(names)

    def fetch_genres_for_release(self, release_id: str) -> list[str]:
        if not release_id:
            return []

        details = self._request_json(
            f"/release/{release_id}",
            {"fmt": "json", "inc": "genres+tags"},
        )
        if not details:
            return []

        genres: list[str] = []
        for key in ("genres", "tags"):
            for item in details.get(key, []):
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if not name:
                    continue
                genres.append(name)

        return ordered_unique(genres)

    def fetch_genres(self, album: str, artists: Sequence[str]) -> list[str]:
        candidates = self.search_releases(album, artists, limit=1)
        if not candidates:
            return []
        return self.fetch_genres_for_release(candidates[0].release_id)


class BandcampClient:
    SEARCH_URL = "https://bandcamp.com/search"

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "release-genre-sync/0.1 (+https://github.com)"
                )
            }
        )
        self._last_request_monotonic: float | None = None
        self._random = random.Random()

    def _wait_between_requests(self) -> None:
        if self._last_request_monotonic is None:
            return
        # Requirement: random 1-5 seconds between Bandcamp requests.
        time.sleep(self._random.uniform(1.0, 5.0))

    def _request_text(self, url: str, params: dict[str, str] | None = None) -> str | None:
        self._wait_between_requests()
        try:
            response = self.session.get(url, params=params, timeout=20)
            self._last_request_monotonic = time.monotonic()
            response.raise_for_status()
            return response.text
        except requests.RequestException as exc:
            logging.warning("Bandcamp request failed (%s): %s", url, exc)
            return None

    def search_album_candidates(
        self, album: str, artists: Sequence[str], limit: int = 10
    ) -> list[BandcampCandidate]:
        artist = artists[0] if artists else ""
        query = " ".join(part for part in [artist, album] if part).strip()
        if not query:
            return []

        html = self._request_text(
            self.SEARCH_URL, params={"q": query, "item_type": "a"}
        )
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        candidates: list[BandcampCandidate] = []
        album_norm = normalize_text(album)
        artist_norm = normalize_text(artist)

        for result in soup.select("li.searchresult"):
            link = result.select_one("a[href*='/album/']")
            if not link or not link.get("href"):
                continue
            url = self._normalize_url(str(link["href"]))
            title_node = result.select_one(".heading")
            title_text = (
                title_node.get_text(" ", strip=True)
                if title_node
                else link.get_text(" ", strip=True)
            )
            artist_node = result.select_one(".subhead")
            artist_text = (
                artist_node.get_text(" ", strip=True) if artist_node else ""
            )
            if artist_text.lower().startswith("by "):
                artist_text = artist_text[3:].strip()
            score = self._candidate_score(
                title_text, artist_text, album_norm, artist_norm, url
            )
            candidates.append(
                BandcampCandidate(
                    url=url,
                    title=normalize_space(title_text),
                    artist=normalize_space(artist_text),
                    score=score,
                )
            )

        if not candidates:
            for link in soup.select("a[href*='/album/']"):
                href = link.get("href")
                if not href:
                    continue
                url = self._normalize_url(str(href))
                title_text = link.get_text(" ", strip=True)
                score = self._candidate_score(
                    title_text, "", album_norm, artist_norm, url
                )
                candidates.append(
                    BandcampCandidate(
                        url=url,
                        title=normalize_space(title_text),
                        artist="",
                        score=score,
                    )
                )

        deduped: dict[str, BandcampCandidate] = {}
        for candidate in candidates:
            existing = deduped.get(candidate.url)
            if existing is None or candidate.score > existing.score:
                deduped[candidate.url] = candidate
        if not deduped:
            return []

        ranked = sorted(
            deduped.values(),
            key=lambda item: (item.score, item.title, item.artist, item.url),
            reverse=True,
        )
        return ranked[:limit]

    @staticmethod
    def _normalize_url(url: str) -> str:
        clean = url.split("?")[0].strip()
        parsed = urlparse(clean)
        if not parsed.scheme:
            return urljoin("https://bandcamp.com", clean)
        return clean

    def find_album_url(self, album: str, artists: Sequence[str]) -> str | None:
        candidates = self.search_album_candidates(album, artists, limit=1)
        if not candidates:
            return None
        return candidates[0].url

    @staticmethod
    def _candidate_score(
        candidate_album: str,
        candidate_artist: str,
        album_norm: str,
        artist_norm: str,
        url: str,
    ) -> int:
        score = 0
        cand_album_norm = normalize_text(candidate_album)
        cand_artist_norm = normalize_text(candidate_artist)
        if cand_album_norm == album_norm:
            score += 8
        elif album_norm and album_norm in cand_album_norm:
            score += 4
        if artist_norm and artist_norm == cand_artist_norm:
            score += 6
        elif artist_norm and artist_norm in cand_artist_norm:
            score += 3
        if "/album/" in url:
            score += 2
        return score

    def fetch_tags_from_url(self, album_url: str) -> list[str]:
        if not album_url:
            return []

        html = self._request_text(album_url)
        if not html:
            return []
        return self._extract_tags_from_release_page(html)

    def fetch_tags(self, album: str, artists: Sequence[str]) -> list[str]:
        album_url = self.find_album_url(album, artists)
        if not album_url:
            return []
        return self.fetch_tags_from_url(album_url)

    @staticmethod
    def _extract_tags_from_release_page(html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        tags: list[str] = []

        for node in soup.select("a.tag"):
            text = node.get_text(" ", strip=True)
            if text:
                tags.append(text)
        if tags:
            return ordered_unique(tags)

        for node in soup.select("a[href*='/tag/']"):
            text = node.get_text(" ", strip=True)
            if text:
                tags.append(text)
        if tags:
            return ordered_unique(tags)

        headings = soup.find_all(re.compile(r"^h[1-6]$"))
        for heading in headings:
            title = normalize_text(heading.get_text(" ", strip=True))
            if title != "tags":
                continue
            sibling = heading.find_next_sibling()
            while sibling and sibling.name not in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                for anchor in sibling.find_all("a", href=True):
                    text = anchor.get_text(" ", strip=True)
                    if text:
                        tags.append(text)
                sibling = sibling.find_next_sibling()
            if tags:
                return ordered_unique(tags)

        meta_keywords = soup.find("meta", attrs={"name": "keywords"})
        if meta_keywords and meta_keywords.get("content"):
            return ordered_unique(str(meta_keywords["content"]).split(","))

        return []


def read_track_info(path: Path) -> TrackInfo | None:
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as exc:  # mutagen can raise format-specific exceptions
        logging.warning("Failed reading tags from %s: %s", path, exc)
        return None
    if audio is None:
        return None

    tags = audio.tags or {}
    album_values = tags.get("album", [])
    album = normalize_space(album_values[0]) if album_values else ""
    if not album:
        return None

    artists = tags.get("albumartist", []) or tags.get("artist", [])
    genres = split_genre_values(tags.get("genre", []))
    return TrackInfo(
        path=path,
        album=album,
        artists=ordered_unique(artists),
        genres=ordered_unique(genres),
    )


def discover_releases(root: Path) -> list[Release]:
    grouped: dict[tuple[Path, str], list[TrackInfo]] = defaultdict(list)
    album_names: dict[tuple[Path, str], str] = {}

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        track = read_track_info(path)
        if track is None:
            continue
        key = (path.parent, normalize_text(track.album))
        grouped[key].append(track)
        album_names[key] = track.album

    releases: list[Release] = []
    for key, tracks in grouped.items():
        folder, _ = key
        release = Release(
            folder=folder,
            album=album_names[key],
            tracks=sorted(tracks, key=lambda t: str(t.path)),
        )
        releases.append(release)

    releases.sort(key=lambda r: (str(r.folder), normalize_text(r.album)))
    return releases


def write_genres(path: Path, genres: Sequence[str]) -> None:
    audio = MutagenFile(path, easy=True)
    if audio is None:
        raise RuntimeError(f"Unsupported audio format: {path}")
    if audio.tags is None:
        audio.add_tags()
    audio["genre"] = list(genres)
    audio.save()


def choose_musicbrainz_candidate(
    release: Release,
    candidates: Sequence[MusicBrainzCandidate],
    interactive: bool,
) -> MusicBrainzCandidate | None:
    if not candidates:
        return None
    if not interactive or len(candidates) == 1:
        return candidates[0]

    print()
    print(
        f"MusicBrainz candidates for '{release.album}' "
        f"({', '.join(release.artists) or 'unknown artist'})"
    )
    for idx, candidate in enumerate(candidates, start=1):
        details = ", ".join(
            part
            for part in [
                candidate.artist or "unknown artist",
                candidate.date or "unknown date",
                candidate.country or "unknown country",
            ]
            if part
        )
        print(
            f"  {idx}. {candidate.title or '(untitled)'} | {details} | "
            f"score={candidate.rank_score} | id={candidate.release_id}"
        )
    print("  0. Skip MusicBrainz for this release")

    while True:
        try:
            raw = input(f"Select MusicBrainz match [Enter=1, 0-{len(candidates)}]: ").strip()
        except KeyboardInterrupt:
            print()
            raise
        except EOFError:
            print()
            logging.warning("Input ended; defaulting to top MusicBrainz match")
            return candidates[0]
        if not raw:
            return candidates[0]
        if raw.isdigit():
            selection = int(raw)
            if selection == 0:
                return None
            if 1 <= selection <= len(candidates):
                return candidates[selection - 1]
        print("Invalid selection.")


def choose_bandcamp_candidate(
    release: Release,
    candidates: Sequence[BandcampCandidate],
    interactive: bool,
) -> BandcampCandidate | None:
    if not candidates:
        return None
    if not interactive or len(candidates) == 1:
        return candidates[0]

    print()
    print(
        f"Bandcamp candidates for '{release.album}' "
        f"({', '.join(release.artists) or 'unknown artist'})"
    )
    for idx, candidate in enumerate(candidates, start=1):
        print(
            f"  {idx}. {candidate.title or '(untitled)'} | "
            f"{candidate.artist or 'unknown artist'} | score={candidate.score}"
        )
        print(f"     {candidate.url}")
    print("  0. Skip Bandcamp for this release")

    while True:
        try:
            raw = input(f"Select Bandcamp match [Enter=1, 0-{len(candidates)}]: ").strip()
        except KeyboardInterrupt:
            print()
            raise
        except EOFError:
            print()
            logging.warning("Input ended; defaulting to top Bandcamp match")
            return candidates[0]
        if not raw:
            return candidates[0]
        if raw.isdigit():
            selection = int(raw)
            if selection == 0:
                return None
            if 1 <= selection <= len(candidates):
                return candidates[selection - 1]
        print("Invalid selection.")


def process_release(
    release: Release,
    db: ReleaseDatabase,
    musicbrainz: MusicBrainzClient | None,
    bandcamp: BandcampClient | None,
    dry_run: bool,
    interactive: bool,
) -> tuple[int, int]:
    fingerprint = release.fingerprint()
    if db.is_completed(release.release_id, fingerprint):
        logging.info("Skipping already-complete release: %s / %s", release.folder, release.album)
        return (0, 1)

    existing = merge_tags(*(track.genres for track in release.tracks))
    target = list(existing)

    mb_tags: list[str] = []
    bc_tags: list[str] = []
    mb_candidates: list[MusicBrainzCandidate] = []
    bc_candidates: list[BandcampCandidate] = []
    selected_mb: MusicBrainzCandidate | None = None
    selected_bc: BandcampCandidate | None = None

    if musicbrainz:
        mb_candidates = musicbrainz.search_releases(release.album, release.artists)
        selected_mb = choose_musicbrainz_candidate(
            release=release,
            candidates=mb_candidates,
            interactive=interactive,
        )
        if selected_mb:
            mb_tags = musicbrainz.fetch_genres_for_release(selected_mb.release_id)
            target = merge_tags(target, mb_tags)
    if bandcamp:
        bc_candidates = bandcamp.search_album_candidates(release.album, release.artists)
        selected_bc = choose_bandcamp_candidate(
            release=release,
            candidates=bc_candidates,
            interactive=interactive,
        )
        if selected_bc:
            bc_tags = bandcamp.fetch_tags_from_url(selected_bc.url)
            target = merge_tags(target, bc_tags)

    changed_files = 0
    for track in release.tracks:
        current_keys = [canonical_genre(g) for g in track.genres]
        target_keys = [canonical_genre(g) for g in target]
        if current_keys == target_keys:
            continue
        changed_files += 1
        if not dry_run:
            write_genres(track.path, target)

    existing_keys = {canonical_genre(x) for x in existing}
    after_mb = merge_tags(existing, mb_tags)
    after_mb_keys = {canonical_genre(x) for x in after_mb}

    notes = json.dumps(
        {
            "existing": existing,
            "musicbrainz_candidates": len(mb_candidates),
            "musicbrainz_selected": selected_mb.release_id if selected_mb else None,
            "musicbrainz_added": [t for t in mb_tags if canonical_genre(t) not in existing_keys],
            "bandcamp_candidates": len(bc_candidates),
            "bandcamp_selected": selected_bc.url if selected_bc else None,
            "bandcamp_added": [t for t in bc_tags if canonical_genre(t) not in after_mb_keys],
            "changed_files": changed_files,
            "dry_run": dry_run,
            "interactive": interactive,
        }
    )
    status = "dry_run" if dry_run else "ok"
    db.mark(release, fingerprint, status, genres=target, notes=notes)

    logging.info(
        "Processed: %s / %s | tracks=%d changed=%d genres=%s",
        release.folder,
        release.album,
        len(release.tracks),
        changed_files,
        ", ".join(target) if target else "(none)",
    )
    return (changed_files, 0)


def safe_release_fingerprint(release: Release) -> str:
    try:
        return release.fingerprint()
    except Exception:
        return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively discover album releases, harmonize genre tags across files, "
            "and enrich genres from MusicBrainz and Bandcamp."
        )
    )
    parser.add_argument("root", type=Path, help="Root folder to scan recursively")
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="SQLite state DB path (default: <root>/.release_genre_sync.sqlite3)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not write tags, only report what would change",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "Prompt for source candidate selection when multiple matches exist "
            "for MusicBrainz or Bandcamp"
        ),
    )
    parser.add_argument(
        "--no-musicbrainz",
        action="store_true",
        help="Disable MusicBrainz genre lookup",
    )
    parser.add_argument(
        "--no-bandcamp",
        action="store_true",
        help="Disable Bandcamp tag lookup",
    )
    parser.add_argument(
        "--musicbrainz-contact",
        default="",
        help="Optional contact info appended to MusicBrainz User-Agent",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log verbosity",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    root = args.root.expanduser().resolve()
    if not root.exists() or not root.is_dir():
        logging.error("Root folder does not exist or is not a directory: %s", root)
        return 1

    db_path = args.db_path.expanduser().resolve() if args.db_path else root / ".release_genre_sync.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = ReleaseDatabase(db_path)

    musicbrainz = None if args.no_musicbrainz else MusicBrainzClient(contact=args.musicbrainz_contact)
    bandcamp = None if args.no_bandcamp else BandcampClient()
    interactive = bool(args.interactive)
    if interactive and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        logging.warning(
            "--interactive requested without a TTY; continuing in automatic mode"
        )
        interactive = False

    try:
        releases = discover_releases(root)
        if not releases:
            logging.info("No releases found under %s", root)
            return 0

        logging.info("Discovered %d releases", len(releases))
        total_changed = 0
        total_skipped = 0

        for release in releases:
            try:
                changed, skipped = process_release(
                    release=release,
                    db=db,
                    musicbrainz=musicbrainz,
                    bandcamp=bandcamp,
                    dry_run=args.dry_run,
                    interactive=interactive,
                )
                total_changed += changed
                total_skipped += skipped
            except KeyboardInterrupt:
                db.mark(
                    release=release,
                    fingerprint=safe_release_fingerprint(release),
                    status="interrupted",
                    notes="KeyboardInterrupt",
                )
                logging.warning(
                    "Interrupted during release: %s / %s",
                    release.folder,
                    release.album,
                )
                raise
            except Exception as exc:
                db.mark(
                    release=release,
                    fingerprint=safe_release_fingerprint(release),
                    status="error",
                    notes=str(exc),
                )
                logging.exception(
                    "Failed release %s / %s: %s",
                    release.folder,
                    release.album,
                    exc,
                )

        logging.info(
            "Done. releases=%d changed_files=%d skipped_releases=%d state_db=%s",
            len(releases),
            total_changed,
            total_skipped,
            db_path,
        )
        return 0
    except KeyboardInterrupt:
        logging.warning("Interrupted by user (Ctrl-C). Progress saved in %s", db_path)
        return 130
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
