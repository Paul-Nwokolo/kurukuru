"""Alternating-runs harness for guest-boot measurements. Reports a distribution.

WHY THIS EXISTS (DECISIONS #40)
--------------------------------
This host is bimodal for Windows guests: the *same* QEMU command line either
reaches the Setup language screen in about 21 seconds or pegs both vCPUs and
reaches nothing at all. There is no middle outcome. Phase 13 lost three separate
hypotheses to that — `-vnc`, host free RAM, and `hostfwd`/reconciler polling were
each "confirmed" by a run that could not have distinguished them, and each was
later unconfirmed. One configuration failed three times consecutively, which is
exactly the shape of a deterministic defect; two single-variable variants of it
then passed on the first try.

So this tool refuses to do the thing that caused all that. It:

* runs arms **alternating** (A B A B ...), never all of A then all of B, so a
  drifting host (page cache, background load) cannot land entirely on one arm;
* reports a **distribution** — every run, plus a per-arm summary — and never a
  verdict;
* **refuses to compare** arms with fewer than MIN_RUNS runs each, and says so
  rather than printing a weaker conclusion;
* records host free memory per run, because that was one of the confounds.

It deliberately does not decide anything. A human reads the distribution.

USAGE
-----
    python ab_measure.py --runs 3 \\
        --arm "novnc:" \\
        --arm "vnc:-vnc,127.0.0.1:30" \\
        --iso ~/.local-iaas/isos/Windows10.iso

Arm syntax is ``name:arg,arg,arg`` — a comma-separated argv fragment appended to
the base command, empty for the control arm. Commas rather than spaces so a
single shell word carries the whole arm.

The milestone is "the framebuffer shows a rich GUI screen": more than
``--colours`` distinct colours and not predominantly black. That distinguishes a
Windows Setup or OOBE screen from a boot logo (~30 colours on black) without
needing to read the screen. Adjust for other guests.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time

#: A comparison needs at least this many runs per arm. Three is the minimum that
#: lets a failure be *bracketed* by successes of the other arm (A-B-A), which is
#: what finally settled the one Phase 13 result that survived scrutiny.
MIN_RUNS = 3


# --------------------------------------------------------------------------- #
# QMP
# --------------------------------------------------------------------------- #
class Qmp:
    def __init__(self, port: int, timeout: float = 10.0) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.io = self.sock.makefile("rwb")
        self._read()
        self.cmd("qmp_capabilities")

    def _read(self) -> dict:
        while True:
            line = self.io.readline()
            if not line:
                raise RuntimeError("QMP closed")
            msg = json.loads(line)
            if "event" not in msg:
                return msg

    def cmd(self, execute: str, **args) -> dict:
        payload = {"execute": execute}
        if args:
            payload["arguments"] = args
        self.io.write((json.dumps(payload) + "\n").encode())
        self.io.flush()
        reply = self._read()
        if "error" in reply:
            # Silent QMP errors are indistinguishable from a guest ignoring
            # input; `hold_time` vs `hold-time` cost a long detour once.
            raise RuntimeError(f"QMP {execute} failed: {reply['error']}")
        return reply

    def screendump(self, path: pathlib.Path) -> pathlib.Path:
        if path.exists():
            path.unlink()
        self.cmd("screendump", filename=str(path))
        for _ in range(50):
            if path.exists() and path.stat().st_size:
                time.sleep(0.3)
                return path
            time.sleep(0.2)
        raise RuntimeError("screendump produced nothing")

    def close(self) -> None:
        try:
            self.io.close()
            self.sock.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Framebuffer milestone
# --------------------------------------------------------------------------- #
def read_ppm(path: pathlib.Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    if not data.startswith(b"P6"):
        raise ValueError("not a P6 ppm")
    fields, idx = [], 2
    while len(fields) < 3:
        while idx < len(data) and data[idx:idx + 1].isspace():
            idx += 1
        if data[idx:idx + 1] == b"#":
            while data[idx:idx + 1] not in (b"\n", b""):
                idx += 1
            continue
        start = idx
        while idx < len(data) and not data[idx:idx + 1].isspace():
            idx += 1
        fields.append(int(data[start:idx]))
    return fields[0], fields[1], data[idx + 1:]


def frame_stats(path: pathlib.Path) -> tuple[int, str]:
    """(distinct colours, dominant colour hex)."""
    _w, _h, px = read_ppm(path)
    seen: dict[bytes, int] = {}
    for i in range(0, len(px) - 2, 3):
        c = px[i:i + 3]
        seen[c] = seen.get(c, 0) + 1
    if not seen:
        return 0, "000000"
    dominant = max(seen.items(), key=lambda kv: kv[1])[0]
    return len(seen), dominant.hex()


def free_mem_gb() -> float | None:
    """Host free physical memory, recorded per run because it was a confound."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
        return round(int(out) / 1024 / 1024, 2)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# One run
