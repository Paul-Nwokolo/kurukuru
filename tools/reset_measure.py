"""Reproduces the upstream QEMU/WHPX `system_reset` guest-reboot hang.

WHY THIS EXISTS (DECISIONS #55, docs/WINDOWS.md)
-------------------------------------------------
QEMU/WHPX's `system_reset` — the mechanism a guest-triggered reboot actually
uses — deterministically fails to bring a guest back up: the guest is left
stuck at SeaBIOS's boot-device-probe prompt (a static, ~2-colour text-mode
framebuffer) forever, while the QEMU process stays alive and its QMP socket
stays fully responsive. A *fresh* QEMU process against the identical disk
state always boots cleanly instead. Measured at 36/36 across every chipset,
RTC/HPET, Hyper-V-enlightenment and CD-ejection combination tried, on two
separate QEMU builds (this project's dev snapshot and the officially tagged
QEMU 11.1.1 release). Filed upstream as
https://gitlab.com/qemu-project/qemu/-/issues/4410; kurukuru's `reboot_watchdog.py` is the workaround for it. This is the
tool that produced that reproduction — kept here, not in host-local scratch,
because a filed upstream report should not depend on one machine still having
the right one-off script lying around.

**No completed Windows install is required.** The hang lives at the firmware
level, not in guest OS state: this tool boots once from a plain disk overlay
(no OS installed) and a Windows install ISO on the CD-ROM path, waits for the
guest to reach a stable *graphical* screen (Setup's language selection —
proof the guest is well past SeaBIOS), sends `system_reset` directly over
QMP, and watches whether the guest ever produces another graphical frame
within the timeout. It never drives Setup itself, so it takes well under a
minute per trial instead of the ~9-minute copy phase a full install needs.

METHODOLOGY
-----------
One long-lived QEMU process, boots once, then receives an in-process QMP
`system_reset` — not a fresh process per trial, which is a different,
non-failing scenario (see docs/WINDOWS.md, "Confirmed: the reboot hang is
`system_reset` itself" — that methodological error cost a false "10/10 clean"
result once). Each trial:

  1. build a throwaway disk overlay (or copy `--disk-template` if given)
  2. boot it against `--iso`, poll screendumps until the framebuffer holds a
     stable, above-threshold-colour frame for `--stable-seconds` (this is the
     guest reaching Setup's language screen, not booting an OS)
  3. send `system_reset` over the same QMP connection
  4. poll for up to `--stuck-seconds`; "survived" means a new above-threshold
     frame appeared, "hung" means it never did
  5. if hung and `--confirm-fresh` (default on): quit that process, launch a
     *fresh* one against the identical disk file, and confirm it reaches the
     same stable stage — the control that shows the disk isn't the problem

USAGE
-----
    python reset_measure.py --iso D:/ISOs/Windows10.iso --trials 5

Verified by hand before being written as a tool: 1/1 trial run against
QEMU 11.1.0 (v11.1.0-12130-ge470268ff4), `-machine q35 -accel
whpx,kernel-irqchip=off -cpu Westmere`, on an Intel Core i5-9300H host —
pre-reset frame stable at 65 colours for 48s, post-reset frame stuck at 2
colours (alternating hash = a blinking cursor, not progress) for 180s+ with
zero recovery, and a fresh process against the same disk reached the
identical 65-colour frame in under 25s. Re-run through this script itself
before trusting a number from it on a different host or QEMU build.
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

from qmp_driver import Qmp, frame_stats

#: Distinct colours above which a frame counts as "graphical" rather than
#: SeaBIOS's text mode. Reuses `reboot_watchdog.py`'s threshold (8) rather than
#: inventing a new number: SeaBIOS measured at ~2 colours everywhere in this
#: project's work, every graphical stage at 12+.
COLOUR_THRESHOLD = 8


def build_command(opts, disk: pathlib.Path, qmp_port: int, vnc_display: int,
                   ssh_port: int) -> list[str]:
    """The exact argv `QemuEngine.build_launch_command` emits for a Windows
    guest today (plain q35, no HPET/RTC/enlightenment changes — decision 54's
    copy-phase fix is not carried into the engine yet), against a throwaway
    disk and the given ISO."""
    return [
        opts.qemu,
        "-name", "reset-measure",
        "-machine", opts.machine,
        "-accel", opts.accel,
        "-cpu", opts.cpu,
        "-vga", "std",
        "-smp", str(opts.cpus),
        "-m", str(opts.memory),
        "-device", "ich9-ahci,id=ahci",
        "-drive", f"file={disk},if=none,id=hd0,format=qcow2",
        "-device", "ide-hd,bus=ahci.0,drive=hd0",
        "-drive", f"file={opts.iso},if=none,id=cd0,media=cdrom,readonly=on",
        "-device", "ide-cd,bus=ahci.1,drive=cd0",
        "-boot", "order=dc,menu=on",
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-kbd,bus=xhci.0",
        "-device", "usb-tablet,bus=xhci.0",
        "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
        "-device", "e1000e,netdev=net0",
        "-qmp", f"tcp:127.0.0.1:{qmp_port},server,nowait",
        "-vnc", f"127.0.0.1:{vnc_display}",
        "-display", "none",
    ]


def wait_for_stable_frame(qmp: Qmp, shot: pathlib.Path, cap: float,
                           stable_seconds: float, poll: float,
                           colour_threshold: int) -> tuple[float | None, int, str]:
    """Poll until a graphical frame (colours > threshold) repeats, unchanged,
    for `stable_seconds`. Returns (seconds_to_reach or None, last_colours,
    last_hash)."""
    started = time.time()
    streak_start: float | None = None
    last_hash = ""
    last_colours = 0
    while time.time() - started < cap:
        last_colours, last_hash = frame_stats(qmp.screendump(shot))
        if last_colours > colour_threshold:
            if streak_start is None:
                streak_start = time.time()
                streak_hash = last_hash
            elif last_hash != streak_hash:
                streak_start = time.time()
                streak_hash = last_hash
            elif time.time() - streak_start >= stable_seconds:
                return time.time() - started, last_colours, last_hash
        else:
            streak_start = None
        time.sleep(poll)
    return None, last_colours, last_hash


def wait_for_recovery(qmp: Qmp, shot: pathlib.Path, cap: float, poll: float,
                       colour_threshold: int, stable_seconds: float,
                       reference_hash: str) -> tuple[bool, float, int, str]:
    """Poll after `system_reset` for up to `cap` seconds. Returns
    (recovered, seconds_waited, last_colours, last_hash).

    A screendump taken right after `system_reset` can still show the
    guest's pre-reset framebuffer content — the reset does not clear video
    memory instantaneously, and an early version of this function took that
    stale frame at face value and reported "recovered" after well under a
    second, on a build the project has otherwise measured hanging 36/36
    times. So recovery requires two things in order: first a frame that
    differs from `reference_hash` (proof the reset actually touched the
    display — SeaBIOS's own screen if nothing else), and only after that a
    graphical (colours > threshold) frame that then holds unchanged for
    `stable_seconds`, the same stability bar the pre-reset screen had to
    clear. A frame that never moves off `reference_hash` for the whole
    window is exactly what a genuine hang looks like and correctly reports
    as not recovered.
    """
    started = time.time()
    last_colours = 0
    last_hash = ""
    seen_transition = False
    streak_start: float | None = None
    streak_hash = ""
    while time.time() - started < cap:
        last_colours, last_hash = frame_stats(qmp.screendump(shot))
        if not seen_transition:
            if last_hash != reference_hash:
                seen_transition = True
            else:
                time.sleep(poll)
                continue
        if last_colours > colour_threshold:
            if streak_start is None or last_hash != streak_hash:
                streak_start = time.time()
                streak_hash = last_hash
            elif time.time() - streak_start >= stable_seconds:
                return True, time.time() - started, last_colours, last_hash
        else:
            streak_start = None
        time.sleep(poll)
    return False, cap, last_colours, last_hash


def run_trial(opts, index: int, workdir: pathlib.Path) -> dict:
    disk = workdir / f"trial-{index}.qcow2"
    if opts.disk_template:
        shutil.copy(opts.disk_template, disk)
    else:
        subprocess.run(
            [opts.qemu_img, "create", "-f", "qcow2", str(disk), opts.disk_size],
            check=True, capture_output=True,
        )

    qmp_port = opts.qmp_port_base + index * 2
    ssh_port = opts.ssh_port_base + index
    vnc_display = index
    cmd = build_command(opts, disk, qmp_port, vnc_display, ssh_port)

    result = {
        "trial": index, "booted": False, "boot_s": None,
        "survived_reset": None, "stuck_s": None,
        "last_colours": None, "fresh_process_recovers": None,
    }

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    shot = workdir / f"trial-{index}.ppm"
    try:
        time.sleep(5)  # let QMP come up before the first connection attempt
        qmp = Qmp(qmp_port)
        try:
            boot_s, colours, reference_hash = wait_for_stable_frame(
                qmp, shot, opts.boot_cap, opts.stable_seconds, opts.poll,
                opts.colours,
            )
            if boot_s is None:
                print(f"    [trial {index}] never reached a stable graphical "
                      f"frame in {opts.boot_cap}s (last: {colours} colours) - "
                      f"cannot test the reset", flush=True)
                result["last_colours"] = colours
                return result
            result["booted"] = True
            result["boot_s"] = round(boot_s, 1)
            print(f"    [trial {index}] reached Setup's screen in {boot_s:.1f}s "
                  f"({colours} colours); sending system_reset", flush=True)

            qmp.cmd("system_reset")
            recovered, stuck_s, colours, _hash = wait_for_recovery(
                qmp, shot, opts.stuck_seconds, opts.poll, opts.colours,
                opts.stable_seconds, reference_hash,
            )
            result["survived_reset"] = recovered
            result["stuck_s"] = round(stuck_s, 1)
            result["last_colours"] = colours
            if recovered:
                print(f"    [trial {index}] survived: new frame after "
                      f"{stuck_s:.1f}s", flush=True)
            else:
                print(f"    [trial {index}] HUNG: no graphical frame in "
                      f"{stuck_s:.1f}s post-reset (stuck at {colours} colours)",
                      flush=True)
        finally:
            try:
                qmp.cmd("quit")
            except Exception:
                pass
            qmp.close()
    finally:
        time.sleep(1)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    if result["survived_reset"] is False and opts.confirm_fresh:
        print(f"    [trial {index}] confirming a fresh process against the "
              f"same disk still boots...", flush=True)
        fresh_cmd = build_command(opts, disk, qmp_port + 1, vnc_display,
                                   ssh_port)
        fresh_proc = subprocess.Popen(fresh_cmd, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
        try:
            time.sleep(5)
            fresh_qmp = Qmp(qmp_port + 1)
            try:
                fresh_shot = workdir / f"trial-{index}-fresh.ppm"
                boot_s, colours, _hash = wait_for_stable_frame(
                    fresh_qmp, fresh_shot, opts.boot_cap, opts.stable_seconds,
                    opts.poll, opts.colours,
                )
                result["fresh_process_recovers"] = boot_s is not None
                if boot_s is not None:
                    print(f"    [trial {index}] fresh process reached the same "
                          f"screen in {boot_s:.1f}s - the disk was never the "
                          f"problem", flush=True)
                else:
                    print(f"    [trial {index}] fresh process ALSO failed to "
                          f"boot ({colours} colours) - investigate the disk/ISO "
                          f"before trusting this trial", flush=True)
            finally:
                try:
                    fresh_qmp.cmd("quit")
                except Exception:
                    pass
                fresh_qmp.close()
        finally:
            time.sleep(1)
            if fresh_proc.poll() is None:
                fresh_proc.kill()
                fresh_proc.wait(timeout=10)

    if not opts.keep_disks:
        disk.unlink(missing_ok=True)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--iso", required=True, type=pathlib.Path)
    ap.add_argument("--disk-template", type=pathlib.Path,
                     help="copy this qcow2 instead of creating a blank one "
                          "(e.g. an overlay frozen at 'needs to restart')")
    ap.add_argument("--disk-size", default="20G")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--machine", default="q35")
    ap.add_argument("--accel", default="whpx,kernel-irqchip=off")
    ap.add_argument("--cpu", default="Westmere")
    ap.add_argument("--cpus", type=int, default=2)
    ap.add_argument("--memory", default="2048")
    ap.add_argument("--colours", type=int, default=COLOUR_THRESHOLD,
                     help=f"distinct colours above which a frame counts as "
                          f"graphical (default {COLOUR_THRESHOLD}, matching "
                          f"reboot_watchdog.py)")
    ap.add_argument("--boot-cap", type=float, default=90,
                     help="max seconds to wait for the pre-reset stable frame")
    ap.add_argument("--stable-seconds", type=float, default=15,
                     help="how long a frame must repeat unchanged to count as reached")
    ap.add_argument("--stuck-seconds", type=float, default=120,
                     help="max seconds to wait for recovery after system_reset "
                          "(reboot_watchdog.py's production default is 300s; "
                          "this is a faster check, not a claim the guest is "
                          "permanently stuck sooner than that)")
    ap.add_argument("--poll", type=float, default=3)
    ap.add_argument("--confirm-fresh", action=argparse.BooleanOptionalAction,
                     default=True,
                     help="after a hang, verify a fresh process against the "
                          "same disk still boots (default: on)")
    ap.add_argument("--keep-disks", action="store_true",
                     help="don't delete the per-trial disk overlays")
    ap.add_argument("--qmp-port-base", type=int, default=45500)
    ap.add_argument("--ssh-port-base", type=int, default=12200)
    ap.add_argument("--qemu", default=shutil.which("qemu-system-x86_64")
                     or r"C:\Program Files\qemu\qemu-system-x86_64.exe")
    ap.add_argument("--qemu-img", default=shutil.which("qemu-img")
                     or r"C:\Program Files\qemu\qemu-img.exe")
    opts = ap.parse_args()

    if not opts.iso.exists():
        ap.error(f"--iso not found: {opts.iso}")

    version = subprocess.run([opts.qemu, "--version"], capture_output=True,
                              text=True, check=True).stdout.splitlines()[0]
    print(f"{version}\n{opts.trials} trial(s), -machine {opts.machine} "
          f"-accel {opts.accel} -cpu {opts.cpu}\n")

    results = []
    with tempfile.TemporaryDirectory(prefix="reset_measure_") as tmp:
        workdir = pathlib.Path(tmp)
        for i in range(opts.trials):
            print(f"[trial {i + 1}/{opts.trials}]", flush=True)
            results.append(run_trial(opts, i, workdir))

    survived = [r for r in results if r["survived_reset"] is True]
    hung = [r for r in results if r["survived_reset"] is False]
    untested = [r for r in results if r["survived_reset"] is None]
    print(f"\n{len(hung)}/{len(results)} hung after system_reset, "
          f"{len(survived)}/{len(results)} survived, "
          f"{len(untested)}/{len(results)} never reached a testable state")
    if hung:
        confirmed = [r for r in hung if r["fresh_process_recovers"] is True]
        print(f"of the hangs, {len(confirmed)}/{len(hung)} confirmed a fresh "
              f"process against the same disk recovers")
    if untested:
        print("WARNING: a trial that never booted proves nothing about the "
              "reset defect - check --iso, --boot-cap and --colours before "
              "trusting the numbers above", file=sys.stderr)


if __name__ == "__main__":
    main()
