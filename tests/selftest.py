#!/usr/bin/env python3
"""End-to-end self-test for the sky-survey ZIP fetch service.

Runs against a live server. Phase "pre-restart" builds fixtures, exercises
every endpoint and the Range machinery, then records state; phase
"post-restart" (run after bouncing the server) proves old links still return
the original bytes.

Usage:
    python selftest.py --base http://127.0.0.1:8123 \
        --sources /path/to/data/sources --state /path/to/state.json \
        --phase pre-restart|post-restart
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import random
import struct
import sys
import zipfile
from pathlib import Path

import httpx

CHUNK = 1 << 18  # must match the server streaming chunk size for one test


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    print(f"  ok: {msg}")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def fits_bytes(tag: bytes, blocks: int) -> bytes:
    cards = [
        "SIMPLE  =                    T / conforms to FITS standard",
        "BITPIX  =                    8",
        "NAXIS   =                    0",
        "END",
    ]
    header = "".join(c.ljust(80) for c in cards).encode("ascii").ljust(2880, b" ")
    rng = random.Random(tag)
    body = bytes(rng.getrandbits(8) for _ in range(2880 * (blocks - 1)))
    return header + body


class Unseekable(io.RawIOBase):
    def __init__(self):
        self.buf = io.BytesIO()

    def writable(self):
        return True

    def write(self, b):
        return self.buf.write(b)


def make_zip(path: Path, member: str, content: bytes,
             compression=zipfile.ZIP_DEFLATED, extra_member=None,
             unseekable=False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if unseekable:
        sink = Unseekable()
        with zipfile.ZipFile(sink, "w", compression) as z:
            z.writestr(member, content)
        path.write_bytes(sink.buf.getvalue())
        return
    with zipfile.ZipFile(path, "w", compression) as z:
        z.writestr(member, content)
        if extra_member:
            z.writestr(extra_member, b"junk")


def patch_encrypted(path: Path) -> None:
    data = bytearray(path.read_bytes())
    assert data[:4] == b"PK\x03\x04"
    data[6] |= 0x01  # local header flags: set bit 0 (encrypted)
    i = data.find(b"PK\x01\x02")
    assert i > 0
    data[i + 8] |= 0x01  # central header flags
    path.write_bytes(data)


def source_data_window(zip_path: Path):
    """Return (raw compressed bytes, ZipInfo) of the first member."""
    data = zip_path.read_bytes()
    zf = zipfile.ZipFile(io.BytesIO(data))
    info = zf.infolist()[0]
    name_len, extra_len = struct.unpack_from("<HH", data, info.header_offset + 26)
    start = info.header_offset + 30 + name_len + extra_len
    return data[start:start + info.compress_size], info


# ---------------------------------------------------------------------------
# Phase: pre-restart
# ---------------------------------------------------------------------------

def pre(client: httpx.Client, sources: Path, state_path: Path) -> None:
    state = {"fits": {}}
    fits = {}

    print("== fixtures ==")
    fits["A1"] = fits_bytes(b"OBS-A-v1", 3)
    fits["A2"] = fits_bytes(b"OBS-A-v2", 4)
    fits["B1"] = fits_bytes(b"OBS-B-v1", 3)
    fits["B3"] = fits_bytes(b"OBS-B-v3", 3)
    fits["C1"] = fits_bytes(b"OBS-C-v1", 520)  # ~1.5 MB, multi-chunk streaming
    fits["D1"] = fits_bytes(b"OBS-D-v1", 2)
    make_zip(sources / "obsA/v1.zip", "OBS-A.fits", fits["A1"])
    make_zip(sources / "obsA/v2.zip", "OBS-A.fits", fits["A2"])
    make_zip(sources / "obsB/v1.zip", "B.fits", fits["B1"], unseekable=True)  # data descriptor
    make_zip(sources / "obsC/v1.zip", "whatever.fits", fits["C1"])
    make_zip(sources / "obsD/v1.zip", "OBS-D.fits", fits["D1"])
    # invalid candidates
    make_zip(sources / "bad/twomember.zip", "a.fits", fits["A1"], extra_member="b.fits")
    make_zip(sources / "bad/stored.zip", "a.fits", fits["A1"], compression=zipfile.ZIP_STORED)
    make_zip(sources / "bad/encrypted.zip", "a.fits", fits["A1"])
    patch_encrypted(sources / "bad/encrypted.zip")
    make_zip(sources / "bad/notfits.zip", "a.fits", b"this is not a FITS file at all" * 100)
    make_zip(sources / "bad/truncated.zip", "a.fits", fits["A1"])
    t = sources / "bad/truncated.zip"
    t.write_bytes(t.read_bytes()[:-100])
    for k, v in fits.items():
        state["fits"][k] = sha(v)

    print("== health & docs ==")
    r = client.get("/healthz")
    check(r.status_code == 200 and r.json()["status"] == "ok", "healthz ok")
    r = client.get("/docs")
    check(r.status_code == 200, "swagger docs served at /docs")

    print("== register v1 ==")
    for obs, rel in [("OBS-A", "obsA/v1.zip"), ("OBS-B", "obsB/v1.zip"),
                     ("OBS-C", "obsC/v1.zip"), ("OBS-D", "obsD/v1.zip")]:
        r = client.post(f"/observations/{obs}/versions", json={"path": rel})
        check(r.status_code == 201 and r.json()["version"] == 1, f"{obs} v1 registered ({rel})")

    print("== invalid registrations keep the original version ==")
    for rel, why in [("bad/twomember.zip", "two members"),
                     ("bad/stored.zip", "stored not deflate"),
                     ("bad/encrypted.zip", "encrypted flag"),
                     ("bad/notfits.zip", "not FITS"),
                     ("bad/truncated.zip", "truncated"),
                     ("bad/missing.zip", "missing file"),
                     ("../escape.zip", "path traversal")]:
        r = client.post("/observations/OBS-C/versions", json={"path": rel})
        check(r.status_code == 422, f"rejected {why}: {rel} -> 422")
    r = client.post("/observations/OBS-C/versions", json={"path": "obsA/v1.zip"})
    check(r.status_code == 422, "rejected re-registration of an already pinned path")
    r = client.get("/observations/OBS-C")
    check(r.json()["current_version"] == 1, "OBS-C current version still 1 after failures")

    print("== order validation ==")
    r = client.post("/orders", json={"obs_ids": []})
    check(r.status_code == 422, "empty order rejected")
    r = client.post("/orders", json={"obs_ids": ["OBS-A", "OBS-A"]})
    check(r.status_code == 422, "duplicate ids rejected")
    r = client.post("/orders", json={"obs_ids": [f"OBS-{i:02d}" for i in range(33)]})
    check(r.status_code == 422, "33 ids rejected (limit 32)")
    r = client.post("/orders", json={"obs_ids": ["OBS-A", "NOPE"]})
    check(r.status_code == 409 and "NOPE" in str(r.json()), "unknown id -> 409, whole order rejected")

    print("== order 1: full download ==")
    r = client.post("/orders", json={"obs_ids": ["OBS-A", "OBS-B", "OBS-C"]})
    check(r.status_code == 201, "order 1 created")
    o1 = r.json()
    state["order1_id"] = o1["order_id"]
    check(o1["size"] == sum(30 + len(m["name"]) + m["compressed_size"] for m in o1["members"])
          + sum(46 + len(m["name"]) for m in o1["members"]) + 22, "manifest size arithmetic")
    r = client.get(o1["url"])
    check(r.status_code == 200, "full download 200")
    check(int(r.headers["content-length"]) == o1["size"], "content-length matches manifest")
    check(r.headers.get("accept-ranges") == "bytes", "accept-ranges: bytes")
    full1 = r.content
    state["order1_sha"] = sha(full1)
    zf = zipfile.ZipFile(io.BytesIO(full1))
    check(zf.testzip() is None, "standard unzip: CRC check passed")
    names = zf.namelist()
    check(names == ["OBS-A.fits", "OBS-B.fits", "OBS-C.fits"], "members named by observation id")
    check(zf.read("OBS-A.fits") == fits["A1"], "OBS-A.fits inflates to original bytes")
    check(zf.read("OBS-B.fits") == fits["B1"], "OBS-B.fits inflates to original bytes (descriptor source)")
    check(zf.read("OBS-C.fits") == fits["C1"], "OBS-C.fits inflates to original bytes")
    check(zf.getinfo("OBS-A.fits").compress_type == zipfile.ZIP_DEFLATED, "members are deflate")

    print("== order 1: compressed data reused verbatim ==")
    manifest = client.get(f"/orders/{o1['order_id']}").json()
    for m, rel in [(manifest["members"][0], "obsA/v1.zip"),
                   (manifest["members"][1], "obsB/v1.zip"),
                   (manifest["members"][2], "obsC/v1.zip")]:
        window, info = source_data_window(sources / rel)
        got = full1[m["data_offset"]: m["data_offset"] + m["compressed_size"]]
        check(got == window, f"{m['name']}: compressed bytes identical to source package")
        check(int(m["crc32"], 16) == info.CRC, f"{m['name']}: CRC carried over")

    print("== order 1: EOCD sanity ==")
    eocd = full1[-22:]
    check(eocd[:4] == b"PK\x05\x06", "EOCD signature at tail")
    n_disk, n_tot, cd_size, cd_off = struct.unpack_from("<HHII", eocd, 8)
    check(n_disk == n_tot == 3, "EOCD member counts = 3")
    check(cd_off == o1["central_directory_offset"], "EOCD central offset matches manifest")
    check(cd_off + cd_size + 22 == len(full1), "central directory contiguous before EOCD")

    print("== order 1: range requests ==")
    total = len(full1)
    infos = {i.filename: i for i in zf.infolist()}
    a = infos["OBS-A.fits"]
    b = infos["OBS-B.fits"]
    a_data_end = a.header_offset + 30 + len(a.filename) + a.compress_size
    b_data_start = b.header_offset + 30 + len(b.filename)
    cases = [
        (0, 99, "first 100 bytes (local header + data)"),
        (a_data_end - 5, b_data_start + 5, "span across member boundary A->B"),
        (b_data_start + 10, b_data_start + 50, "inside member B data"),
        (cd_off - 10, cd_off + 10, "span from last member into central directory"),
        (total - 30, total - 1, "span central directory into EOCD"),
        (0, total - 1, "range covering the whole file"),
    ]
    c_info = infos["OBS-C.fits"]
    c_data_start = c_info.header_offset + 30 + len(c_info.filename)
    cases.append((c_data_start + 700_000, c_data_start + 700_999, "deep inside large member C data"))
    for start, end, why in cases:
        r = client.get(o1["url"], headers={"Range": f"bytes={start}-{end}"})
        check(r.status_code == 206, f"206 for {why}")
        check(r.headers["content-range"] == f"bytes {start}-{end}/{total}", f"content-range for {why}")
        check(r.content == full1[start:end + 1], f"exact bytes for {why}")
    r = client.get(o1["url"], headers={"Range": "bytes=-22"})
    check(r.status_code == 206 and r.content == full1[-22:], "suffix range returns EOCD")
    r = client.get(o1["url"], headers={"Range": "bytes=0-0"})
    check(r.status_code == 206 and r.content == full1[:1], "single byte range")
    r = client.get(o1["url"], headers={"Range": f"bytes={total}-"})
    check(r.status_code == 416 and r.headers["content-range"] == f"bytes */{total}", "unsatisfiable range -> 416")
    r = client.get(o1["url"], headers={"Range": "bytes=0-1,5-6"})
    check(r.status_code == 200 and r.content == full1, "multi-range falls back to 200 full body")
    r = client.get(o1["url"], headers={"Range": "bytes=5-3"})
    check(r.status_code == 200 and r.content == full1, "backwards range ignored -> 200")
    r = client.head(o1["url"])
    check(r.status_code == 200 and int(r.headers["content-length"]) == total and not r.content,
          "HEAD returns size without body")

    print("== version change: old links keep original bytes ==")
    r = client.post("/observations/OBS-A/versions", json={"path": "obsA/v2.zip"})
    check(r.status_code == 201 and r.json()["version"] == 2, "OBS-A v2 registered")
    r = client.get("/observations/OBS-A")
    check(r.json()["current_version"] == 2 and len(r.json()["versions"]) == 2, "OBS-A current = v2, history kept")
    r = client.post("/observations/OBS-B/upload",
                    files={"file": ("B-new.zip", _zip_bytes("B-new.fits", fits["B3"]), "application/zip")})
    check(r.status_code == 201 and r.json()["version"] == 2, "OBS-B v2 registered via upload")
    check((sources / r.json()["source_path"]).is_file(), "uploaded package stored under versioned path")

    r = client.post("/orders", json={"obs_ids": ["OBS-A", "OBS-C"]})
    o2 = r.json()
    state["order2_id"] = o2["order_id"]
    full2 = client.get(o2["url"]).content
    state["order2_sha"] = sha(full2)
    zf2 = zipfile.ZipFile(io.BytesIO(full2))
    check(zf2.read("OBS-A.fits") == fits["A2"], "new order carries OBS-A v2 bytes")
    check(zf2.read("OBS-C.fits") == fits["C1"], "new order still carries OBS-C v1 bytes")

    r = client.get(o1["url"])
    check(sha(r.content) == state["order1_sha"], "order 1 link unchanged after v2 registration")
    zf_old = zipfile.ZipFile(io.BytesIO(r.content))
    check(zf_old.read("OBS-A.fits") == fits["A1"], "order 1 still carries OBS-A v1 bytes")
    check(zf_old.read("OBS-B.fits") == fits["B1"], "order 1 still carries OBS-B v1 bytes")

    print("== source availability ==")
    d_src = sources / "obsD/v1.zip"
    d_bak = sources / "obsD/v1.zip.bak"
    d_src.rename(d_bak)
    r = client.post("/orders", json={"obs_ids": ["OBS-D"]})
    check(r.status_code == 409, "missing source package -> whole order rejected (409)")
    d_bak.rename(d_src)
    st = os.stat(d_src)
    os.utime(d_src, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    r = client.post("/orders", json={"obs_ids": ["OBS-D"]})
    check(r.status_code == 409, "tampered mtime -> source considered unavailable (409)")
    os.utime(d_src, ns=(st.st_atime_ns, st.st_mtime_ns))
    r = client.post("/orders", json={"obs_ids": ["OBS-D"]})
    check(r.status_code == 201, "order accepted again once source restored")
    o3 = r.json()
    state["order3_id"] = o3["order_id"]
    state["order3_sha"] = sha(client.get(o3["url"]).content)
    d_src.rename(d_bak)
    r = client.get(o3["url"])
    check(r.status_code == 410, "removed source package -> 410 on download")
    d_bak.rename(d_src)
    r = client.get(o3["url"])
    check(r.status_code == 200 and sha(r.content) == state["order3_sha"], "download restored with source")

    print("== listings ==")
    r = client.get("/observations")
    check(r.status_code == 200 and len(r.json()["observations"]) == 4, "observation list")
    r = client.get("/orders")
    check(r.status_code == 200 and len(r.json()["orders"]) >= 3, "order list")

    state_path.write_text(json.dumps(state))
    print(f"pre-restart phase complete; state saved to {state_path}")


def _zip_bytes(member: str, content: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, content)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Phase: post-restart
# ---------------------------------------------------------------------------

def post(client: httpx.Client, sources: Path, state_path: Path) -> None:
    state = json.loads(state_path.read_text())
    print("== post-restart: old links return original bytes ==")
    r = client.get("/healthz")
    check(r.status_code == 200, "server healthy after restart")
    for key in ("order1", "order2", "order3"):
        oid = state[f"{key}_id"]
        r = client.get(f"/orders/{oid}/zip")
        check(r.status_code == 200, f"{key} link alive after restart")
        check(sha(r.content) == state[f"{key}_sha"], f"{key} bytes identical after restart")
        if key == "order1":
            total = len(r.content)
            rr = client.get(f"/orders/{oid}/zip", headers={"Range": f"bytes={total - 22}-"})
            check(rr.status_code == 206 and rr.content == r.content[-22:], "range still exact after restart")

    print("== post-restart: service fully functional ==")
    r = client.post("/orders", json={"obs_ids": ["OBS-A", "OBS-B", "OBS-C"]})
    check(r.status_code == 201, "new order after restart")
    full = client.get(r.json()["url"]).content
    zf = zipfile.ZipFile(io.BytesIO(full))
    check(zf.testzip() is None, "new order unzips cleanly")
    check(sha(zf.read("OBS-A.fits")) == state["fits"]["A2"], "OBS-A serves v2 after restart")
    check(sha(zf.read("OBS-B.fits")) == state["fits"]["B3"], "OBS-B serves uploaded v2 after restart")
    check(sha(zf.read("OBS-C.fits")) == state["fits"]["C1"], "OBS-C serves v1 after restart")
    print("post-restart phase complete")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8123")
    ap.add_argument("--sources", required=True, type=Path)
    ap.add_argument("--state", required=True, type=Path)
    ap.add_argument("--phase", choices=["pre-restart", "post-restart"], required=True)
    args = ap.parse_args()
    client = httpx.Client(base_url=args.base, timeout=120)
    if args.phase == "pre-restart":
        pre(client, args.sources, args.state)
    else:
        post(client, args.sources, args.state)


if __name__ == "__main__":
    main()
