import sqlite3
import tempfile
import unittest
from pathlib import Path

from RaspberryPi.backend.storage.sqlite.sqlite_storage import SQLiteStorage


class StorageMigrationTests(unittest.TestCase):
    def test_additive_migration_preserves_legacy_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy.db"
            conn = sqlite3.connect(db_path)
            for table in ("minute_avg_with_outliers", "minute_avg_no_outliers"):
                conn.execute(
                    f"""
                    CREATE TABLE {table} (
                        minute_ts INTEGER NOT NULL, node_id INTEGER NOT NULL,
                        protocol_version INTEGER NOT NULL, flags INTEGER NOT NULL,
                        temperature_c REAL, humidity_rh REAL, proximity REAL,
                        ambient_light REAL, white_light REAL, ambient_lux REAL,
                        voc_index REAL, nox_index REAL, sraw_voc REAL, sraw_nox REAL,
                        sample_count INTEGER NOT NULL, computed_at REAL NOT NULL,
                        PRIMARY KEY (minute_ts, node_id)
                    )
                    """
                )
            conn.execute(
                """
                CREATE TABLE cell_config (
                    node_id INTEGER PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL,
                    category_id TEXT, temperature_json TEXT NOT NULL,
                    humidity_json TEXT NOT NULL, gas_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.commit()
            conn.close()

            storage = SQLiteStorage(str(db_path))
            storage.start()
            try:
                minute_columns = {
                    row["name"]
                    for row in storage.conn.execute(
                        "PRAGMA table_info(minute_avg_with_outliers)"
                    )
                }
                config_columns = {
                    row["name"]
                    for row in storage.conn.execute("PRAGMA table_info(cell_config)")
                }
                self.assertIn("voc_change_score", minute_columns)
                self.assertIn("measurement_usable", minute_columns)
                self.assertIn("voc_policy_json", config_columns)
                self.assertIn("assignment_mode", config_columns)
                self.assertIn("profile_snapshot_json", config_columns)
                profile_table = storage.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='product_profiles'"
                ).fetchone()
                self.assertIsNotNone(profile_table)
                voc_state_columns = {
                    row["name"]
                    for row in storage.conn.execute("PRAGMA table_info(voc_alert_state)")
                }
                self.assertIn("warning_active", voc_state_columns)
                self.assertIn("latch_severity", voc_state_columns)

                storage.set_voc_alert_latched(3073, True, now=10)
                self.assertTrue(storage.is_voc_alert_latched(3073))
                storage.stop()
                storage.start()
                self.assertTrue(storage.is_voc_alert_latched(3073))
            finally:
                storage.stop()


if __name__ == "__main__":
    unittest.main()
