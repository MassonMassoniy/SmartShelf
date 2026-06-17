from __future__ import annotations

import os
import sqlite3
import time
from typing import List, Optional


WITH_OUTLIERS_TABLE = "minute_avg_with_outliers"
NO_OUTLIERS_TABLE = "minute_avg_no_outliers"


class SQLiteStorage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def start(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def stop(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _init_schema(self) -> None:
        assert self.conn is not None
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {WITH_OUTLIERS_TABLE} (
                minute_ts INTEGER NOT NULL,
                node_id INTEGER NOT NULL,
                protocol_version INTEGER NOT NULL,
                flags INTEGER NOT NULL,
                temperature_c REAL,
                humidity_rh REAL,
                proximity REAL,
                ambient_light REAL,
                white_light REAL,
                ambient_lux REAL,
                voc_index REAL,
                nox_index REAL,
                sraw_voc REAL,
                sraw_nox REAL,
                sample_count INTEGER NOT NULL,
                computed_at REAL NOT NULL,
                PRIMARY KEY (minute_ts, node_id)
            )
            """
        )
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {NO_OUTLIERS_TABLE} (
                minute_ts INTEGER NOT NULL,
                node_id INTEGER NOT NULL,
                protocol_version INTEGER NOT NULL,
                flags INTEGER NOT NULL,
                temperature_c REAL,
                humidity_rh REAL,
                proximity REAL,
                ambient_light REAL,
                white_light REAL,
                ambient_lux REAL,
                voc_index REAL,
                nox_index REAL,
                sraw_voc REAL,
                sraw_nox REAL,
                sample_count INTEGER NOT NULL,
                computed_at REAL NOT NULL,
                PRIMARY KEY (minute_ts, node_id)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS node_health (
                node_id INTEGER PRIMARY KEY,
                last_seen REAL NOT NULL,
                last_sequence INTEGER NOT NULL,
                dropped_sequences_total INTEGER NOT NULL,
                address TEXT,
                name TEXT
            )
            """
        )
        self.conn.commit()

    def upsert_minute_average(self, table: str, row: dict) -> None:
        assert self.conn is not None
        self.conn.execute(
            f"""
            INSERT INTO {table} (
                minute_ts, node_id, protocol_version, flags,
                temperature_c, humidity_rh, proximity, ambient_light, white_light,
                ambient_lux, voc_index, nox_index, sraw_voc, sraw_nox,
                sample_count, computed_at
            ) VALUES (
                :minute_ts, :node_id, :protocol_version, :flags,
                :temperature_c, :humidity_rh, :proximity, :ambient_light, :white_light,
                :ambient_lux, :voc_index, :nox_index, :sraw_voc, :sraw_nox,
                :sample_count, :computed_at
            )
            ON CONFLICT(minute_ts, node_id) DO UPDATE SET
                protocol_version=excluded.protocol_version,
                flags=excluded.flags,
                temperature_c=excluded.temperature_c,
                humidity_rh=excluded.humidity_rh,
                proximity=excluded.proximity,
                ambient_light=excluded.ambient_light,
                white_light=excluded.white_light,
                ambient_lux=excluded.ambient_lux,
                voc_index=excluded.voc_index,
                nox_index=excluded.nox_index,
                sraw_voc=excluded.sraw_voc,
                sraw_nox=excluded.sraw_nox,
                sample_count=excluded.sample_count,
                computed_at=excluded.computed_at
            """,
            row,
        )
        self.conn.commit()

    def upsert_node_health(
        self,
        node_id: int,
        last_seen: float,
        last_sequence: int,
        dropped_sequences_total: int,
        address: str,
        name: str,
    ) -> None:
        assert self.conn is not None
        self.conn.execute(
            """
            INSERT INTO node_health (
                node_id, last_seen, last_sequence, dropped_sequences_total, address, name
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                last_seen=excluded.last_seen,
                last_sequence=excluded.last_sequence,
                dropped_sequences_total=excluded.dropped_sequences_total,
                address=excluded.address,
                name=excluded.name
            """,
            (node_id, last_seen, last_sequence, dropped_sequences_total, address, name),
        )
        self.conn.commit()

    def get_minute_history(
        self,
        outliers: str,
        from_ts: Optional[int],
        to_ts: Optional[int],
        node_id: Optional[int],
    ) -> List[dict]:
        assert self.conn is not None
        table = WITH_OUTLIERS_TABLE if outliers == "include" else NO_OUTLIERS_TABLE
        query = f"SELECT * FROM {table} WHERE 1=1"
        params: list = []
        if from_ts is not None:
            query += " AND minute_ts >= ?"
            params.append(from_ts)
        if to_ts is not None:
            query += " AND minute_ts <= ?"
            params.append(to_ts)
        if node_id is not None:
            query += " AND node_id = ?"
            params.append(node_id)
        query += " ORDER BY minute_ts ASC, node_id ASC"

        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def latest_minute_rows(self, outliers: str) -> List[dict]:
        assert self.conn is not None
        table = WITH_OUTLIERS_TABLE if outliers == "include" else NO_OUTLIERS_TABLE
        latest_ts = self.conn.execute(f"SELECT MAX(minute_ts) as ts FROM {table}").fetchone()["ts"]
        if latest_ts is None:
            return []
        rows = self.conn.execute(
            f"SELECT * FROM {table} WHERE minute_ts=? ORDER BY node_id ASC", (latest_ts,)
        ).fetchall()
        return [dict(row) for row in rows]

    def health_summary(self) -> dict:
        assert self.conn is not None
        rows = self.conn.execute("SELECT COUNT(*) AS c FROM node_health").fetchone()
        return {"known_nodes": int(rows["c"]), "checked_at": time.time()}

