"""Tests for verify_media.verify_wim_integrity.

Microsoft's retail install media ships WIMs with NO integrity table — measured
on Windows 10 19041 (both the good and the bad ISO) and on Server 2025 26100,
all of which report an integrity resource of (0,0,0,0). Verified against a real
header rather than assumed: the lookup-table and XML offsets chain exactly to
the file size, so the parser is reading the structure correctly and the absence
is genuine.

That means the checking code would never execute on any real input here, and
untested verification code is exactly the failure mode DECISIONS #34 is about.
These tests synthesise WIMs with known-good and known-corrupt integrity tables
so the verifier is exercised in both directions.

    python -m pytest tools/test_verify_media.py -q
"""
from __future__ import annotations

import hashlib
import pathlib
import struct
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from verify_media import verify_wim_integrity

CHUNK = 4096
HEADER = 208


def reshdr(size: int, offset: int, original: int, flags: int = 0) -> bytes:
    return size.to_bytes(7, "little") + bytes([flags]) + struct.pack("<QQ", offset, original)


def build_wim(body_len: int, corrupt_chunk: int | None = None,
              omit_integrity: bool = False, wrong_count: bool = False) -> bytes:
    """Assemble a minimal but structurally honest WIM.

    Layout: header | body (the integrity-covered region) | integrity table. The
    covered region runs from HEADER to lookup_off+lookup_size, so the body is
    made to end exactly at the end of the 'lookup table'.
    """
    body = bytes((i * 7 + 3) & 0xFF for i in range(body_len))
    lookup_size = 64
    lookup_off = HEADER + body_len - lookup_size
    covered = body

    n = (len(covered) + CHUNK - 1) // CHUNK
    digests = []
    for i in range(n):
        blob = covered[i * CHUNK:(i + 1) * CHUNK]
        if corrupt_chunk is not None and i == corrupt_chunk:
            digests.append(hashlib.sha1(blob + b"x").digest())   # wrong on purpose
        else:
            digests.append(hashlib.sha1(blob).digest())
    claimed = n + 1 if wrong_count else n
    if wrong_count:
        # Pad to the claimed length, otherwise the list simply reads as
        # truncated and the count check never gets a chance to fire.
        digests.append(bytes(20))
    table = struct.pack("<III", 12 + claimed * 20, claimed, CHUNK) + b"".join(digests)

    integ_off = HEADER + body_len
    header = bytearray(HEADER)
    header[0:8] = b"MSWIM\0\0\0"
    struct.pack_into("<I", header, 8, HEADER)
    struct.pack_into("<I", header, 12, 0x00010D00)
    struct.pack_into("<I", header, 16, 0)
    struct.pack_into("<I", header, 20, 32768)
    struct.pack_into("<HH", header, 40, 1, 1)
    struct.pack_into("<I", header, 44, 1)
    header[48:72] = reshdr(lookup_size, lookup_off, lookup_size)
    header[72:96] = reshdr(0, 0, 0)
    header[96:120] = reshdr(0, 0, 0)
    struct.pack_into("<I", header, 120, 1)
    header[124:148] = (reshdr(0, 0, 0) if omit_integrity
                       else reshdr(len(table), integ_off, len(table)))
    return bytes(header) + body + table


def check(tmp_path: pathlib.Path, blob: bytes):
    p = tmp_path / "t.wim"
    p.write_bytes(blob)
    return verify_wim_integrity(p)


def test_a_valid_integrity_table_passes(tmp_path):
    ok, detail = check(tmp_path, build_wim(CHUNK * 10))
    assert ok is True
    assert "all match" in detail


def test_a_body_that_is_not_a_chunk_multiple_still_passes(tmp_path):
    """The last chunk is short; hashing it as a full chunk would fail everything."""
    ok, detail = check(tmp_path, build_wim(CHUNK * 6 + 1234))
    assert ok is True
    assert "all match" in detail


def test_one_corrupted_chunk_is_caught(tmp_path):
    ok, detail = check(tmp_path, build_wim(CHUNK * 10, corrupt_chunk=4))
    assert ok is False
    assert "FAILED sha1" in detail


def test_a_chunk_count_mismatch_is_caught(tmp_path):
    ok, detail = check(tmp_path, build_wim(CHUNK * 10, wrong_count=True))
    assert ok is False
    assert "layout implies" in detail


def test_an_absent_integrity_table_is_not_a_failure(tmp_path):
    """None, not False. Every piece of genuine Microsoft media tested here ships
    without one, so treating absence as corruption would fail all of it."""
    ok, detail = check(tmp_path, build_wim(CHUNK * 4, omit_integrity=True))
    assert ok is None
    assert "no integrity table" in detail


def test_a_non_wim_is_rejected(tmp_path):
    ok, detail = check(tmp_path, b"NOTAWIM!" + bytes(400))
    assert ok is False
    assert "bad magic" in detail
