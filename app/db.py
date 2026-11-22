"""SQLite persistence: the observation-version index and the order manifests.

Everything the service needs after a restart lives here; assembled archives
are never written to disk.
"""
from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import zipkit

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
  obs_id          TEXT PRIMARY KEY,
  current_version INTEGER NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
  obs_id          TEXT NOT NULL,
  version         INTEGER NOT NULL,
  source_path     TEXT NOT NULL,  -- relative to the sources directory
  member_name     TEXT NOT NULL,
  crc32           INTEGER NOT NULL,
  csize           INTEGER NOT NULL,
  usize           INTEGER NOT NULL,
  mod_time        INTEGER NOT NULL,
  mod_date        INTEGER NOT NULL,
  src_data_offset INTEGER NOT NULL,
  file_size       INTEGER NOT NULL,
  file_mtime_ns   INTEGER NOT NULL,
  registered_at   TEXT NOT NULL,
  PRIMARY KEY (obs_id, version),
  UNIQUE (source_path)            -- a package file may be registered only once
);
CREATE TABLE IF NOT EXISTS orders (
  order_id     TEXT PRIMARY KEY,
  created_at   TEXT NOT NULL,
  zip_path     TEXT NOT NULL,     -- the fetch link handed to the analyst
  total_size   INTEGER NOT NULL,
  cd_offset    INTEGER NOT NULL,
  member_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS order_members (
  order_id        TEXT NOT NULL REFERENCES orders(order_id),
  seq             INTEGER NOT NULL,
  obs_id          TEXT NOT NULL,
  version         INTEGER NOT NULL,
  arc_name        TEXT NOT NULL,
  source_path     TEXT NOT NULL,
  src_data_offset INTEGER NOT NULL,
  crc32           INTEGER NOT NULL,
  csize           INTEGER NOT NULL,
  usize           INTEGER NOT NULL,
  mod_time        INTEGER NOT NULL,
  mod_date        INTEGER NOT NULL,
  lh_offset       INTEGER NOT NULL,
  file_size       INTEGER NOT NULL,
  file_mtime_ns   INTEGER NOT NULL,
  PRIMARY KEY (order_id, seq)
);
"""


class PathInUse(Exception):
    """The source package path is already pinned to a registered version."""


class OrderRejected(Exception):
    def __init__(self, status: int, detail):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db(db_path) -> None:
    con = connect(db_path)
    try:
        con.executescript(SCHEMA)
        con.execute("PRAGMA journal_mode=WAL")
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Version registration
# ---------------------------------------------------------------------------

def _next_version(con, obs_id: str) -> int:
    row = con.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 AS v FROM versions WHERE obs_id = ?",
        (obs_id,),
    ).fetchone()
    return row["v"]


def _insert_version(con, obs_id: str, version: int, info: zipkit.SourceMember, rel: str) -> None:
    now = _now()
    con.execute(
        """INSERT INTO versions
           (obs_id, version, source_path, member_name, crc32, csize, usize,
            mod_time, mod_date, src_data_offset, file_size, file_mtime_ns, registered_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            obs_id, version, rel, info.member_name, info.crc32, info.csize,
            info.usize, info.mod_time, info.mod_date, info.data_offset,
            info.file_size, info.file_mtime_ns, now,
        ),
    )
    con.execute(
        """INSERT INTO observations (obs_id, current_version, updated_at)
           VALUES (?,?,?)
           ON CONFLICT(obs_id) DO UPDATE SET
             current_version = excluded.current_version,
             updated_at = excluded.updated_at""",
        (obs_id, version, now),
    )


def register_version(db_path, obs_id: str, info: zipkit.SourceMember, rel: str) -> int:
    """Register an already-on-disk package as the new current version.

    Validation happened before this call; any failure here rolls the whole
    transaction back, so the previous current version is always preserved.
    """
    con = connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT obs_id, version FROM versions WHERE source_path = ?", (rel,)
        ).fetchone()
        if row:
            raise PathInUse(
                f"path {rel!r} is already registered as {row['obs_id']} v{row['version']}; "
                "place each new package at a fresh path"
            )
        version = _next_version(con, obs_id)
        _insert_version(con, obs_id, version, info, rel)
        con.commit()
        return version
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def register_uploaded(db_path, obs_id: str, info: zipkit.SourceMember, tmp_rel: str, sources_root: Path):
    """Register an uploaded package, moving it to its final versioned path."""
    con = connect(db_path)
    final_rel = None
    try:
        con.execute("BEGIN IMMEDIATE")
        version = _next_version(con, obs_id)
        final_rel = f"{obs_id}/v{version}.zip"
        n = 1
        while (sources_root / final_rel).exists() or con.execute(
            "SELECT 1 FROM versions WHERE source_path = ?", (final_rel,)
        ).fetchone():
            final_rel = f"{obs_id}/v{version}-{n}.zip"
            n += 1
        (sources_root / final_rel).parent.mkdir(parents=True, exist_ok=True)
        os.replace(sources_root / tmp_rel, sources_root / final_rel)
        _insert_version(con, obs_id, version, info, final_rel)
        con.commit()
        return version, final_rel
    except Exception:
        con.rollback()
        if final_rel:
            try:
                os.unlink(sources_root / final_rel)
            except OSError:
                pass
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Observation queries
# ---------------------------------------------------------------------------