# --------------------------------------------------------------------------- #
def run_once(arm_name: str, extra: list[str], opts, workdir: pathlib.Path,
             index: int) -> dict:
    disk = workdir / f"{arm_name}-{index}.qcow2"
    if disk.exists():
        disk.unlink()
    subprocess.run([opts.qemu_img, "create", "-f", "qcow2", str(disk), opts.disk],
                   check=True, capture_output=True)

    port = opts.qmp_port + index
    cmd = [
        opts.qemu,
        "-machine", opts.machine,
        "-accel", opts.accel,
        "-cpu", opts.cpu,
        "-smp", str(opts.cpus),
        "-m", opts.memory,
        "-device", "ich9-ahci,id=ahci",
        "-drive", f"file={disk},if=none,id=hd0,format=qcow2",
        "-device", "ide-hd,bus=ahci.0,drive=hd0",
        "-drive", f"file={opts.iso},if=none,id=cd0,media=cdrom,readonly=on",
        "-device", "ide-cd,bus=ahci.1,drive=cd0",
        "-boot", "order=dc,menu=on",
        "-netdev", "user,id=n0",
        "-device", "e1000e,netdev=n0",
        "-vga", "std",
        "-qmp", f"tcp:127.0.0.1:{port},server,nowait",
        "-display", "none",
    ] + extra

    free_before = free_mem_gb()
    started = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    reached: float | None = None
    last = (0, "?")
    try:
        time.sleep(5)
        qmp = Qmp(port)
        try:
            shot = workdir / f"{arm_name}-{index}.ppm"
            while time.time() - started < opts.cap:
                colours, dominant = frame_stats(qmp.screendump(shot))
                last = (colours, dominant)
                if colours > opts.colours and dominant != "000000":
                    reached = time.time() - started
                    break
                time.sleep(opts.poll)
        finally:
            try:
                qmp.cmd("quit")
            except Exception:
                pass
            qmp.close()
    except Exception as exc:            # a dead VM is a data point, not a crash
        print(f"    (run error: {exc})", flush=True)
    finally:
        time.sleep(1)
        if proc.poll() is None:
            proc.kill()
        disk.unlink(missing_ok=True)

    return {
        "arm": arm_name,
        "index": index,
        "reached_s": round(reached, 1) if reached is not None else None,
        "last_colours": last[0],
        "last_dominant": last[1],
        "free_gb_before": free_before,
    }


