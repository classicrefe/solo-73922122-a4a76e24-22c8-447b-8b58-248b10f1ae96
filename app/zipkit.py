"""Core ZIP machinery.

Two jobs:

1. Inspect a local source package (read-only): prove it is a plain ZIP with
   exactly one Deflate-compressed FITS member, no encryption, no ZIP64, and
   that the compressed payload really inflates to the advertised bytes.
2. Assemble a "virtual" ZIP for an order: fresh local headers + central
   directory are generated in memory, while the compressed member data is
   referenced as byte windows into the source packages and streamed verbatim
   (never recompressed, never written to disk).
"""
from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass

LOCAL_SIG = 0x04034B50
CENTRAL_SIG = 0x02014B50
EOCD_SIG = 0x06054B50

METHOD_DEFLATE = 8
FLAG_ENCRYPTED = 0x01
U32_MAX = 0xFFFFFFFF
EOCD_LEN = 22
EOCD_SEARCH = EOCD_LEN + 65535  # EOCD + max comment
FITS_MAGIC = b"SIMPLE  ="

MAX_ZIP32_TOTAL = 4 * 1024**3  # assembled archive must stay strictly below 4 GiB

_LH = struct.Struct("<I5H3I2H")    # local file header, 30 bytes
_CD = struct.Struct("<I6H3I5H2I")  # central directory header, 46 bytes
_EOCD = struct.Struct("<I4H2IH")   # end of central directory, 22 bytes


class SourceError(Exception):
    """A candidate source package failed validation."""


class ZipTooLarge(Exception):
    """The assembled archive would exceed the 4 GiB ZIP32 limit."""


# ---------------------------------------------------------------------------
# Source package inspection
# ---------------------------------------------------------------------------

@dataclass
class SourceMember:
    path: str            # absolute path of the source package
    member_name: str     # original member name inside the source ZIP
    crc32: int
    csize: int           # compressed size
    usize: int           # uncompressed size
    mod_time: int        # DOS time/date copied verbatim into output archives
    mod_date: int
    data_offset: int     # absolute offset of the compressed data in the file
    file_size: int
    file_mtime_ns: int


def _find_eocd(tail: bytes) -> int:
    pos = len(tail)
    while True:
        pos = tail.rfind(b"PK\x05\x06", 0, pos)
        if pos < 0:
            raise SourceError("no end-of-central-directory record found")
        if pos + EOCD_LEN <= len(tail):
            (comment_len,) = struct.unpack_from("<H", tail, pos + 20)
            if pos + EOCD_LEN + comment_len == len(tail):
                return pos
        if pos == 0:
            raise SourceError("no end-of-central-directory record found")


def _verify_payload(f, offset: int, csize: int, usize: int, crc: int) -> None:
    """Inflate the whole member once: proves the deflate stream is intact,
    matches the central directory CRC/size, and starts with a FITS header."""
    f.seek(offset)
    dec = zlib.decompressobj(-15)  # raw deflate
    remaining = csize
    pending = b""
    out_len = 0
    check = 0
    head = b""
    while True:
        if not pending:
            if remaining == 0:
                break
            pending = f.read(min(1 << 18, remaining))
            if not pending:
                raise SourceError("unexpected end of file inside compressed data")
            remaining -= len(pending)
        out = dec.decompress(pending, 1 << 22)  # bounded memory per step
        pending = dec.unconsumed_tail
        if len(head) < len(FITS_MAGIC):
            head += out[: len(FITS_MAGIC) - len(head)]
        out_len += len(out)
        check = zlib.crc32(out, check)
        if out_len > usize:
            raise SourceError("uncompressed data exceeds the declared size")
        if dec.eof:
            break
    if not dec.eof:
        raise SourceError("deflate stream is truncated")
    tail = dec.flush()
    out_len += len(tail)
    check = zlib.crc32(tail, check)
    if out_len != usize:
        raise SourceError("uncompressed size does not match central directory")
    if check != crc:
        raise SourceError("CRC-32 does not match central directory")
    if head[: len(FITS_MAGIC)] != FITS_MAGIC:
        raise SourceError("member is not a FITS file (missing SIMPLE card)")