def list_observations(db_path):
    con = connect(db_path)
    try:
        rows = con.execute(
            """SELECT o.obs_id, o.current_version, o.updated_at, v.csize, v.usize
               FROM observations o
               JOIN versions v ON v.obs_id = o.obs_id AND v.version = o.current_version
               ORDER BY o.obs_id"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_observation(db_path, obs_id: str):
    con = connect(db_path)
    try:
        obs = con.execute(
            "SELECT * FROM observations WHERE obs_id = ?", (obs_id,)
        ).fetchone()
        if not obs:
            return None
        versions = con.execute(
            """SELECT version, source_path, member_name, crc32, csize, usize,
                      registered_at
               FROM versions WHERE obs_id = ? ORDER BY version""",
            (obs_id,),
        ).fetchall()
        return {
            "obs_id": obs["obs_id"],
            "current_version": obs["current_version"],
            "updated_at": obs["updated_at"],
            "versions": [dict(v) for v in versions],
        }
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def create_order(db_path, sources_root: Path, obs_ids: list[str]):
    """Pin the current version of every requested observation in one
    transaction; any missing id or unavailable package rejects the order."""
    con = connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        marks = ",".join("?" for _ in obs_ids)
        rows = con.execute(
            f"""SELECT v.*, o.current_version
                FROM observations o
                JOIN versions v ON v.obs_id = o.obs_id AND v.version = o.current_version
                WHERE o.obs_id IN ({marks})""",
            obs_ids,
        ).fetchall()
        by_id = {r["obs_id"]: r for r in rows}
        missing = [i for i in obs_ids if i not in by_id]
        if missing:
            raise OrderRejected(
                409, {"error": "unknown observation ids", "missing": missing}
            )
        unavailable = []
        for i in obs_ids:
            r = by_id[i]
            if not zipkit.source_state_ok(
                sources_root / r["source_path"], r["file_size"], r["file_mtime_ns"]
            ):
                unavailable.append(i)
        if unavailable:
            raise OrderRejected(
                409,
                {
                    "error": "source packages unavailable; order rejected as a whole",
                    "unavailable": unavailable,
                },
            )

        members: list[zipkit.OutMember] = []
        rel_paths: list[str] = []
        meta: list[sqlite3.Row] = []
        for i in obs_ids:
            r = by_id[i]
            meta.append(r)
            rel_paths.append(r["source_path"])
            members.append(
                zipkit.OutMember(
                    obs_id=i,
                    version=r["version"],
                    arc_name=f"{i}.fits".encode("ascii"),
                    source_path=str(sources_root / r["source_path"]),
                    src_data_offset=r["src_data_offset"],
                    crc32=r["crc32"],
                    csize=r["csize"],
                    usize=r["usize"],
                    mod_time=r["mod_time"],
                    mod_date=r["mod_date"],
                )
            )
        try:
            cd_offset, _cd_size, total = zipkit.compute_layout(members)
        except zipkit.ZipTooLarge as exc:
            raise OrderRejected(422, {"error": str(exc)}) from exc

        order_id = uuid.uuid4().hex
        zip_path = f"/orders/{order_id}/zip"
        created_at = _now()
        con.execute(
            """INSERT INTO orders (order_id, created_at, zip_path, total_size, cd_offset, member_count)
               VALUES (?,?,?,?,?,?)""",
            (order_id, created_at, zip_path, total, cd_offset, len(members)),
        )
        for seq, (m, r, rel) in enumerate(zip(members, meta, rel_paths)):
            con.execute(
                """INSERT INTO order_members
                   (order_id, seq, obs_id, version, arc_name, source_path,
                    src_data_offset, crc32, csize, usize, mod_time, mod_date,
                    lh_offset, file_size, file_mtime_ns)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    order_id, seq, m.obs_id, m.version, m.arc_name.decode("ascii"),
                    rel, m.src_data_offset, m.crc32, m.csize, m.usize,
                    m.mod_time, m.mod_date, m.lh_offset,
                    r["file_size"], r["file_mtime_ns"],
                ),
            )
        con.commit()
        return order_id, zip_path, total, cd_offset, members, created_at
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def list_orders(db_path):
    con = connect(db_path)
    try:
        rows = con.execute(
            "SELECT order_id, created_at, zip_path, total_size, member_count FROM orders ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def get_order(db_path, sources_root: Path, order_id: str):
    """Return (order_row, members, member_rows) or None."""
    con = connect(db_path)
    try:
        order = con.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        ).fetchone()
        if not order:
            return None
        rows = con.execute(
            "SELECT * FROM order_members WHERE order_id = ? ORDER BY seq",
            (order_id,),
        ).fetchall()
        members = [
            zipkit.OutMember(
                obs_id=r["obs_id"],
                version=r["version"],
                arc_name=r["arc_name"].encode("ascii"),
                source_path=str(sources_root / r["source_path"]),
                src_data_offset=r["src_data_offset"],
                crc32=r["crc32"],
                csize=r["csize"],
                usize=r["usize"],
                mod_time=r["mod_time"],
                mod_date=r["mod_date"],
                lh_offset=r["lh_offset"],
            )
            for r in rows
        ]
        return order, members, rows
    finally:
        con.close()