# --------------------------------------------------------------------------- #
# Reporting — a distribution, never a verdict
# --------------------------------------------------------------------------- #
def summarise(results: list[dict], arms: list[str], min_runs: int = MIN_RUNS) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append("PER-RUN, in execution order (alternating by construction)")
    lines.append(f"  {'#':>3}  {'arm':<12} {'result':>12}  {'lastframe':>12}  free GB")
    for i, r in enumerate(results, 1):
        got = f"{r['reached_s']}s" if r["reached_s"] is not None else "NOT REACHED"
        frame = f"{r['last_colours']}c #{r['last_dominant']}"
        free = "?" if r["free_gb_before"] is None else f"{r['free_gb_before']:.2f}"
        lines.append(f"  {i:>3}  {r['arm']:<12} {got:>12}  {frame:>12}  {free}")

    lines.append("")
    lines.append("PER-ARM DISTRIBUTION")
    counts: dict[str, list[dict]] = {a: [x for x in results if x["arm"] == a] for a in arms}
    for arm, runs in counts.items():
        hits = [r["reached_s"] for r in runs if r["reached_s"] is not None]
        n = len(runs)
        line = f"  {arm:<12} n={n}  reached {len(hits)}/{n}"
        if hits:
            line += (f"  min={min(hits)}s median={statistics.median(hits)}s"
                     f" max={max(hits)}s")
        lines.append(line)

    lines.append("")
    thin = [a for a, runs in counts.items() if len(runs) < min_runs]
    if thin:
        lines.append(f"NO COMPARISON OFFERED. Arms with fewer than {min_runs} runs: "
                     f"{', '.join(thin)}.")
        lines.append("  This host is bimodal (DECISIONS #40): identical command lines")
        lines.append("  either succeed quickly or not at all. Below three runs per arm a")
        lines.append("  difference cannot be told from a streak, and three hypotheses in")
        lines.append("  Phase 13 died to exactly that. Re-run with --runs 3 or more.")
    else:
        lines.append("READ THE DISTRIBUTION ABOVE. This tool does not conclude.")
        lines.append("  A difference is only meaningful if each arm's outcome is")
        lines.append("  consistent across its runs AND the failing arm is bracketed by")
        lines.append("  successes of the other one in execution order. If any arm is")
        lines.append("  mixed, you are looking at the host's bimodality, not your")
        lines.append("  variable.")
    return "\n".join(lines)


def parse_arm(spec: str) -> tuple[str, list[str]]:
    name, _, rest = spec.partition(":")
    extra = [piece for piece in rest.split(",") if piece]
    return name.strip(), extra


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--arm", action="append", required=True,
                    help="name:arg,arg — repeatable; empty args for a control arm")
    ap.add_argument("--runs", type=int, default=MIN_RUNS,
                    help=f"runs per arm (default {MIN_RUNS})")
    ap.add_argument("--iso", required=True, type=pathlib.Path)
    ap.add_argument("--memory", default="2048")
    ap.add_argument("--cpus", type=int, default=2)
    ap.add_argument("--disk", default="40G")
    ap.add_argument("--cap", type=int, default=240, help="seconds per run")
    ap.add_argument("--poll", type=int, default=15)
    ap.add_argument("--colours", type=int, default=300,
                    help="distinct colours above which a frame counts as a GUI screen")
    ap.add_argument("--machine", default="q35")
    ap.add_argument("--accel", default="whpx,kernel-irqchip=off")
    ap.add_argument("--cpu", default="Westmere")
    ap.add_argument("--qmp-port", type=int, default=4700)
    ap.add_argument("--qemu", default=shutil.which("qemu-system-x86_64")
                    or r"C:\Program Files\qemu\qemu-system-x86_64.exe")
    ap.add_argument("--qemu-img", default=shutil.which("qemu-img")
                    or r"C:\Program Files\qemu\qemu-img.exe")
    ap.add_argument("--json", type=pathlib.Path, help="also write raw results here")
    opts = ap.parse_args()

    arms = [parse_arm(a) for a in opts.arm]
    if len(arms) < 2:
        ap.error("give at least two --arm values; a single arm cannot be compared")

    print(f"{len(arms)} arms x {opts.runs} runs, alternating, cap {opts.cap}s each")
    for name, extra in arms:
        print(f"  {name:<12} extra: {' '.join(extra) if extra else '(none)'}")
    if opts.runs < MIN_RUNS:
        print(f"\n  WARNING: --runs {opts.runs} is below {MIN_RUNS}; no comparison "
              f"will be offered.")

    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="ab_measure_") as tmp:
        workdir = pathlib.Path(tmp)
        for cycle in range(opts.runs):
            for name, extra in arms:            # alternating, not batched
                idx = cycle * len(arms) + arms.index((name, extra))
                print(f"\n[cycle {cycle + 1}/{opts.runs}] {name}", flush=True)
                r = run_once(name, extra, opts, workdir, idx)
                got = f"{r['reached_s']}s" if r["reached_s"] is not None else "NOT REACHED"
                print(f"    -> {got}", flush=True)
                results.append(r)

    print(summarise(results, [n for n, _ in arms]))
    if opts.json:
        opts.json.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nraw results: {opts.json}")


if __name__ == "__main__":
    main()