def inspect_source(path) -> SourceMember:
    """Validate a local source package and locate its compressed payload.

    Raises SourceError on any violation; the caller keeps the previous
    version untouched in that case.
    """
    try:
        st = os.stat(path)
    except OSError as exc:
        raise SourceError(f"cannot stat file: {exc}") from exc
    size = st.st_size
    if size < EOCD_LEN:
        raise SourceError("file is too small to be a ZIP archive")

    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - EOCD_SEARCH))
            tail = f.read()
            eocd_at = _find_eocd(tail)
            try:
                (
                    sig,
                    disk_no,
                    cd_disk,
                    disk_entries,
                    total_entries,
                    cd_size,
                    cd_offset,
                    _comment_len,
                ) = _EOCD.unpack_from(tail, eocd_at)
            except struct.error as exc:
                raise SourceError("malformed end-of-central-directory record") from exc
            assert sig == EOCD_SIG
            if disk_no or cd_disk:
                raise SourceError("multi-disk archives are not supported")
            if disk_entries != 1 or total_entries != 1:
                raise SourceError("source package must contain exactly one member")
            if cd_size >= U32_MAX or cd_offset >= U32_MAX:
                raise SourceError("ZIP64 archives are not supported")
            if cd_size > 1 << 20:
                raise SourceError("central directory is unreasonably large")
            if cd_size < _CD.size or cd_offset + cd_size > size:
                raise SourceError("central directory lies outside the file")

            f.seek(cd_offset)
            cd = f.read(cd_size)
            try:
                (
                    csig,
                    _ver_made,
                    _ver_need,
                    flags,
                    method,
                    mod_time,
                    mod_date,
                    crc,
                    csize,
                    usize,
                    name_len,
                    extra_len,
                    comment_len,
                    disk_start,
                    _int_attr,
                    _ext_attr,
                    local_offset,
                ) = _CD.unpack_from(cd, 0)
            except struct.error as exc:
                raise SourceError("malformed central directory header") from exc
            if csig != CENTRAL_SIG:
                raise SourceError("central directory signature mismatch")
            if flags & FLAG_ENCRYPTED:
                raise SourceError("encrypted members are not supported")
            if method != METHOD_DEFLATE:
                raise SourceError("member is not Deflate-compressed")
            if csize >= U32_MAX or usize >= U32_MAX or local_offset >= U32_MAX:
                raise SourceError("ZIP64 archives are not supported")
            if disk_start:
                raise SourceError("multi-disk archives are not supported")
            if name_len == 0 or _CD.size + name_len + extra_len + comment_len > len(cd):
                raise SourceError("malformed central directory entry")
            raw_name = cd[_CD.size : _CD.size + name_len]
            member_name = raw_name.decode("utf-8", "replace")

            f.seek(local_offset)
            lh = f.read(_LH.size)
            if len(lh) < _LH.size:
                raise SourceError("local header lies outside the file")
            try:
                fields = _LH.unpack(lh)
            except struct.error as exc:
                raise SourceError("malformed local header") from exc
            if fields[0] != LOCAL_SIG:
                raise SourceError("local header signature mismatch")
            data_offset = local_offset + _LH.size + fields[9] + fields[10]
            if data_offset + csize > size:
                raise SourceError("compressed data lies outside the file")

            _verify_payload(f, data_offset, csize, usize, crc)
    except SourceError:
        raise
    except OSError as exc:
        raise SourceError(f"cannot read file: {exc}") from exc

    return SourceMember(
        path=str(path),
        member_name=member_name,
        crc32=crc,
        csize=csize,
        usize=usize,
        mod_time=mod_time,
        mod_date=mod_date,
        data_offset=data_offset,
        file_size=size,
        file_mtime_ns=st.st_mtime_ns,
    )


