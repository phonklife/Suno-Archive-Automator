import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import generator


class GeneratorTests(unittest.TestCase):
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
                    SELECT title, artist, status, archived_at, tags_json
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
                    "archived",
                    "2026-09-05T10:00:00+00:00",
                    '["electronic"]',
                ),
            )


if __name__ == "__main__":
    unittest.main()
