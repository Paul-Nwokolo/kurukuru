"""Check whether an ISO in the ISO directory is pristine, bootable Microsoft media.

Run this on anything newly downloaded, before spending time booting it:

    python verify_media.py                      # every .iso in the ISO directory
    python verify_media.py <path-to-iso>        # just one
    python verify_media.py --firmware uefi ...  # check the UEFI path instead

Microsoft does not reliably publish a SHA256 for these downloads, so the hash
is *reported* for you to compare against whatever the download page showed
rather than asserted against a value baked in here.

WHY THIS TOOL IS SHAPED THIS WAY (DECISIONS #34)
------------------------------------------------
An earlier version checked a volume label and ran Authenticode over four PE
files, and passed a Windows 10 ISO whose installer could not boot. The reason
it passed is the point: the engine boots **legacy BIOS**, and three of those
four files are UEFI-only, while the fourth runs after WinPE is already up. The
checked set and the executing set did not intersect at all, so a green result
carried no information about the failure.

So this version is organised around the boot path rather than around which
files happen to be signable:

  * every component is labelled with whether it EXECUTES on the selected
    firmware path, and the verdict is computed only from those that do;
  * components that cannot be signature-checked (``etfsboot.com`` is raw real
    -mode code with no PE header) are hashed and reported anyway, because
    "unverifiable" is a fact worth printing, not a reason to skip;
  * ``boot.wim`` — 446 MB of WinPE, on the executed path, and where the actual
    fault turned out to be — has its integrity table verified rather than
    merely being confirmed to exist.

Structural completeness is not functional integrity. This tool still cannot
prove an ISO boots; it can only stop reporting confidence it has not earned.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import pathlib
import struct
import subprocess
import sys
import tempfile

import pycdlib

ISO_DIR = pathlib.Path.home() / ".local-iaas" / "isos"

#: Volume identifiers Microsoft's own media uses. Note that a label outside
#: this shape is weak evidence at best: `ESD_ISO` was once read here as proof
#: of a third-party repack, but Microsoft's own Media Creation Tool emits that
#: label too. It describes how an ISO was assembled, not by whom.
GENUINE_LABEL_HINTS = (
    "CCCOMA_",   # Windows 10/11 client, e.g. CCCOMA_X64FRE_EN-US_DV9
    "CENA_",     # some client SKUs
    "SSS_",      # Server evaluation, e.g. SSS_X64FREE_EN-US_DV9
    "SW_DVD",    # VLSC / volume licensing
    "J_CCSA_",   # Windows 11 variants
)

#: The boot chain, in execution order, per firmware. `signable` is False where
#: the component has no PE header and therefore cannot carry an Authenticode
#: signature at all — that is a property of the format, not a defect.
BOOT_PATHS = {
    "bios": [
        ("/boot/etfsboot.com", "El Torito boot sector", False),
        ("/bootmgr", "BIOS boot manager", False),
        ("/sources/boot.wim", "WinPE image", False),
        ("/sources/setup.exe", "Setup, inside WinPE", True),
    ],
    "uefi": [
        ("/efi/boot/bootx64.efi", "UEFI boot loader", True),
        ("/efi/microsoft/boot/cdboot.efi", "El Torito UEFI loader", True),
        ("/bootmgr.efi", "UEFI boot manager", True),
        ("/sources/boot.wim", "WinPE image", False),
        ("/sources/setup.exe", "Setup, inside WinPE", True),
    ],
}

#: Everything else worth reporting, none of which executes on either path
#: before Setup. Kept visible so their passing cannot be mistaken for the
#: media being bootable.
OFF_PATH = [
    ("/setup.exe", "root stub, runs post-WinPE", True),
    ("/efi/microsoft/boot/efisys.bin", "UEFI El Torito image", False),
    ("/sources/install.wim", "the OS payload", False),
]


# --------------------------------------------------------------------------
# WIM integrity table
# --------------------------------------------------------------------------

def _reshdr(buf: bytes, off: int) -> tuple[int, int, int]:
    """Parse a WIM resource header: 7-byte size, flags, offset, original size."""
    size = int.from_bytes(buf[off : off + 7], "little")
    flags = buf[off + 7]
    offset, original = struct.unpack_from("<QQ", buf, off + 8)
    return size, offset, original


def verify_wim_integrity(path: pathlib.Path) -> tuple[bool | None, str]:
    """Verify a WIM's integrity table.

    A WIM optionally carries a table of SHA-1 digests, one per fixed-size chunk,
    covering everything from the end of the header to the end of the lookup
    table. Returns (ok, detail); ok is None when the WIM has no integrity table,
    which is not a failure — plenty of genuine media ships without one.
    """
    with path.open("rb") as fh:
        header = fh.read(208)
        if len(header) < 208 or header[:8] != b"MSWIM\0\0\0":
            return False, "not a WIM (bad magic)"

        lookup_size, lookup_off, _ = _reshdr(header, 48)       # offset_table
        integ_size, integ_off, _ = _reshdr(header, 124)        # integrity

        if integ_off == 0 or integ_size == 0:
            return None, "no integrity table present"

        fh.seek(integ_off)
        head = fh.read(12)
        if len(head) < 12:
            return False, "integrity table truncated"
        _tbl_size, num_entries, chunk_size = struct.unpack("<III", head)
        digests = fh.read(num_entries * 20)
        if len(digests) < num_entries * 20:
            return False, "integrity digest list truncated"

        # The covered region runs from the end of the header to the end of the
        # lookup table.
        start, end = 208, lookup_off + lookup_size
        expected_chunks = (end - start + chunk_size - 1) // chunk_size
        if expected_chunks != num_entries:
            return False, (f"table claims {num_entries} chunks, layout implies "
                           f"{expected_chunks}")

        fh.seek(start)
        bad = 0
        for i in range(num_entries):
            want = digests[i * 20 : (i + 1) * 20]
            todo = min(chunk_size, end - (start + i * chunk_size))
            got = hashlib.sha1()
            remaining = todo
            while remaining:
                block = fh.read(min(1 << 20, remaining))
                if not block:
                    break
                got.update(block)
                remaining -= len(block)
            if got.digest() != want:
                bad += 1
        if bad:
            return False, f"{bad}/{num_entries} chunks FAILED sha1"
        mb = (end - start) / 1e6
        return True, f"{num_entries} chunks over {mb:,.0f} MB all match"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def authenticode(path: pathlib.Path) -> tuple[str, str]:
    ps = (
        f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
        "Write-Output $s.Status; "
        "if ($s.SignerCertificate) { Write-Output $s.SignerCertificate.Subject } "
        "else { Write-Output '(no signer)' }"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=180,
    ).stdout.strip().splitlines()
    status = out[0].strip() if out else "?"
    signer = out[1].strip() if len(out) > 1 else "(no signer)"
    for part in signer.split(","):
        if part.strip().upper().startswith("CN="):
            signer = part.strip()[3:]
            break
    return status, signer


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def extract(facade, name: str, dest: pathlib.Path) -> bool:
    try:
        with dest.open("wb") as out:
            facade.get_file_from_iso_fp(out, name)
        return True
    except Exception:
        return False


def facade_for(iso: pycdlib.PyCdlib):
    for getter in ("get_udf_facade", "get_joliet_facade", "get_iso9660_facade"):
        try:
            return getattr(iso, getter)()
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------

def verify(path: pathlib.Path, firmware: str, do_hash: bool) -> bool:
    print(f"\n=== {path.name} ===")
    print(f"  size     : {path.stat().st_size:,} bytes")
    print(f"  firmware : {firmware}  <- components are judged against THIS path")

    iso = pycdlib.PyCdlib()
    try:
        iso.open(str(path))
    except Exception as exc:
        print(f"  NOT A READABLE ISO: {exc}")
        return False

    label = iso.pvds[0].volume_identifier.decode("utf-8", "replace").strip()
    genuine_label = any(label.upper().startswith(h) for h in GENUINE_LABEL_HINTS)
    print(f"  label    : {label}"
          f"{'' if genuine_label else '   <-- not a Microsoft-shaped label'}")

    facade = facade_for(iso)
    if facade is None:
        print("  (no readable filesystem facade — not Windows media)")
        iso.close()
        return False

    on_path = BOOT_PATHS[firmware]
    on_path_names = {n for n, _, _ in on_path}
    rows: list[tuple[str, bool, str]] = []
    path_ok = True

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = pathlib.Path(tmp)

        def check(name: str, role: str, signable: bool, executes: bool) -> None:
            nonlocal path_ok
            dest = tmpd / name.replace("/", "_")
            if not extract(facade, name, dest):
                rows.append((name, executes, "ABSENT"))
                if executes:
                    path_ok = False
                return

            size = dest.stat().st_size
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()[:16] \
                if size < 64 * 1024 * 1024 else sha256(dest)[:16]
            bits = [f"{size:,} B", f"sha256:{digest}…"]

            if name.endswith(".wim"):
                ok, detail = verify_wim_integrity(dest)
                if ok is False:
                    bits.append(f"INTEGRITY FAILED — {detail}")
                    if executes:
                        path_ok = False
                elif ok is None:
                    bits.append(f"integrity: {detail}")
                else:
                    bits.append(f"integrity OK — {detail}")
            elif signable:
                status, signer = authenticode(dest)
                good = status == "Valid" and "Microsoft" in signer
                bits.append(f"{status}/{signer}")
                if not good and executes:
                    path_ok = False
            else:
                bits.append("unsignable (no PE header) — hash only")

            rows.append((f"{name}  [{role}]", executes, "  ".join(bits)))

        print("\n  ON THE BOOT PATH — these execute, and decide the verdict")
        for name, role, signable in on_path:
            check(name, role, signable, executes=True)
        for name, executes, detail in rows:
            print(f"    {'RUN ' if executes else '    '}{name}\n         {detail}")

        rows.clear()
        print("\n  OFF THE BOOT PATH — reported, but proves nothing about booting")
        for name, role, signable in OFF_PATH:
            if name in on_path_names:
                continue
            check(name, role, signable, executes=False)
        for name, executes, detail in rows:
            print(f"    --- {name}\n         {detail}")

    iso.close()

    if do_hash:
        print("\n  sha256   : computing…", flush=True)
        print(f"  sha256   : {sha256(path)}")
        print("             ^ compare against the download page if it showed one.")

    print()
    if path_ok:
        print(f"  VERDICT  : every component on the {firmware} boot path is present,")
        print("             hashes cleanly and — where signable — is validly signed.")
        print("             This does NOT mean it boots. It means nothing checkable")
        print("             on the executed path is wrong.")
    else:
        print(f"  VERDICT  : SOMETHING ON THE {firmware.upper()} BOOT PATH IS WRONG — see RUN rows.")
    if not genuine_label:
        print("             (label is unusual; weak signal, see the note in this file)")
    return path_ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("isos", nargs="*", type=pathlib.Path)
    ap.add_argument("--firmware", choices=("bios", "uefi"), default="bios",
                    help="which boot path to judge against (default: bios, "
                         "which is what the engine uses)")
    ap.add_argument("--no-hash", action="store_true",
                    help="skip the whole-ISO SHA256, which is slow on 8 GB media")
    args = ap.parse_args()

    targets = args.isos or sorted(ISO_DIR.glob("*.iso"))
    if not targets:
        print(f"no ISOs found in {ISO_DIR}")
        return
    for target in targets:
        verify(target, args.firmware, not args.no_hash)


if __name__ == "__main__":
    main()
