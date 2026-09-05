import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generator


class FakeResponse:
    def __init__(self, body: bytes, *, content_type: str, content_length: str | None = None) -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._body
        return self._body[:size]


class GeneratorTests(unittest.TestCase):
    def test_load_source_payload_rejects_non_http_scheme(self) -> None:
        with self.assertRaisesRegex(ValueError, "http or https"):
            generator.load_source_payload(None, "file:///tmp/source.json")

    def test_load_source_payload_requires_json_response(self) -> None:
        with patch(
            "generator.urlopen",
            return_value=FakeResponse(b"<html></html>", content_type="text/html"),
        ):
            with self.assertRaisesRegex(ValueError, "JSON response"):
                generator.load_source_payload(None, "https://example.com/tracks")

    def test_load_source_payload_rejects_large_response(self) -> None:
        huge_length = str(generator.MAX_SOURCE_BYTES + 1)
        with patch(
            "generator.urlopen",
            return_value=FakeResponse(b"{}", content_type="application/json", content_length=huge_length),
        ):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                generator.load_source_payload(None, "https://example.com/tracks")

    def test_load_and_import_from_file_payload(self) -> None:
        payload = {
            "tracks": [
                {
                    "id": "song-001",
                    "title": "Solar Echo",
                    "artist": "virtualluser",
                    "tags": ["ambient", "synthwave"],
                    "url": "https://example.com/tracks/song-001",
                    "audio_url": "https://example.com/audio/song-001.mp3",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            source_path = Path(temp_dir) / "source.json"
            database_path = Path(temp_dir) / "archive.db"
            source_path.write_text(json.dumps(payload), encoding="utf-8")

            loaded_payload = generator.load_source_payload(str(source_path), None)
            imported = generator.import_tracks(
                database_path=str(database_path),
                payload=loaded_payload,
                root_key="tracks",
                default_artist="fallback-artist",
            )

            self.assertEqual(imported, 1)

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    "SELECT source_id, title, artist, status, tags_json FROM tracks"
                ).fetchone()

            self.assertEqual(
                row,
                (
                    "song-001",
                    "Solar Echo",
                    "virtualluser",
                    "pending",
                    '["ambient", "synthwave"]',
                ),
            )

    def test_upsert_updates_existing_track(self) -> None:
        first_payload = [
            {
                "id": "song-002",
                "title": "Night Signal",
                "tags": "electronic, cinematic",
                "url": "https://example.com/tracks/song-002",
                "audio_url": "https://example.com/audio/song-002.mp3",
            }
        ]
        second_payload = [
            {
                "id": "song-002",
                "title": "Night Signal (Archive Cut)",
                "artist": "guest",
                "status": "archived",
                "archived_at": "2026-09-05T10:00:00+00:00",
                "tags": ["electronic"],
            }
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "archive.db"

            generator.import_tracks(
                database_path=str(database_path),
                payload=first_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )
            generator.import_tracks(
                database_path=str(database_path),
                payload=second_payload,
                root_key="tracks",
                default_artist="virtualluser",
            )

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    """
                    SELECT title, artist, source_url, audio_url, status, archived_at, tags_json
                    FROM tracks
                    WHERE source_id = 'song-002'
                    """
                ).fetchone()
                count = connection.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

            self.assertEqual(count, 1)
            self.assertEqual(
                row,
                (
                    "Night Signal (Archive Cut)",
                    "guest",
                    "https://example.com/tracks/song-002",
                    "https://example.com/audio/song-002.mp3",
                    "archived",
                    "2026-09-05T10:00:00+00:00",
                    '["electronic"]',
                ),
            )

    def test_archived_at_implies_archived_status(self) -> None:
        record = generator.normalize_track(
            {
                "id": "song-003",
                "title": "Dawn Pulse",
                "archived_at": "2026-09-05T10:00:00+00:00",
            },
            "virtualluser",
        )

        self.assertEqual(record.status, "archived")

    def test_archived_at_overrides_conflicting_status(self) -> None:
        record = generator.normalize_track(
            {
                "id": "song-004",
                "title": "Orbit Fade",
                "status": "pending",
                "archived_at": "2026-09-05T10:00:00+00:00",
            },
            "virtualluser",
        )

        self.assertEqual(record.status, "archived")


if __name__ == "__main__":
    unittest.main()
