from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, List, Optional


WITH_OUTLIERS_TABLE = "minute_avg_with_outliers"
NO_OUTLIERS_TABLE = "minute_avg_no_outliers"
CELL_CONFIG_TABLE = "cell_config"
VOC_ALERT_TABLE = "voc_alert_state"
PRODUCT_PROFILES_TABLE = "product_profiles"

VOC_MINUTE_COLUMNS = {
    "voc_change_score": "REAL",
    "autonomous_state": "INTEGER",
    "baseline_valid": "INTEGER",
    "environment_fault": "INTEGER",
    "voc_6sigma": "INTEGER",
    "voc_10sigma": "INTEGER",
    "measurement_usable": "INTEGER",
}


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
        for table in (WITH_OUTLIERS_TABLE, NO_OUTLIERS_TABLE):
            self._add_columns(table, VOC_MINUTE_COLUMNS)
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
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {CELL_CONFIG_TABLE} (
                node_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                mode TEXT NOT NULL,
                category_id TEXT,
                temperature_json TEXT NOT NULL,
                humidity_json TEXT NOT NULL,
                gas_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._add_columns(
            CELL_CONFIG_TABLE,
            {
                "voc_policy_json": "TEXT",
                "assignment_mode": "TEXT",
                "profile_id": "TEXT",
                "profile_snapshot_json": "TEXT",
                "voc_policy_override_json": "TEXT",
            },
        )
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {PRODUCT_PROFILES_TABLE} (
                profile_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                temperature_json TEXT NOT NULL,
                humidity_json TEXT NOT NULL,
                gas_json TEXT NOT NULL,
                voc_policy_json TEXT NOT NULL,
                source_category_id TEXT UNIQUE,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {VOC_ALERT_TABLE} (
                node_id INTEGER PRIMARY KEY,
                alert_latched INTEGER NOT NULL DEFAULT 0,
                latched_at REAL,
                cleared_at REAL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._add_columns(
            VOC_ALERT_TABLE,
            {
                "warning_active": "INTEGER NOT NULL DEFAULT 0",
                "latch_severity": "TEXT NOT NULL DEFAULT 'none'",
                "warning_started_at": "REAL",
                "warning_cleared_at": "REAL",
                "alert_latched_at": "REAL",
                "critical_latched_at": "REAL",
                "state_changed_at": "REAL",
            },
        )
        # Migrate the original binary latch into the richer severity column.
        self.conn.execute(
            f"""
            UPDATE {VOC_ALERT_TABLE}
            SET latch_severity='alert',
                alert_latched_at=COALESCE(alert_latched_at, latched_at),
                state_changed_at=COALESCE(state_changed_at, updated_at)
            WHERE alert_latched=1 AND latch_severity='none'
            """
        )
        self.conn.commit()

    def _add_columns(self, table: str, columns: dict[str, str]) -> None:
        """Apply additive, idempotent SQLite migrations."""
        assert self.conn is not None
        existing = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, column_type in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")

    def upsert_minute_average(self, table: str, row: dict, auto_commit: bool = True) -> None:
        assert self.conn is not None
        self.conn.execute(
            f"""
            INSERT INTO {table} (
                minute_ts, node_id, protocol_version, flags,
                temperature_c, humidity_rh, proximity, ambient_light, white_light,
                ambient_lux, voc_index, nox_index, sraw_voc, sraw_nox,
                voc_change_score, autonomous_state, baseline_valid,
                environment_fault, voc_6sigma, voc_10sigma, measurement_usable,
                sample_count, computed_at
            ) VALUES (
                :minute_ts, :node_id, :protocol_version, :flags,
                :temperature_c, :humidity_rh, :proximity, :ambient_light, :white_light,
                :ambient_lux, :voc_index, :nox_index, :sraw_voc, :sraw_nox,
                :voc_change_score, :autonomous_state, :baseline_valid,
                :environment_fault, :voc_6sigma, :voc_10sigma, :measurement_usable,
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
                voc_change_score=excluded.voc_change_score,
                autonomous_state=excluded.autonomous_state,
                baseline_valid=excluded.baseline_valid,
                environment_fault=excluded.environment_fault,
                voc_6sigma=excluded.voc_6sigma,
                voc_10sigma=excluded.voc_10sigma,
                measurement_usable=excluded.measurement_usable,
                sample_count=excluded.sample_count,
                computed_at=excluded.computed_at
            """,
            row,
        )
        if auto_commit:
            self.conn.commit()

    def upsert_node_health(
        self,
        node_id: int,
        last_seen: float,
        last_sequence: int,
        dropped_sequences_total: int,
        address: str,
        name: str,
        auto_commit: bool = True,
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
        if auto_commit:
            self.conn.commit()

    def commit(self) -> None:
        assert self.conn is not None
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

    def count_cell_configs(self) -> int:
        assert self.conn is not None
        row = self.conn.execute(f"SELECT COUNT(*) AS c FROM {CELL_CONFIG_TABLE}").fetchone()
        return int(row["c"])

    def load_cell_configs(self) -> dict[int, dict[str, Any]]:
        assert self.conn is not None
        rows = self.conn.execute(
            f"""
            SELECT node_id, name, mode, category_id,
                   temperature_json, humidity_json, gas_json, voc_policy_json,
                   assignment_mode, profile_id, profile_snapshot_json,
                   voc_policy_override_json
            FROM {CELL_CONFIG_TABLE}
            ORDER BY node_id ASC
            """
        ).fetchall()
        cells: dict[int, dict[str, Any]] = {}
        for row in rows:
            cells[int(row["node_id"])] = {
                "name": row["name"],
                "mode": row["mode"],
                "category_id": row["category_id"],
                "temperature": json.loads(row["temperature_json"]),
                "humidity": json.loads(row["humidity_json"]),
                "gas": json.loads(row["gas_json"]),
                "voc_policy": json.loads(row["voc_policy_json"]) if row["voc_policy_json"] else None,
                "assignment_mode": row["assignment_mode"],
                "profile_id": row["profile_id"],
                "profile_snapshot": (
                    json.loads(row["profile_snapshot_json"])
                    if row["profile_snapshot_json"] else None
                ),
                "voc_policy_override": (
                    json.loads(row["voc_policy_override_json"])
                    if row["voc_policy_override_json"] else None
                ),
            }
        return cells

    def upsert_cell_config(self, node_id: int, entry: dict[str, Any], auto_commit: bool = True) -> None:
        assert self.conn is not None
        self.conn.execute(
            f"""
            INSERT INTO {CELL_CONFIG_TABLE} (
                node_id, name, mode, category_id,
                temperature_json, humidity_json, gas_json, voc_policy_json,
                assignment_mode, profile_id, profile_snapshot_json,
                voc_policy_override_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                name=excluded.name,
                mode=excluded.mode,
                category_id=excluded.category_id,
                temperature_json=excluded.temperature_json,
                humidity_json=excluded.humidity_json,
                gas_json=excluded.gas_json,
                voc_policy_json=excluded.voc_policy_json,
                assignment_mode=excluded.assignment_mode,
                profile_id=excluded.profile_id,
                profile_snapshot_json=excluded.profile_snapshot_json,
                voc_policy_override_json=excluded.voc_policy_override_json,
                updated_at=excluded.updated_at
            """,
            (
                int(node_id),
                entry["name"],
                entry["mode"],
                entry.get("category_id"),
                json.dumps(entry["temperature"]),
                json.dumps(entry["humidity"]),
                json.dumps(entry["gas"]),
                json.dumps(entry.get("voc_policy")) if entry.get("voc_policy") is not None else None,
                entry.get("assignment_mode"),
                entry.get("profile_id"),
                (
                    json.dumps(entry.get("profile_snapshot"))
                    if entry.get("profile_snapshot") is not None else None
                ),
                (
                    json.dumps(entry.get("voc_policy_override"))
                    if entry.get("voc_policy_override") is not None else None
                ),
                time.time(),
            ),
        )
        if auto_commit:
            self.conn.commit()

    def replace_cell_configs(self, cells: dict[int, dict[str, Any]]) -> None:
        assert self.conn is not None
        self.conn.execute(f"DELETE FROM {CELL_CONFIG_TABLE}")
        for node_id, entry in cells.items():
            self.upsert_cell_config(node_id, entry, auto_commit=False)
        self.conn.commit()

    def load_product_profiles(self) -> dict[str, dict[str, Any]]:
        assert self.conn is not None
        rows = self.conn.execute(
            f"SELECT * FROM {PRODUCT_PROFILES_TABLE} ORDER BY name, profile_id"
        ).fetchall()
        return {
            str(row["profile_id"]): {
                "profile_id": str(row["profile_id"]),
                "name": row["name"],
                "temperature": json.loads(row["temperature_json"]),
                "humidity": json.loads(row["humidity_json"]),
                "gas": json.loads(row["gas_json"]),
                "voc_policy": json.loads(row["voc_policy_json"]),
                "source_category_id": row["source_category_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def upsert_product_profile(
        self, profile: dict[str, Any], auto_commit: bool = True
    ) -> None:
        assert self.conn is not None
        now = time.time()
        self.conn.execute(
            f"""
            INSERT INTO {PRODUCT_PROFILES_TABLE} (
                profile_id, name, temperature_json, humidity_json, gas_json,
                voc_policy_json, source_category_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_id) DO UPDATE SET
                name=excluded.name,
                temperature_json=excluded.temperature_json,
                humidity_json=excluded.humidity_json,
                gas_json=excluded.gas_json,
                voc_policy_json=excluded.voc_policy_json,
                source_category_id=COALESCE({PRODUCT_PROFILES_TABLE}.source_category_id,
                                           excluded.source_category_id),
                updated_at=excluded.updated_at
            """,
            (
                profile["profile_id"],
                profile["name"],
                json.dumps(profile["temperature"]),
                json.dumps(profile["humidity"]),
                json.dumps(profile["gas"]),
                json.dumps(profile["voc_policy"]),
                profile.get("source_category_id"),
                profile.get("created_at", now),
                now,
            ),
        )
        if auto_commit:
            self.conn.commit()

    def delete_product_profile(self, profile_id: str, auto_commit: bool = True) -> None:
        assert self.conn is not None
        self.conn.execute(
            f"DELETE FROM {PRODUCT_PROFILES_TABLE} WHERE profile_id=?", (profile_id,)
        )
        if auto_commit:
            self.conn.commit()

    def save_profile_and_cells(
        self, profile: dict[str, Any], cells: dict[int, dict[str, Any]]
    ) -> None:
        assert self.conn is not None
        try:
            self.conn.execute("BEGIN")
            self.upsert_product_profile(profile, auto_commit=False)
            for node_id, entry in cells.items():
                self.upsert_cell_config(node_id, entry, auto_commit=False)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def is_voc_alert_latched(self, node_id: int) -> bool:
        assert self.conn is not None
        row = self.conn.execute(
            f"SELECT alert_latched FROM {VOC_ALERT_TABLE} WHERE node_id=?", (int(node_id),)
        ).fetchone()
        return bool(row and row["alert_latched"])

    def get_voc_state(self, node_id: int) -> dict[str, Any]:
        assert self.conn is not None
        row = self.conn.execute(
            f"SELECT * FROM {VOC_ALERT_TABLE} WHERE node_id=?", (int(node_id),)
        ).fetchone()
        if row is None:
            return {
                "warning_active": False,
                "latch_severity": "none",
                "warning_started_at": None,
                "warning_cleared_at": None,
                "alert_latched_at": None,
                "critical_latched_at": None,
                "state_changed_at": None,
            }
        result = dict(row)
        result["warning_active"] = bool(result.get("warning_active"))
        if result.get("latch_severity") not in {"none", "alert", "critical"}:
            result["latch_severity"] = "alert" if result.get("alert_latched") else "none"
        return result

    def set_voc_state(
        self,
        node_id: int,
        warning_active: bool,
        latch_severity: str,
        now: Optional[float] = None,
    ) -> None:
        assert self.conn is not None
        if latch_severity not in {"none", "alert", "critical"}:
            raise ValueError("invalid VOC latch severity")
        now = time.time() if now is None else now
        old = self.get_voc_state(node_id)
        warning_changed = bool(old["warning_active"]) != bool(warning_active)
        latch_changed = old["latch_severity"] != latch_severity
        self.conn.execute(
            f"""
            INSERT INTO {VOC_ALERT_TABLE} (
                node_id, alert_latched, latched_at, cleared_at, updated_at,
                warning_active, latch_severity, warning_started_at,
                warning_cleared_at, alert_latched_at, critical_latched_at,
                state_changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                alert_latched=excluded.alert_latched,
                latched_at=CASE WHEN excluded.alert_latched=1
                                THEN COALESCE({VOC_ALERT_TABLE}.latched_at, excluded.latched_at)
                                ELSE {VOC_ALERT_TABLE}.latched_at END,
                cleared_at=CASE WHEN excluded.alert_latched=0 AND {VOC_ALERT_TABLE}.alert_latched=1
                                THEN excluded.cleared_at ELSE {VOC_ALERT_TABLE}.cleared_at END,
                updated_at=excluded.updated_at,
                warning_active=excluded.warning_active,
                latch_severity=excluded.latch_severity,
                warning_started_at=CASE WHEN excluded.warning_active=1
                                     AND {VOC_ALERT_TABLE}.warning_active=0
                                     THEN excluded.warning_started_at
                                     ELSE {VOC_ALERT_TABLE}.warning_started_at END,
                warning_cleared_at=CASE WHEN excluded.warning_active=0
                                     AND {VOC_ALERT_TABLE}.warning_active=1
                                     THEN excluded.warning_cleared_at
                                     ELSE {VOC_ALERT_TABLE}.warning_cleared_at END,
                alert_latched_at=CASE WHEN excluded.latch_severity IN ('alert','critical')
                                   AND {VOC_ALERT_TABLE}.latch_severity='none'
                                   THEN excluded.alert_latched_at
                                   ELSE {VOC_ALERT_TABLE}.alert_latched_at END,
                critical_latched_at=CASE WHEN excluded.latch_severity='critical'
                                      AND {VOC_ALERT_TABLE}.latch_severity!='critical'
                                      THEN excluded.critical_latched_at
                                      ELSE {VOC_ALERT_TABLE}.critical_latched_at END,
                state_changed_at=excluded.state_changed_at
            """,
            (
                int(node_id),
                int(latch_severity != "none"),
                now if latch_severity != "none" else None,
                now if latch_severity == "none" and old["latch_severity"] != "none" else None,
                now,
                int(warning_active),
                latch_severity,
                now if warning_changed and warning_active else None,
                now if warning_changed and not warning_active else None,
                now if latch_changed and latch_severity in {"alert", "critical"} else None,
                now if latch_changed and latch_severity == "critical" else None,
                now if warning_changed or latch_changed else old.get("state_changed_at"),
            ),
        )
        self.conn.commit()

    def set_voc_alert_latched(self, node_id: int, latched: bool, now: Optional[float] = None) -> None:
        old = self.get_voc_state(node_id)
        self.set_voc_state(
            node_id,
            warning_active=bool(old["warning_active"]),
            latch_severity="alert" if latched else "none",
            now=now,
        )

    def get_voc_alert_states(self) -> dict[int, dict[str, Any]]:
        assert self.conn is not None
        rows = self.conn.execute(f"SELECT * FROM {VOC_ALERT_TABLE} ORDER BY node_id").fetchall()
        return {int(row["node_id"]): dict(row) for row in rows}

