"""Drives the dashboard's in-browser console the way a person would, and
measures whether the guest's display renders and keyboard input reaches it.

ANSWERED: Phase 19, Q6 (docs/PORTABILITY.md "Q6 results"; DECISIONS #79)
-------------------------------------------------------------------------
Yes, in headless Chromium 156 on a US layout. Two runs (clean close and
browser killed) connected in 208-209 ms. The no-input floor was 8.70e-5 of the
frame (64 px, a 16x4 box: the cursor); Enter and typing changed 15-60x that,
and all 17 typed characters arrived in order. Outputs:
/srv/kurukuru/logs/q6/<timestamp>-<instance>-<close>/. Not tested: a headed
browser, Firefox, other layouts, Ctrl+Alt+Del, fullscreen, reconnect.

WHY THIS EXISTS (docs/PORTABILITY.md, Phase 19, Q6)
---------------------------------------------------
The console is noVNC in the dashboard, bridged by the backend to QEMU's VNC
socket (kurukuru/console.py). A protocol-level client proved the bridge; it
could not prove that a *browser* renders the framebuffer and that keystrokes
typed into the page reach the guest. This does it with a real browser
(headless Chromium, through Playwright), against a real instance, entering
through the exact URL ``kurukuru console <name>`` emits.

WHAT A RUN DOES
---------------
  1. baseline the instance's qemu-process.log, then watch it for new lines
     (timestamping each as it appears: QEMU writes none of its own)
  2. sign in through the login form, resolve the instance by name
  3. open ``/?console=<id>`` and wait for the modal's "Connected" badge
  4. S1 after the display settles; S2 after a wait with NO input (the control)
  5. click the display, press Enter twice -> S3; type the probe string -> S4
  6. close the page cleanly (--close clean) or SIGKILL the browser (--close kill)
  7. keep watching for --post-wait seconds, then collect the backend journal
     for the window [page load - 60 s, close + 60 s]

Every browser console message, page error, failed request and WebSocket
open/close/error (with code and reason) is written verbatim with its time. The
one deliberate exception is the console ticket in the WebSocket URL, which is
replaced by ``<ticket>`` where *this harness* writes the URL: it is a
credential, single-use, but nothing here needs its value.

The password comes from KURUKURU_CONSOLE_TEST_PASSWORD and is never printed or
written. Without it, or without KURUKURU_STATE_DIR, nothing runs.

RESULT
------
summary.json holds the timestamps and, for S1->S2 (the floor: no input was
sent), S2->S3 and S3->S4, the fraction of pixels that differ. Exit status 0 means
the run completed and was measured; it is not a verdict on the console. A
missing instance, a failed sign-in, or a console that never connects exits
non-zero with the reason.

RE-RUNNING Q6
-------------
Playwright lives in its own venv, never the backend's, so the backend's
dependency set stays what it ships with. Once:

    python3 -m venv /srv/kurukuru/phase19/pwenv
    /srv/kurukuru/phase19/pwenv/bin/pip install playwright
    PLAYWRIGHT_BROWSERS_PATH=/srv/kurukuru/phase19/pw-browsers \\
      /srv/kurukuru/phase19/pwenv/bin/python -m playwright install chromium

Then, with KURUKURU_STATE_DIR naming the backend's store and the owner's
password in KURUKURU_CONSOLE_TEST_PASSWORD (set it with ``read -rs``, never on
the command line), one run per close mode:

    PLAYWRIGHT_BROWSERS_PATH=/srv/kurukuru/phase19/pw-browsers \\
      /srv/kurukuru/phase19/pwenv/bin/python tools/console_probe.py svc --close clean
    PLAYWRIGHT_BROWSERS_PATH=/srv/kurukuru/phase19/pw-browsers \\
      /srv/kurukuru/phase19/pwenv/bin/python tools/console_probe.py svc --close kill

``--close kill`` SIGKILLs only the pids that *appeared* while the browser
launched. They are found with ``pgrep -f`` on PLAYWRIGHT_BROWSERS_PATH, before
and after the launch, and the difference is killed. Taking the difference keeps
out anything already running, including the shell. Never kill by the pattern
itself (``pkill -f``): in Phase 19 a pattern match hit the agent's own shell
three times (see reset_matrix.py's header).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import threading
import time

PROBE_TEXT = "kurukuru-q6-probe"

#: Wraps the page's WebSocket so open/close/error reach the harness with the
#: close code and reason, which Playwright's own WebSocket events do not carry.
#: Reported through an exposed binding, not console.log, so the console log
#: stays exactly what the page itself printed.
WS_SPY = """
(() => {
  const Native = window.WebSocket;
  function Spied(url, protocols) {
    const ws = protocols === undefined ? new Native(url) : new Native(url, protocols);
    const report = (kind, extra) => window.__q6ws(Object.assign({kind, url}, extra || {}));
    ws.addEventListener('open', () => report('open'));
    ws.addEventListener('error', () => report('error'));
    ws.addEventListener('close', (e) =>
      report('close', {code: e.code, reason: e.reason, wasClean: e.wasClean}));
    return ws;
  }
  Spied.prototype = Native.prototype;
  for (const k of ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED']) Spied[k] = Native[k];
  window.WebSocket = Spied;
})();
"""


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


class Recorder:
    """Append-only, timestamped, verbatim event log."""

    def __init__(self, path: pathlib.Path) -> None:
        self._f = path.open("a", encoding="utf-8")
        self._lock = threading.Lock()

    def write(self, source: str, level: str, text: str) -> None:
        with self._lock:
            self._f.write(f"{now()}\t{source}\t{level}\t{text}\n")
            self._f.flush()

    def close(self) -> None:
        self._f.close()


class LogTail(threading.Thread):
    """Timestamp each line appended to a file after construction."""

    def __init__(self, path: pathlib.Path, rec: Recorder) -> None:
        super().__init__(daemon=True)
        self.path, self.rec = path, rec
        self.offset = path.stat().st_size if path.exists() else 0
        self.lines: list[tuple[str, str]] = []
        self.stop = threading.Event()

    def run(self) -> None:
        buf = b""
        while not self.stop.is_set():
            try:
                with self.path.open("rb") as f:
                    f.seek(self.offset)
                    chunk = f.read()
            except OSError:
                chunk = b""
            if chunk:
                self.offset += len(chunk)
                buf += chunk
                *done, buf = buf.split(b"\n")
                for raw in done:
                    line = raw.decode("utf-8", "replace")
                    self.lines.append((now(), line))
                    self.rec.write("qemu-process.log", "line", line)
            time.sleep(0.1)


def redact_ticket(url: str) -> str:
    return re.sub(r"ticket=[^&]+", "ticket=<ticket>", url)


def pixel_fraction(a: pathlib.Path, b: pathlib.Path) -> dict:
    import numpy as np
    from PIL import Image

    x = np.asarray(Image.open(a).convert("RGB"), dtype=np.int16)
    y = np.asarray(Image.open(b).convert("RGB"), dtype=np.int16)
    if x.shape != y.shape:
        return {"fraction": None, "error": f"size differs: {x.shape} vs {y.shape}"}
    changed = np.any(x != y, axis=2)
    count = int(changed.sum())
    out = {"fraction": count / changed.size, "pixels_changed": count, "pixels": int(changed.size)}
    if count:
        rows, cols = np.nonzero(changed)
        out["bbox_xywh"] = [int(cols.min()), int(rows.min()),
                            int(cols.max() - cols.min() + 1), int(rows.max() - rows.min() + 1)]
    return out


def browser_pids(marker: str) -> set[int]:
    proc = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    return {int(p) for p in proc.stdout.split()}


def fail(rec: Recorder, summary: dict, out: pathlib.Path, message: str, code: int) -> None:
    summary["result"] = "FAILED"
    summary["failure"] = message
    rec.write("harness", "FAIL", message)
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"FAILED: {message}", file=sys.stderr)
    sys.exit(code)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("instance", help="instance name, resolved through the API")
    ap.add_argument("--instance-id", help="skip name resolution and deep-link this id "
                    "(the negative control for a deep link that matches nothing)")
    ap.add_argument("--close", choices=["clean", "kill"], default="clean")
    ap.add_argument("--base-url", default="http://127.0.0.1:7842")
    ap.add_argument("--username", default=os.environ.get("KURUKURU_CONSOLE_TEST_USERNAME", "owner"))
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("/srv/kurukuru/logs/q6"))
    ap.add_argument("--pre-wait", type=float, default=60, help="watch before page load")
    ap.add_argument("--post-wait", type=float, default=60, help="watch after close")
    ap.add_argument("--settle", type=float, default=5, help="after Connected, before S1")
    ap.add_argument("--control-wait", type=float, default=10, help="between S1 and S2, no input")
    ap.add_argument("--connect-timeout", type=float, default=30)
    opts = ap.parse_args()

    password = os.environ.get("KURUKURU_CONSOLE_TEST_PASSWORD")
    if not password:
        sys.exit("KURUKURU_CONSOLE_TEST_PASSWORD is not set; nothing was run.")
    state_dir = os.environ.get("KURUKURU_STATE_DIR")
    if not state_dir:
        sys.exit("KURUKURU_STATE_DIR is not set; it decides which instance's logs are "
                 "read (PORTABILITY finding 28). Nothing was run.")

    run_id = dt.datetime.now().strftime("%Y%m%dT%H%M%S") + f"-{opts.instance}-{opts.close}"
    out = opts.out / run_id
    out.mkdir(parents=True, exist_ok=False)
    rec = Recorder(out / "events.tsv")
    summary: dict = {"run": run_id, "instance": opts.instance, "close": opts.close,
                     "base_url": opts.base_url, "timestamps": {}}
    ts = summary["timestamps"]

    def mark(name: str) -> None:
        ts[name] = now()
        rec.write("harness", "mark", name)

    qlog = pathlib.Path(state_dir) / "qemu" / "instances" / opts.instance / "qemu-process.log"
    summary["qemu_process_log"] = str(qlog)
    tail = LogTail(qlog, rec)
    summary["qemu_log_baseline_bytes"] = tail.offset
    tail.start()
    mark("baseline")
    if opts.pre_wait:
        time.sleep(opts.pre_wait)

    from playwright.sync_api import Error as PwError
    from playwright.sync_api import TimeoutError as PwTimeout
    from playwright.sync_api import sync_playwright

    marker = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "ms-playwright")
    before = browser_pids(marker)
    frames = {"sent": 0, "received": 0}
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    spawned = browser_pids(marker) - before
    summary["browser"] = f"chromium {browser.version}"
    context = browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()

    def on_ws_event(_source, event: dict) -> None:
        detail = {k: v for k, v in event.items() if k != "url"}
        rec.write("websocket", event.get("kind", "?"),
                  f"{redact_ticket(event.get('url', ''))} {json.dumps(detail)}")

    context.expose_binding("__q6ws", on_ws_event)
    context.add_init_script(WS_SPY)
    page.on("console", lambda m: rec.write(
        "console", m.type, f"{m.text}\t@ {m.location.get('url', '')}:{m.location.get('lineNumber', '')}"))
    page.on("pageerror", lambda e: rec.write("pageerror", "error", f"{e}\n{getattr(e, 'stack', '')}"))
    page.on("requestfailed", lambda r: rec.write(
        "requestfailed", "error", f"{r.method} {redact_ticket(r.url)} :: {r.failure}"))
    page.on("response", lambda r: r.status >= 400 and rec.write(
        "response", str(r.status), f"{r.request.method} {redact_ticket(r.url)}"))

    def on_websocket(ws) -> None:
        ws.on("framesent", lambda _p: frames.__setitem__("sent", frames["sent"] + 1))
        ws.on("framereceived", lambda _p: frames.__setitem__("received", frames["received"] + 1))
        ws.on("socketerror", lambda err: rec.write("websocket", "socketerror", str(err)))

    page.on("websocket", on_websocket)

    def frame_counts(label: str) -> None:
        summary.setdefault("ws_frames", {})[label] = dict(frames)

    try:
        # --- sign in, through the form ------------------------------------
        page.goto(opts.base_url + "/", wait_until="domcontentloaded")
        page.get_by_label("Username").fill(opts.username)
        page.get_by_label("Password").fill(password)
        page.get_by_role("button", name="Sign in").click()
        try:
            page.wait_for_selector("input[type=password]", state="detached", timeout=15000)
        except PwTimeout:
            fail(rec, summary, out, f"sign-in as '{opts.username}' did not complete", 4)
        mark("signed_in")

        # --- resolve the instance, as the CLI does -------------------------
        if opts.instance_id:
            instance_id = opts.instance_id
        else:
            resp = page.request.get(opts.base_url + "/api/instances")
            if not resp.ok:
                fail(rec, summary, out, f"GET /api/instances answered {resp.status}", 5)
            match = [i for i in resp.json() if i.get("name") == opts.instance]
            if not match:
                fail(rec, summary, out, f"no instance named '{opts.instance}'", 3)
            instance_id = match[0]["id"]
        summary["instance_id"] = instance_id

        # --- open the console through the CLI's own deep link -------------
        page.goto(f"{opts.base_url}/?console={instance_id}", wait_until="domcontentloaded")
        mark("page_load")
        try:
            page.get_by_text("Connected", exact=True).wait_for(timeout=opts.connect_timeout * 1000)
        except PwTimeout:
            page.screenshot(path=str(out / "no-connect-page.png"))
            fail(rec, summary, out, f"console never reported Connected within "
                 f"{opts.connect_timeout:.0f} s", 6)
        mark("connected")
        frame_counts("connected")
        canvas = page.locator("canvas").first

        page.wait_for_timeout(opts.settle * 1000)
        canvas.screenshot(path=str(out / "S1.png"))
        mark("S1")
        frame_counts("S1")

        page.wait_for_timeout(opts.control_wait * 1000)  # no input at all
        canvas.screenshot(path=str(out / "S2.png"))
        mark("S2")
        frame_counts("S2")

        canvas.click()
        rec.write("harness", "input", "click display")
        for _ in range(2):
            page.keyboard.press("Enter")
            rec.write("harness", "input", "Enter")
            page.wait_for_timeout(300)
        page.wait_for_timeout(2000)
        canvas.screenshot(path=str(out / "S3.png"))
        mark("S3")
        frame_counts("S3")

        page.keyboard.type(PROBE_TEXT, delay=60)
        rec.write("harness", "input", f"typed {PROBE_TEXT!r}")
        page.wait_for_timeout(2000)
        canvas.screenshot(path=str(out / "S4.png"))
        mark("S4")
        frame_counts("S4")

        # --- close -----------------------------------------------------------
        if opts.close == "clean":
            page.get_by_role("button", name="Close console").click()
            page.wait_for_timeout(1000)
            page.close()
            context.close()
            browser.close()
            mark("page_close")
        else:
            summary["killed_pids"] = sorted(spawned)
            mark("page_close")  # the kill instant
            for pid in spawned:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except PwError as exc:
        if "page_close" not in ts:
            fail(rec, summary, out, f"browser error before close: {exc}", 7)
    finally:
        try:
            pw.stop()
        except Exception:  # noqa: BLE001 - after a SIGKILL the driver may already be gone
            pass

    time.sleep(opts.post_wait)
    tail.stop.set()
    tail.join(timeout=2)
    mark("end")

    # --- correlation material ---------------------------------------------
    t_load = dt.datetime.fromisoformat(ts["page_load"])
    t_close = dt.datetime.fromisoformat(ts["page_close"])
    since = (t_load - dt.timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    until = (t_close + dt.timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    journal = subprocess.run(
        ["journalctl", "--user", "-u", "kurukuru.service", "--no-pager",
         "-o", "short-iso-precise", "--since", since, "--until", until],
        capture_output=True, text=True)
    (out / "journal-kurukuru.txt").write_text(journal.stdout + journal.stderr)
    (out / "qemu-process-new-lines.tsv").write_text(
        "".join(f"{t}\t{line}\n" for t, line in tail.lines))
    summary["qemu_log_new_lines"] = [{"seen": t, "line": line} for t, line in tail.lines]

    # --- pixels ------------------------------------------------------------
    pairs = {"S1_S2_floor": ("S1", "S2"), "S2_S3_enter": ("S2", "S3"), "S3_S4_typed": ("S3", "S4")}
    summary["diff"] = {k: pixel_fraction(out / f"{a}.png", out / f"{b}.png")
                       for k, (a, b) in pairs.items()}
    summary["result"] = "MEASURED"
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    rec.close()
    print(json.dumps({"run": run_id, "diff": {k: v.get("fraction") for k, v in summary["diff"].items()},
                      "qemu_log_new_lines": len(tail.lines)}, indent=2))


if __name__ == "__main__":
    main()
