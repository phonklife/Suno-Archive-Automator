import argparse
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from urllib.request import urlopen


DEFAULT_ARTIST = "virtualluser"
DEFAULT_ROOT_KEY = "tracks"
MAX_SOURCE_BYTES = 5 * 1024 * 1024


@dataclass
class TrackRecord:
    source_id: str
    title: str
    artist: str
    source_url: str | None
    audio_url: str | None
    status: str
    archived_at: str | None
    tags: list[str] | None
    tags_provided: bool
    raw_payload: str


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import track metadata from a source feed into a local SQLite archive."
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--source-file", help="Path to a JSON file with track metadata.")
    source_group.add_argument("--source-url", help="HTTP(S) URL returning JSON track metadata.")
    parser.add_argument(
        "--database",
        default="archive.db",
        help="SQLite database file to create or update. Default: archive.db",
    )
    parser.add_argument(
        "--root-key",
        default=DEFAULT_ROOT_KEY,
        help=(
            "Key containing the track list when the source JSON is an object. "
            f"Default: {DEFAULT_ROOT_KEY}"
        ),
    )
    parser.add_argument(
        "--default-artist",
        default=DEFAULT_ARTIST,
        help=f"Fallback artist name when a record omits it. Default: {DEFAULT_ARTIST}",
    )
    return parser.parse_args()


def load_source_payload(source_file: str | None, source_url: str | None) -> Any:
    if source_file:
        return json.loads(Path(source_file).read_text(encoding="utf-8"))

    if not source_url:
        raise ValueError("Either source_file or source_url must be provided.")

    parsed_url = urlparse(source_url)
    if parsed_url.scheme not in {"http", "https"}:
        raise ValueError("source_url must use the http or https scheme.")

    with urlopen(source_url, timeout=30) as response:  # nosec B310
        final_url = response.geturl()
        if urlparse(final_url).scheme not in {"http", "https"}:
            raise ValueError("source_url redirect must remain on http or https.")

        content_type = (response.headers.get("Content-Type") or "").lower()
        if "application/json" not in content_type and "+json" not in content_type:
            raise ValueError("source_url must return a JSON response.")

        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                parsed_content_length = int(content_length)
            except ValueError:
                parsed_content_length = None
            if parsed_content_length is not None and parsed_content_length > MAX_SOURCE_BYTES:
                raise ValueError(f"source_url response exceeds {MAX_SOURCE_BYTES} bytes.")

        body = response.read(MAX_SOURCE_BYTES + 1)
        if len(body) > MAX_SOURCE_BYTES:
            raise ValueError(f"source_url response exceeds {MAX_SOURCE_BYTES} bytes.")

        return json.loads(body.decode("utf-8"))


def extract_items(payload: Any, root_key: str) -> Iterable[dict[str, Any]]:
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get(root_key)
        if items is None:
            if all(key in payload for key in ("id", "title")):
                items = [payload]
            else:
                raise ValueError(
                    f"JSON object does not contain the configured root key: {root_key}"
                )
    else:
        raise ValueError("Source payload must be a JSON object or array.")

    if not isinstance(items, list):
        raise ValueError("Track collection must be a JSON array.")

    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Each track item must be a JSON object.")
        yield item


def normalize_track(item: dict[str, Any], default_artist: str) -> TrackRecord:
    source_id = str(
        item.get("id")
        or item.get("track_id")
        or item.get("slug")
        or item.get("url")
        or ""
    ).strip()
    if not source_id:
        raise ValueError(f"Track record is missing a usable identifier: {item}")

    title = str(item.get("title") or item.get("name") or "").strip()
    if not title:
        raise ValueError(f"Track record is missing a title: {item}")

    raw_tags = item.get("tags") if "tags" in item else None
    tags: list[str] | None
    tags_provided = "tags" in item
    if raw_tags is None and not tags_provided:
        tags = None
    elif isinstance(raw_tags, str):
        tags = [part.strip() for part in raw_tags.split(",") if part.strip()]
    elif isinstance(raw_tags, list):
        tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
    else:
        raise ValueError(f"Track tags must be a list or comma-separated string: {item}")

    archived_at = item.get("archived_at")
    if archived_at is not None:
        archived_at = str(archived_at).strip() or None
    raw_status = str(item.get("status") or "").strip()
    status = raw_status or "pending"
    if item.get("archived") or archived_at:
        status = "archived"

    return TrackRecord(
        source_id=source_id,
        title=title,
        artist=str(item.get("artist") or default_artist).strip() or default_artist,
        source_url=_optional_str(item.get("source_url") or item.get("url")),
        audio_url=_optional_str(item.get("audio_url") or item.get("download_url")),
        status=status or "pending",
        archived_at=archived_at,
        tags=tags,
        tags_provided=tags_provided,
        raw_payload=json.dumps(item, ensure_ascii=False, sort_keys=True),
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            source_url TEXT,
            audio_url TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            archived_at TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            raw_payload TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def upsert_tracks(connection: sqlite3.Connection, tracks: Iterable[TrackRecord]) -> int:
    now = utcnow_iso()
    imported = 0

    for track in tracks:
        connection.execute(
            """
            INSERT INTO tracks (
                source_id, title, artist, source_url, audio_url, status, archived_at,
                tags_json, raw_payload, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(?, '[]'), ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                title = excluded.title,
                artist = excluded.artist,
                source_url = COALESCE(excluded.source_url, tracks.source_url),
                audio_url = COALESCE(excluded.audio_url, tracks.audio_url),
                status = excluded.status,
                archived_at = COALESCE(excluded.archived_at, tracks.archived_at),
                tags_json = CASE WHEN ? THEN excluded.tags_json ELSE tracks.tags_json END,
                raw_payload = excluded.raw_payload,
                last_seen_at = excluded.last_seen_at
            """,
            (
                track.source_id,
                track.title,
                track.artist,
                track.source_url,
                track.audio_url,
                track.status,
                track.archived_at,
                json.dumps(track.tags, ensure_ascii=False) if track.tags is not None else None,
                track.raw_payload,
                now,
                now,
                track.tags_provided,
            ),
        )
        connection.commit()
        imported += 1
    return imported


def import_tracks(
    *,
    database_path: str,
    payload: Any,
    root_key: str,
    default_artist: str,
) -> int:
    items = extract_items(payload, root_key)
    tracks = (normalize_track(item, default_artist) for item in items)

    with sqlite3.connect(database_path) as connection:
        initialize_database(connection)
        return upsert_tracks(connection, tracks)


def main() -> int:
    args = parse_args()
    payload = load_source_payload(args.source_file, args.source_url)
    imported = import_tracks(
        database_path=args.database,
        payload=payload,
        root_key=args.root_key,
        default_artist=args.default_artist,
    )
    print(f"Imported {imported} track(s) into {args.database}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
