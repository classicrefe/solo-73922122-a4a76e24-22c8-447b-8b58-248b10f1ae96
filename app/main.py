"""Sky-survey ZIP fetch service.

Analysts pick observation ids, place an order (<= 32 ids), and receive a
stable link to a standard ZIP that is assembled on the fly: central directory
first (via HTTP Range on the tail), individual FITS members on demand.
"""
from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.params import Path as PathParam
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import db, zipkit
from .config import settings

OBS_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_obs_id_param = PathParam(pattern=OBS_ID_RE)


@asynccontextmanager
async def lifespan(_app):
    settings.sources.mkdir(parents=True, exist_ok=True)
    db.init_db(settings.db_path)
    yield


app = FastAPI(
    title="Sky Survey ZIP Fetch",
    version="1.0.0",
    description=(
        "Register single-FITS Deflate source packages, place orders of up to "
        "32 observation ids, and stream the assembled standard ZIP (whole "
        "file or single HTTP ranges). Compressed member data is reused "
        "verbatim from the pinned source packages; only the manifest and the "
        "version index are persisted (SQLite)."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class RegisterBody(BaseModel):
    path: str


class OrderBody(BaseModel):
    obs_ids: list[str]


def _resolve_rel(raw: str) -> str:
    """Normalize a user-supplied path and confine it to the sources dir."""
    p = PurePosixPath(raw)
    if p.is_absolute() or ".." in p.parts:
        raise HTTPException(422, "path must be relative and stay inside the sources directory")
    parts = [part for part in p.parts if part != "."]
    if not parts:
        raise HTTPException(422, "empty path")
    rel = PurePosixPath(*parts).as_posix()
    abs_p = (settings.sources / rel).resolve()
    if not abs_p.is_relative_to(settings.sources.resolve()):
        raise HTTPException(422, "path escapes the sources directory")
    return rel


def _inspect_or_422(path: Path) -> zipkit.SourceMember:
    try:
        return zipkit.inspect_source(path)
    except zipkit.SourceError as exc:
        raise HTTPException(422, f"invalid source package: {exc}") from exc


class _Unsatisfiable(Exception):
    pass


def _parse_single_range(header: str | None, size: int):
    """Parse a single byte range. Returns (start, end) inclusive, None to
    serve the full body, or raises _Unsatisfiable (-> 416)."""
    if not header:
        return None
    unit, sep, spec = header.partition("=")
    if not sep or unit.strip() != "bytes":
        return None
    spec = spec.strip()
    if "," in spec:
        return None  # multi-range unsupported: serve the whole archive
    first, sep, last = spec.partition("-")
    if not sep:
        return None
    first, last = first.strip(), last.strip()
    try:
        if first == "":
            suffix = int(last)
            if suffix <= 0:
                raise _Unsatisfiable
            return (max(0, size - suffix), size - 1)
        start = int(first)
        end = size - 1 if last == "" else int(last)
    except ValueError:
        return None
    if start < 0 or end < 0:
        return None
    if start >= size:
        raise _Unsatisfiable
    if end < start:
        return None  # invalid explicit range: ignore the header
    return (start, min(end, size - 1))


# ---------------------------------------------------------------------------
# Observations / versions
# ---------------------------------------------------------------------------

@app.post("/observations/{obs_id}/versions", status_code=201)
def register_version(obs_id: str = _obs_id_param, body: RegisterBody = ...):
    """Register a local source package (already dropped into the sources
    directory) as the new current version of an observation.

    The package is fully validated first; on any failure the previous
    current version is kept.
    """
    rel = _resolve_rel(body.path)
    src = settings.sources / rel
    if not src.is_file():
        raise HTTPException(422, f"source package not found in sources directory: {rel}")
    info = _inspect_or_422(src)
    try:
        version = db.register_version(settings.db_path, obs_id, info, rel)
    except db.PathInUse as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "obs_id": obs_id,
        "version": version,
        "current": True,
        "source_path": rel,
        "member_name": info.member_name,
        "crc32": f"{info.crc32:08x}",
        "compressed_size": info.csize,
        "uncompressed_size": info.usize,
    }


@app.post("/observations/{obs_id}/upload", status_code=201)
async def upload_version(obs_id: str = _obs_id_param, file: UploadFile = File(...)):
    """Register a new version by uploading the source package directly.

    The package is stored under sources/<obs_id>/v<N>.zip (never overwriting
    an earlier version), validated, then made current.
    """
    tmp_rel = f".incoming/{obs_id}-{uuid.uuid4().hex}.zip"
    tmp_abs = settings.sources / tmp_rel
    tmp_abs.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(tmp_abs, "wb") as out:
            while chunk := await file.read(1 << 20):
                out.write(chunk)
        info = _inspect_or_422(tmp_abs)
        version, final_rel = db.register_uploaded(
            settings.db_path, obs_id, info, tmp_rel, settings.sources
        )
    finally:
        try:
            tmp_abs.unlink()
        except OSError:
            pass
    return {
        "obs_id": obs_id,
        "version": version,
        "current": True,
        "source_path": final_rel,
        "member_name": info.member_name,
        "crc32": f"{info.crc32:08x}",
        "compressed_size": info.csize,
        "uncompressed_size": info.usize,
    }


@app.get("/observations")
def list_observations():
    return {"observations": db.list_observations(settings.db_path)}


@app.get("/observations/{obs_id}")
def get_observation(obs_id: str = _obs_id_param):
    obs = db.get_observation(settings.db_path, obs_id)
    if not obs:
        raise HTTPException(404, "unknown observation id")
    return obs


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

@app.post("/orders", status_code=201)
def create_order(body: OrderBody):
    """Pin the current versions of up to 32 observation ids into one order.

    All pins are taken from the same consistent snapshot; if any observation
    is unknown or its source package is unavailable, the whole order is
    rejected and nothing is stored.
    """
    ids = body.obs_ids
    if not ids:
        raise HTTPException(422, "obs_ids must not be empty")
    if len(ids) > settings.max_members:
        raise HTTPException(422, f"at most {settings.max_members} observation ids per order")
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "duplicate observation ids in order")
    for i in ids:
        if not re.match(OBS_ID_RE, i):
            raise HTTPException(422, f"invalid observation id: {i!r}")
    try:
        order_id, zip_path, total, cd_offset, members, created_at = db.create_order(
            settings.db_path, settings.sources, ids
        )
    except db.OrderRejected as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    return _order_json(order_id, zip_path, total, cd_offset, members, created_at=created_at)