def source_state_ok(path, file_size: int, file_mtime_ns: int) -> bool:
    """Cheap staleness check: the pinned source package must be untouched."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return st.st_size == file_size and st.st_mtime_ns == file_mtime_ns


# ---------------------------------------------------------------------------
# Virtual assembly
# ---------------------------------------------------------------------------

@dataclass
class OutMember:
    obs_id: str
    version: int
    arc_name: bytes      # member name inside the assembled archive
    source_path: str     # absolute path of the pinned source package
    src_data_offset: int # offset of compressed data inside the source package
    crc32: int
    csize: int
    usize: int
    mod_time: int
    mod_date: int
    lh_offset: int = 0   # offset of the local header in the assembled archive

    @property
    def data_offset(self) -> int:
        return self.lh_offset + _LH.size + len(self.arc_name)


def compute_layout(members: list[OutMember]) -> tuple[int, int, int]:
    """Assign local-header offsets; return (cd_offset, cd_size, total)."""
    off = 0
    for m in members:
        m.lh_offset = off
        off += _LH.size + len(m.arc_name) + m.csize
    cd_offset = off
    cd_size = sum(_CD.size + len(m.arc_name) for m in members)
    total = cd_offset + cd_size + EOCD_LEN
    if total >= MAX_ZIP32_TOTAL:
        raise ZipTooLarge(
            f"assembled archive would be {total} bytes; the limit is 4 GiB"
        )
    return cd_offset, cd_size, total


def local_header(m: OutMember) -> bytes:
    return _LH.pack(
        LOCAL_SIG, 20, 0, METHOD_DEFLATE,
        m.mod_time, m.mod_date, m.crc32, m.csize, m.usize,
        len(m.arc_name), 0,
    ) + m.arc_name


def central_header(m: OutMember) -> bytes:
    ext_attr = 0o100644 << 16  # regular file, 0644
    return _CD.pack(
        CENTRAL_SIG, (3 << 8) | 20, 20, 0, METHOD_DEFLATE,
        m.mod_time, m.mod_date, m.crc32, m.csize, m.usize,
        len(m.arc_name), 0, 0, 0, 0, ext_attr, m.lh_offset,
    ) + m.arc_name


def eocd(count: int, cd_size: int, cd_offset: int) -> bytes:
    return _EOCD.pack(EOCD_SIG, 0, 0, count, count, cd_size, cd_offset, 0)


def assemble_segments(members: list[OutMember]):
    """Build the ordered segment list for the virtual archive.

    Each segment is (start, length, kind, payload) where kind is:
      * "gen"  -- payload is bytes generated on the fly (headers, directory)
      * "file" -- payload is (path, offset): a raw compressed-data window
    """
    cd_offset, cd_size, total = compute_layout(members)
    segments = []
    for m in members:
        lh = local_header(m)
        segments.append((m.lh_offset, len(lh), "gen", lh))
        segments.append(
            (m.data_offset, m.csize, "file", (m.source_path, m.src_data_offset))
        )
    cd = b"".join(central_header(m) for m in members)
    segments.append((cd_offset, len(cd), "gen", cd))
    segments.append((cd_offset + len(cd), EOCD_LEN, "gen", eocd(len(members), len(cd), cd_offset)))
    return segments, total


def stream_segments(segments, start: int, end: int, chunk_size: int = 1 << 18):
    """Yield the bytes of the inclusive range [start, end].

    Only the file windows intersecting the range are opened, and only the
    intersecting part of each window is read -- a range request never pulls
    unrelated compressed data from disk.
    """
    for seg_start, seg_len, kind, payload in segments:
        if seg_len == 0:
            continue
        seg_end = seg_start + seg_len  # exclusive
        if seg_end <= start or seg_start > end:
            continue
        lo = max(start, seg_start)
        hi = min(end + 1, seg_end)
        if kind == "gen":
            yield payload[lo - seg_start : hi - seg_start]
        else:
            path, file_off = payload
            with open(path, "rb") as f:
                f.seek(file_off + (lo - seg_start))
                remaining = hi - lo
                while remaining > 0:
                    chunk = f.read(min(chunk_size, remaining))
                    if not chunk:
                        raise IOError(f"short read on {path}")
                    remaining -= len(chunk)
                    yield chunk
