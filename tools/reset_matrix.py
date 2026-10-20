"""Does an in-process guest reset hang under KVM, as qemu#4410 does under WHPX?

ANSWERED: Phase 19, Q4 (docs/PORTABILITY.md "Q4 results"; DECISIONS #78)
-------------------------------------------------------------------------
No. 60 raw trials with this tool (QEMU 10.2.1 and 11.1.1, each with a Windows
template disk, the Windows ISO and an Ubuntu control) and 30 through Kurukuru
all recovered. ``--sigstop`` proved the detector calls a real hang. KVM
refuses ``kernel-irqchip=off`` outright ("KVM does not support userspace
APIC"), so the configuration the hang was seen in under WHPX cannot be
entered here. Outputs are in /srv/kurukuru/logs/q4/<timestamp>-<cell>/:
log.txt, summary.json, command.txt and frames.

WHY THIS EXISTS (docs/WINDOWS.md; DECISIONS #55)
------------------------------------------------
Under WHPX an in-process QMP ``system_reset`` left a Windows guest at SeaBIOS's
text-mode prompt in 36/36 trials. That is 20 across four configurations and 6
at -smp 1 on a template disk, plus 10 on 11.1.1 with the disk state
unrecorded. kurukuru/reboot_watchdog.py exists to work around it. Whether that
is WHPX's defect or QEMU's decides whether the watchdog belongs on Linux.

SUBCOMMANDS
-----------
  template  Meant to build the 20's guest state: Setup's copy phase completed,
            frozen at "Windows needs to restart to continue", with
            ``-no-reboot`` making QEMU exit at the very reset that would have
            restarted it. **It did not produce the template Q4 used.** The
            consumer multi-edition ISO's Setup stops for a product key that an
            answer file without one cannot get past, and both runs stopped
            there (logs/q4/*-template). The template was then built by hand:
            this subcommand's QEMU command line, Setup clicked through with
            QMP ``send-key`` ("I don't have a product key", Pro, accept,
            Custom), no key used. Those helpers were not kept.
  run       One raw-QEMU cell: N trials, each a fresh overlay and one
            long-lived process, booted to a ready state, then ``system_reset``.
  attach    The same reset trials against an already-running VM (one launched
            through Kurukuru), by pid and QMP port. Opens QMP per operation,
            because the backend's own probes need the monitor too.
  restart   N passes of the product's restart path (POST .../restart), which
            stops the VM and starts a *fresh* process. By construction it
            cannot show this defect; it measures the product path.

WHAT COUNTS
-----------
Windows readiness is a graphical frame (more than --colours distinct colours,
reboot_watchdog.py's threshold) held for --ready-seconds; the frame may change.
After the reset, recovery needs, in order: evidence the reset happened (a QMP
RESET event, or a low-colour BIOS frame — a screendump straight after a reset
can still show the old picture, see reset_measure.py), then a graphical frame
held for --ready-seconds. The Linux control's readiness is a new ``login:`` on
the serial console. No recovery within --timeout is a HANG; a QEMU that exits
is DIED; a trial that never became testable is INCONCLUSIVE.

Per trial: wall time from reset to ready, QMP query-status afterwards, the
process's CPU and its busiest thread's CPU over the window, exit status, and
every QMP event with its time. ``--sigstop`` freezes QEMU right after the reset
(then resumes it): the detector's proof that a hang is reported as one.

RE-RUNNING Q4
-------------
The Phase 19 cells, with the 10.2.1 binary at /usr/bin and 11.1.1 built under
/srv/kurukuru/qemu-11.1.1 (swap --qemu for the other version). TEMPLATE is a
Windows disk built as described under ``template``, and BASE a *copy* of the
Ubuntu noble cloud image. Never point --base-image at a live state directory's
image.

    python tools/reset_matrix.py run --cell kvm-10.2.1-win-template --guest windows \
        --qemu /usr/bin/qemu-system-x86_64 --disk-template TEMPLATE --iso ISO --trials 10
    python tools/reset_matrix.py run --cell kvm-10.2.1-win-iso --guest windows \
        --qemu /usr/bin/qemu-system-x86_64 --iso ISO --trials 10
    python tools/reset_matrix.py run --cell kvm-10.2.1-linux --guest linux \
        --qemu /usr/bin/qemu-system-x86_64 --base-image BASE --trials 10
    python tools/reset_matrix.py run --cell proof-sigstop-windows --guest windows \
        --disk-template TEMPLATE --iso ISO --trials 1 --sigstop

Not run in Phase 19, and the one cell that would separate "WHPX's defect" from
"the userspace APIC path's": ``--accel tcg`` (TCG uses the same userspace
``apic`` device as WHPX with kernel-irqchip=off). ``--accel
kvm,kernel-irqchip=off`` fails at once on both builds; see
logs/q4/kernel-irqchip-off-refusal.txt.

FINDING YOUR OWN QEMU: NEVER ``pgrep -f`` / ``pkill -f``
--------------------------------------------------------
In Phase 19, ``pgrep -f qemu-system`` / ``pkill -f`` matched the agent's own
shell three times, because the shell's command line contained the pattern,
and killed it. Use ``ps -o pid=,args= -C qemu-system-x86_64``, which matches
the executable name, never a command line that merely mentions it. Then check
the pid against what you started: a production VM (svc) runs on the same
host.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from qmp_driver import frame_stats  # noqa: E402

#: reboot_watchdog.py's threshold: SeaBIOS text renders as ~2 colours.
COLOUR_THRESHOLD = 8
CLK_TCK = os.sysconf("SC_CLK_TCK")


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


class Log:
    def __init__(self, path: pathlib.Path) -> None:
        self._f = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def __call__(self, text: str) -> None:
        line = f"{now()}  {text}"
        with self._lock:
            self._f.write(line + "\n")
            self._f.flush()
        print(line, flush=True)


# --------------------------------------------------------------------------- #
# QMP: one short connection per command, so a frozen QEMU costs a timeout, not
# a hung harness, and an attached VM's own backend can still reach its monitor.
# --------------------------------------------------------------------------- #
class QmpError(Exception):
    pass


def qmp(port: int, execute: str, timeout: float = 5.0, **arguments) -> dict:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            s.settimeout(timeout)
            f = s.makefile("rwb")

            def read() -> dict:
                while True:
                    line = f.readline()
                    if not line:
                        raise QmpError("connection closed")
                    msg = json.loads(line)
                    if "event" not in msg:
                        return msg

            read()
            for cmd, args in (("qmp_capabilities", {}), (execute, arguments)):
                payload = {"execute": cmd}
                if args:
                    payload["arguments"] = args
                f.write((json.dumps(payload) + "\n").encode())
                f.flush()
                reply = read()
                if "error" in reply:
                    raise QmpError(f"{cmd}: {reply['error']}")
            return reply.get("return", {})
    except (OSError, ValueError) as exc:
        raise QmpError(str(exc)) from exc


def frame(port: int, path: pathlib.Path) -> tuple[int, str]:
    """(colours, hash) of the current framebuffer; raises QmpError."""
    path.unlink(missing_ok=True)
    qmp(port, "screendump", filename=str(path))
    for _ in range(30):
        if path.exists() and path.stat().st_size:
            time.sleep(0.1)
            return frame_stats(path)
        time.sleep(0.1)
    raise QmpError("screendump produced nothing")


class EventWatch(threading.Thread):
    """Holds a second QMP monitor open and records every event (raw arm only)."""

    def __init__(self, port: int) -> None:
        super().__init__(daemon=True)
        self.port, self.events, self.stop = port, [], threading.Event()

    def run(self) -> None:
        # Unbuffered reads: a timeout on a socket.makefile() reader leaves it
        # unusable, which made the first version reconnect every second and
        # drop whatever was emitted in between (it missed RESET, caught SHUTDOWN).
        while not self.stop.is_set():
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=2) as s:
                    s.settimeout(1.0)
                    s.sendall(b'{"execute":"qmp_capabilities"}\n')
                    buf = b""
                    while not self.stop.is_set():
                        try:
                            chunk = s.recv(65536)
                        except (socket.timeout, TimeoutError):
                            continue
                        if not chunk:
                            break
                        buf += chunk
                        *lines, buf = buf.split(b"\n")
                        for line in lines:
                            if not line.strip():
                                continue
                            msg = json.loads(line)
                            if "event" in msg:
                                self.events.append({"at": now(), "event": msg["event"],
                                                    "data": msg.get("data", {})})
            except OSError:
                time.sleep(0.5)

    def count(self, name: str) -> int:
        return sum(1 for e in self.events if e["event"] == name)


# --------------------------------------------------------------------------- #
# CPU: the hang's signature in this class is a vCPU thread spinning.
# --------------------------------------------------------------------------- #
def _ticks(stat_path: str) -> int | None:
    try:
        fields = pathlib.Path(stat_path).read_text().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])  # utime + stime
    except (OSError, IndexError, ValueError):
        return None


def cpu_snapshot(pid: int) -> dict:
    snap = {"t": time.monotonic(), "proc": _ticks(f"/proc/{pid}/stat"), "threads": {}}
    try:
        for tid in os.listdir(f"/proc/{pid}/task"):
            snap["threads"][tid] = _ticks(f"/proc/{pid}/task/{tid}/stat")
    except OSError:
        pass
    return snap


def cpu_between(a: dict, b: dict) -> dict:
    span = b["t"] - a["t"]
    if span <= 0 or a["proc"] is None or b["proc"] is None:
        return {"process_pct": None, "busiest_thread_pct": None}
    proc = 100.0 * (b["proc"] - a["proc"]) / CLK_TCK / span
    busiest = max((100.0 * (b["threads"][t] - a["threads"][t]) / CLK_TCK / span
                   for t in b["threads"] if a["threads"].get(t) is not None
                   and b["threads"][t] is not None), default=None)
    return {"process_pct": round(proc, 1),
            "busiest_thread_pct": None if busiest is None else round(busiest, 1)}


# --------------------------------------------------------------------------- #
# Command lines
# --------------------------------------------------------------------------- #
def raw_command(opts, disk: pathlib.Path, qmp_port: int, events_port: int,
                vnc_display: int, serial: pathlib.Path) -> list[str]:
    """reset_measure.py's command — QemuEngine's Windows output as it stood
    when the 36/36 were measured (plain q35, -cpu Westmere, AHCI, e1000e,
    std VGA, xHCI) — with -accel kvm. The Linux control runs the same machine
    with its own disk and no installer."""
    cmd = [
        opts.qemu, "-name", f"q4-{opts.cell}",
        "-machine", opts.machine, "-accel", opts.accel, "-cpu", opts.cpu,
        "-vga", "std", "-smp", str(opts.cpus), "-m", str(opts.memory),
        "-device", "ich9-ahci,id=ahci",
        "-drive", f"file={disk},if=none,id=hd0,format=qcow2",
        "-device", "ide-hd,bus=ahci.0,drive=hd0",
    ]
    if opts.guest == "windows" and opts.iso:
        cmd += ["-drive", f"file={opts.iso},if=none,id=cd0,media=cdrom,readonly=on",
                "-device", "ide-cd,bus=ahci.1,drive=cd0"]
    cmd += [
        "-boot", "order=dc,menu=on",
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-kbd,bus=xhci.0", "-device", "usb-tablet,bus=xhci.0",
        "-netdev", "user,id=net0", "-device", "e1000e,netdev=net0",
        "-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait",
        "-qmp", f"tcp:127.0.0.1:{events_port},server,nowait",
        "-vnc", f"127.0.0.1:{vnc_display}", "-display", "none",
        "-serial", f"file:{serial}",
        # The defect lives in the reset path, not in audio; keep QEMU off the
        # desktop's PipeWire (Q6) so nothing outside the guest is in the loop.
        "-audio", "none",
    ]
    return cmd


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# Readiness
# --------------------------------------------------------------------------- #
def serial_logins(serial: pathlib.Path) -> int:
    try:
        return serial.read_bytes().count(b"login:")
    except OSError:
        return 0


def wait_windows_ready(port: int, shot: pathlib.Path, cap: float, hold: float,
                       colours_at: int, alive) -> tuple[float | None, int, str]:
    """Graphical frame held for `hold` seconds. Returns (seconds, colours, hash)."""
    start, since, last = time.monotonic(), None, (0, "")
    while time.monotonic() - start < cap and alive():
        try:
            last = frame(port, shot)
        except QmpError:
            since = None
            time.sleep(0.5)
            continue
        if last[0] > colours_at:
            since = since or time.monotonic()
            if time.monotonic() - since >= hold:
                return time.monotonic() - start, *last
        else:
            since = None
        time.sleep(0.5)
    return None, *last


def trial(opts, log: Log, index: int, *, pid: int, qmp_port: int, events: EventWatch | None,
          serial: pathlib.Path | None, shot: pathlib.Path, alive, exit_code) -> dict:
    """Ready -> reset -> recovered? Shared by `run` and `attach`."""
    res: dict = {"trial": index, "verdict": None}
    if opts.guest == "windows":
        ready_s, colours, ref_hash = wait_windows_ready(
            qmp_port, shot, opts.boot_cap, opts.ready_seconds, opts.colours, alive)
        res["pre_reset"] = {"ready_s": ready_s and round(ready_s, 1), "colours": colours}
    else:
        start = time.monotonic()
        while serial_logins(serial) == 0 and time.monotonic() - start < opts.boot_cap and alive():
            time.sleep(0.5)
        ready_s = time.monotonic() - start if serial_logins(serial) else None
        res["pre_reset"] = {"ready_s": ready_s and round(ready_s, 1)}
        ref_hash = None
    if ready_s is None:
        res["verdict"] = "DIED" if not alive() else "INCONCLUSIVE"
        res["note"] = "never reached a testable state before the reset"
        res["exit_code"] = exit_code()
        log(f"[{opts.cell} #{index}] {res['verdict']}: {res['note']}")
        return res

    logins_before = serial_logins(serial) if serial else 0
    resets_before = events.count("RESET") if events else 0
    cpu0 = cpu_snapshot(pid)
    res["reset_at"] = now()
    qmp(qmp_port, "system_reset")
    t0 = time.monotonic()
    if opts.sigstop:
        os.kill(pid, signal.SIGSTOP)
        log(f"[{opts.cell} #{index}] DETECTOR PROOF: SIGSTOP sent to {pid} after the reset")
    log(f"[{opts.cell} #{index}] ready after {ready_s:.1f}s; system_reset sent")

    saw_low, saw_event_and_change, since, recovered_s, trace = False, False, None, None, []
    window_cpu = []
    last_cpu = cpu0
    while time.monotonic() - t0 < opts.timeout:
        if not alive():
            break
        if opts.guest == "windows":
            try:
                colours, digest = frame(qmp_port, shot)
            except QmpError:
                colours, digest = None, None
            if colours is not None and (not trace or trace[-1][1] != colours):
                trace.append((round(time.monotonic() - t0, 1), colours))
            if colours is not None and colours <= opts.colours:
                saw_low = True
            if (digest is not None and digest != ref_hash and events
                    and events.count("RESET") > resets_before):
                saw_event_and_change = True
            reset_seen = saw_low or saw_event_and_change
            if reset_seen and colours is not None and colours > opts.colours:
                since = since or time.monotonic()
                if time.monotonic() - since >= opts.ready_seconds:
                    recovered_s = since - t0
                    break
            elif colours is None or colours <= opts.colours:
                since = None
        else:
            if serial_logins(serial) > logins_before:
                recovered_s = time.monotonic() - t0
                break
        snap = cpu_snapshot(pid)
        if snap["t"] - last_cpu["t"] >= 5:
            window_cpu.append(cpu_between(last_cpu, snap))
            last_cpu = snap
        time.sleep(0.5)

    elapsed = time.monotonic() - t0
    res["cpu_window"] = cpu_between(cpu0, cpu_snapshot(pid))
    res["cpu_max_5s"] = max((c["busiest_thread_pct"] or 0 for c in window_cpu), default=None)
    if opts.sigstop:
        os.kill(pid, signal.SIGCONT)
    try:
        res["qmp_status_after"] = qmp(qmp_port, "query-status").get("status")
    except QmpError as exc:
        res["qmp_status_after"] = f"no answer: {exc}"
    if opts.guest == "windows":
        res["colour_trace"] = trace[:40]
        res["saw_low_colour_frame"] = saw_low
        res["saw_reset_event_and_new_frame"] = saw_event_and_change
    if events:
        res["reset_events"] = events.count("RESET") - resets_before
    if not alive():
        res["verdict"], res["exit_code"] = "DIED", exit_code()
    elif recovered_s is not None:
        res["verdict"], res["recovered_s"] = "PASS", round(recovered_s, 1)
    else:
        res["verdict"], res["waited_s"] = "HANG", round(elapsed, 1)
    if opts.sigstop:
        res["discard"] = "detector proof (SIGSTOP); not a measurement"
    log(f"[{opts.cell} #{index}] {res['verdict']}"
        + (f" in {res.get('recovered_s')}s" if res["verdict"] == "PASS" else "")
        + f"; qmp={res['qmp_status_after']}; cpu {res['cpu_window']}"
        + (f"; resets seen {res.get('reset_events')}" if events else ""))
    return res


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #
def cmd_template(opts) -> None:
    out = opts.out / f"{dt.datetime.now():%Y%m%dT%H%M%S}-template"
    out.mkdir(parents=True)
    log = Log(out / "log.txt")
    disk = pathlib.Path(opts.disk)
    if disk.exists():
        sys.exit(f"{disk} exists; refusing to overwrite a template")
    subprocess.run([opts.qemu_img, "create", "-f", "qcow2", str(disk), opts.disk_size],
                   check=True, capture_output=True)
    port, events_port = free_port(), free_port()
    opts.guest, opts.cell = "windows", "template"
    cmd = raw_command(opts, disk, port, events_port, 50, out / "serial.log")
    cmd += ["-drive", f"file={opts.unattend},if=none,id=cd1,media=cdrom,readonly=on",
            "-device", "ide-cd,bus=ahci.2,drive=cd1", "-no-reboot"]
    (out / "command.txt").write_text(" ".join(cmd))
    log(f"template: {' '.join(cmd)}")
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=(out / "qemu-stderr.txt").open("w"))
    last_hash, same_since = "", time.monotonic()
    while proc.poll() is None:
        if time.monotonic() - t0 > opts.timeout:
            log("template: time limit reached; stopping QEMU, the disk is NOT a template")
            proc.terminate()
            proc.wait(30)
            sys.exit(1)
        try:
            shot = out / "latest.ppm"
            colours, digest = frame(port, shot)
            if digest != last_hash:
                last_hash, same_since = digest, time.monotonic()
                shutil.copy(shot, out / f"frame-{int(time.monotonic() - t0):05d}s.ppm")
                log(f"template: +{time.monotonic() - t0:.0f}s new frame, {colours} colours")
            elif time.monotonic() - same_since > opts.static_limit:
                log(f"template: frame unchanged for {opts.static_limit:.0f}s — Setup is "
                    "probably waiting for input; stopping, see the last frame")
                proc.terminate()
                proc.wait(30)
                sys.exit(1)
        except QmpError:
            pass
        time.sleep(10)
    log(f"template: QEMU exited {proc.returncode} after {time.monotonic() - t0:.0f}s "
        "(-no-reboot: Setup asked for its restart)")
    (out / "result.json").write_text(json.dumps(
        {"disk": str(disk), "exit": proc.returncode,
         "seconds": round(time.monotonic() - t0)}, indent=2))


def cmd_run(opts) -> None:
    out = opts.out / f"{dt.datetime.now():%Y%m%dT%H%M%S}-{opts.cell}"
    out.mkdir(parents=True)
    log = Log(out / "log.txt")
    version = subprocess.run([opts.qemu, "--version"], capture_output=True, text=True).stdout.split("\n")[0]
    summary = {"cell": opts.cell, "guest": opts.guest, "qemu": version,
               "kernel": os.uname().release, "sigstop": opts.sigstop, "trials": []}
    log(f"cell {opts.cell}: {version}, kernel {os.uname().release}, guest {opts.guest}")
    for i in range(1, opts.trials + 1):
        disk = out / f"trial-{i}.qcow2"
        base = opts.disk_template or opts.base_image
        if base:
            subprocess.run([opts.qemu_img, "create", "-f", "qcow2", "-F", "qcow2",
                            "-b", str(base), str(disk)], check=True, capture_output=True)
        else:
            subprocess.run([opts.qemu_img, "create", "-f", "qcow2", str(disk), opts.disk_size],
                           check=True, capture_output=True)
        port, events_port = free_port(), free_port()
        serial = out / f"trial-{i}-serial.log"
        cmd = raw_command(opts, disk, port, events_port, opts.vnc_base + i % 10, serial)
        if i == 1:
            (out / "command.txt").write_text(" ".join(cmd))
        stderr = (out / f"trial-{i}-stderr.txt").open("w")
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr)
        time.sleep(2)
        events = EventWatch(events_port)
        events.start()
        try:
            res = trial(opts, log, i, pid=proc.pid, qmp_port=port, events=events,
                        serial=serial if opts.guest == "linux" else None,
                        shot=out / f"trial-{i}.ppm",
                        alive=lambda: proc.poll() is None, exit_code=lambda: proc.poll())
            res["events"] = events.events
            if opts.guest == "windows" and (out / f"trial-{i}.ppm").exists():
                # Absent when the last screendump failed (a frozen QEMU, say).
                shutil.copy(out / f"trial-{i}.ppm", out / f"trial-{i}-final.ppm")
        finally:
            events.stop.set()
            if proc.poll() is None:
                try:
                    qmp(port, "quit")
                except QmpError:
                    pass
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(10)
            stderr.close()
            disk.unlink(missing_ok=True)
        summary["trials"].append(res)
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
    verdicts = [t["verdict"] for t in summary["trials"]]
    summary["tally"] = {v: verdicts.count(v) for v in sorted(set(verdicts))}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    log(f"cell {opts.cell}: {summary['tally']}")


def cmd_attach(opts) -> None:
    out = opts.out / f"{dt.datetime.now():%Y%m%dT%H%M%S}-{opts.cell}"
    out.mkdir(parents=True)
    log = Log(out / "log.txt")
    cmdline = pathlib.Path(f"/proc/{opts.pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
    (out / "command.txt").write_text(cmdline)
    summary = {"cell": opts.cell, "pid": opts.pid, "command": cmdline, "trials": []}
    alive = lambda: pathlib.Path(f"/proc/{opts.pid}").exists()  # noqa: E731
    for i in range(1, opts.trials + 1):
        res = trial(opts, log, i, pid=opts.pid, qmp_port=opts.qmp_port, events=None,
                    serial=None, shot=out / f"trial-{i}.ppm", alive=alive,
                    exit_code=lambda: None)
        summary["trials"].append(res)
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
        if res["verdict"] != "PASS":
            log(f"[{opts.cell}] stopping after a non-PASS so the evidence stays as it is")
            break
    verdicts = [t["verdict"] for t in summary["trials"]]
    summary["tally"] = {v: verdicts.count(v) for v in sorted(set(verdicts))}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    log(f"cell {opts.cell}: {summary['tally']}")


def cmd_restart(opts) -> None:
    out = opts.out / f"{dt.datetime.now():%Y%m%dT%H%M%S}-{opts.cell}"
    out.mkdir(parents=True)
    log = Log(out / "log.txt")
    token = json.loads(pathlib.Path(opts.token_file).read_text())["token"]
    summary = {"cell": opts.cell, "instance_id": opts.instance_id, "trials": []}
    for i in range(1, opts.trials + 1):
        t0 = time.monotonic()
        req = urllib.request.Request(
            f"{opts.api}/api/instances/{opts.instance_id}/restart", method="POST",
            headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=opts.timeout) as r:
                body = json.loads(r.read())
            status = body.get("status")
        except Exception as exc:  # noqa: BLE001 - record whatever the API said
            status = f"error: {exc}"
        api_s = time.monotonic() - t0
        pid = int(pathlib.Path(opts.runtime).read_text() and
                  json.loads(pathlib.Path(opts.runtime).read_text())["pid"] or 0)
        ready_s, colours, _ = wait_windows_ready(
            opts.qmp_port, out / f"trial-{i}.ppm", opts.timeout, opts.ready_seconds,
            opts.colours, lambda: pathlib.Path(f"/proc/{pid}").exists())
        res = {"trial": i, "api_status": status, "api_s": round(api_s, 1), "new_pid": pid,
               "verdict": "PASS" if ready_s is not None else "HANG",
               "graphical_after_s": ready_s and round(ready_s, 1), "colours": colours}
        log(f"[{opts.cell} #{i}] restart -> {status} in {api_s:.1f}s, pid {pid}; {res['verdict']}")
        summary["trials"].append(res)
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
    verdicts = [t["verdict"] for t in summary["trials"]]
    summary["tally"] = {v: verdicts.count(v) for v in sorted(set(verdicts))}
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    log(f"cell {opts.cell}: {summary['tally']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--out", type=pathlib.Path, default=pathlib.Path("/srv/kurukuru/logs/q4"))
        p.add_argument("--cell", default="cell")
        p.add_argument("--guest", choices=["windows", "linux"], default="windows")
        p.add_argument("--trials", type=int, default=10)
        p.add_argument("--timeout", type=float, default=300,
                       help="per-trial hard limit after the reset (the watchdog's own 300 s)")
        p.add_argument("--boot-cap", type=float, default=300)
        p.add_argument("--ready-seconds", type=float, default=10)
        p.add_argument("--colours", type=int, default=COLOUR_THRESHOLD)
        p.add_argument("--sigstop", action="store_true",
                       help="detector proof: freeze QEMU right after the reset")

    def machine(p):
        p.add_argument("--qemu", default=shutil.which("qemu-system-x86_64"))
        p.add_argument("--qemu-img", default=shutil.which("qemu-img"))
        p.add_argument("--iso", type=pathlib.Path)
        p.add_argument("--machine", default="q35")
        p.add_argument("--accel", default="kvm")
        p.add_argument("--cpu", default="Westmere")
        p.add_argument("--cpus", type=int, default=2)
        p.add_argument("--memory", default="4096")
        p.add_argument("--disk-size", default="40G")

    t = sub.add_parser("template")
    common(t)
    machine(t)
    t.add_argument("--disk", required=True)
    t.add_argument("--unattend", required=True)
    t.add_argument("--static-limit", type=float, default=900)
    t.set_defaults(func=cmd_template, timeout=3600)

    r = sub.add_parser("run")
    common(r)
    machine(r)
    r.add_argument("--disk-template", type=pathlib.Path)
    r.add_argument("--base-image", type=pathlib.Path)
    r.add_argument("--vnc-base", type=int, default=40,
                   help="VNC display base; give concurrent cells different ones")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("attach")
    common(a)
    a.add_argument("--pid", type=int, required=True)
    a.add_argument("--qmp-port", type=int, required=True)
    a.set_defaults(func=cmd_attach)

    s = sub.add_parser("restart")
    common(s)
    s.add_argument("--api", default="http://127.0.0.1:7843")
    s.add_argument("--instance-id", required=True)
    s.add_argument("--token-file", required=True)
    s.add_argument("--runtime", required=True, help="the instance's runtime.json")
    s.add_argument("--qmp-port", type=int, required=True)
    s.set_defaults(func=cmd_restart)

    opts = ap.parse_args()
    opts.out.mkdir(parents=True, exist_ok=True)
    opts.func(opts)


if __name__ == "__main__":
    main()