def _order_json(order_id, zip_path, total, cd_offset, members, created_at=None):
    return {
        "order_id": order_id,
        "url": zip_path,
        "created_at": created_at,
        "size": total,
        "central_directory_offset": cd_offset,
        "member_count": len(members),
        "members": [
            {
                "seq": n,
                "obs_id": m.obs_id,
                "version": m.version,
                "name": m.arc_name.decode("ascii"),
                "method": "deflate",
                "crc32": f"{m.crc32:08x}",
                "compressed_size": m.csize,
                "uncompressed_size": m.usize,
                "local_header_offset": m.lh_offset,
                "data_offset": m.data_offset,
            }
            for n, m in enumerate(members)
        ],
    }


@app.get("/orders")
def list_orders():
    return {"orders": db.list_orders(settings.db_path)}


@app.get("/orders/{order_id}")
def get_order(order_id: str):
    got = db.get_order(settings.db_path, settings.sources, order_id)
    if not got:
        raise HTTPException(404, "order not found")
    order, members, _rows = got
    return _order_json(
        order_id, order["zip_path"], order["total_size"],
        order["cd_offset"], members, created_at=order["created_at"],
    )


@app.head("/orders/{order_id}/zip")
@app.get("/orders/{order_id}/zip")
def download_zip(order_id: str, request: Request):
    """Stream the assembled archive. Supports whole-file download and a
    single HTTP byte range (e.g. central directory first, members later)."""
    got = db.get_order(settings.db_path, settings.sources, order_id)
    if not got:
        raise HTTPException(404, "order not found")
    order, members, rows = got
    stale = [
        r["obs_id"]
        for r in rows
        if not zipkit.source_state_ok(
            settings.sources / r["source_path"], r["file_size"], r["file_mtime_ns"]
        )
    ]
    if stale:
        raise HTTPException(
            410, f"pinned source package changed or removed for: {', '.join(stale)}"
        )
    segments, total = zipkit.assemble_segments(members)
    if total != order["total_size"]:  # manifest integrity self-check
        raise HTTPException(500, "stored manifest is inconsistent")

    filename = f"skyfetch-{order_id}.zip"
    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{filename}"',
    }
    if request.method == "HEAD":
        return Response(
            status_code=200,
            media_type="application/zip",
            headers={**base_headers, "Content-Length": str(total)},
        )
    try:
        rng = _parse_single_range(request.headers.get("range"), total)
    except _Unsatisfiable:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"},
        )
    if rng is None:
        return StreamingResponse(
            zipkit.stream_segments(segments, 0, total - 1),
            media_type="application/zip",
            headers={**base_headers, "Content-Length": str(total)},
        )
    start, end = rng
    return StreamingResponse(
        zipkit.stream_segments(segments, start, end),
        status_code=206,
        media_type="application/zip",
        headers={
            **base_headers,
            "Content-Length": str(end - start + 1),
            "Content-Range": f"bytes {start}-{end}/{total}",
        },
    )


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")
