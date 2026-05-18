#!/usr/bin/env python3
"""claude-link daemon — per-session network monitor.

Streams per-process bytes via a long-running `nettop` subprocess, probes HTTPS
connect time to api.anthropic.com via curl, aggregates hook-written
per-session state, and writes a snapshot to /tmp/claude-link-status.json for
the statusline fragment to render.

Designed to be launched by Claude Code's plugin Monitor framework — runs in
foreground, all logging goes to a file, stdout is silent so it does not
generate Claude notifications.
"""

import atexit
import glob
import json
import os
import pty
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# All persistent paths live under /tmp so multiple Claude sessions share state
# (the daemon is single-instance per machine, guarded by a pidfile).
STATE_GLOB  = "/tmp/claude-link-state-*.json"
STATUS_FILE = Path("/tmp/claude-link-status.json")
PIDFILE     = Path("/tmp/claude-link.pid")
LOG_FILE    = Path(
    os.environ.get("CLAUDE_PLUGIN_DATA", "/tmp")
) / "claude-link.log"

PROBE_URL       = "https://api.anthropic.com/"
PROBE_INTERVAL  = 15.0
WRITE_INTERVAL  = 1.0
IDLE_EXIT_SECS  = 30 * 60
STATE_TTL_SECS  = 60 * 60
PHASE_OUT_BPS   = 1024
PHASE_IN_BPS    = 100
TRACK_BPS       = 50
RECENT_BURST    = 3.0
WAIT_CAP_SECS   = 60.0
VIS_HOLD_SECS   = 3.0

_pid_re = re.compile(r"\.(\d+)$")


def log(msg):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def acquire_lock():
    if PIDFILE.exists():
        try:
            other = int(PIDFILE.read_text().strip())
            os.kill(other, 0)
            return False
        except (OSError, ValueError):
            pass
    PIDFILE.write_text(str(os.getpid()))
    atexit.register(lambda: PIDFILE.unlink(missing_ok=True))
    return True


def reap_orphan_nettop():
    """Kill leftover nettop processes that match our spawn signature and have
    been reparented to init (PPID=1). A daemon killed via SIGKILL can't run
    cleanup; its child nettop ends up orphaned and burns CPU. This catches
    them at the next startup. Args match avoids touching a user's
    interactive nettop."""
    try:
        out = subprocess.check_output(["ps", "-axo", "pid=,ppid=,args="], text=True)
    except Exception:
        return
    killed = 0
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        args = parts[2]
        if ppid != 1 or "nettop" not in args:
            continue
        if "-P" in args and "-L 0" in args and "bytes_in" in args:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    if killed:
        log(f"reaped {killed} orphan nettop process(es) from a prior run")


def claude_pids():
    """Identify claude-related processes — interactive TUI (comm='claude'),
    versioned bg sessions and bg-pty wrappers (args contain
    '/share/claude/versions/'), and the central daemon ('claude daemon')."""
    try:
        out = subprocess.check_output(["ps", "-axo", "pid=,comm=,args="], text=True)
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        comm = parts[1]
        args = parts[2] if len(parts) > 2 else ""
        if comm == "claude":
            pids.append(pid)
        elif "/share/claude/versions/" in args:
            pids.append(pid)
        elif "claude daemon" in args:
            pids.append(pid)
    return pids


class NettopFeed:
    """Spawns `nettop` once in CSV streaming mode via a PTY and parses
    batches. nettop reports cumulative bytes per process; deltas across our
    samples give throughput.

    The PTY is required because nettop block-buffers stdout (~4KB) when piped
    to a regular pipe, which batches ~30 seconds of samples before flushing.
    A pty makes it line-buffered.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.latest: dict = {}
        self.batch_ts = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self):
        with self.lock:
            return dict(self.latest), self.batch_ts

    def _run(self):
        while not self._stop.is_set():
            master_fd = slave_fd = None
            proc = None
            try:
                master_fd, slave_fd = pty.openpty()
                proc = subprocess.Popen(
                    ["nettop", "-P", "-L", "0", "-s", "1",
                     "-J", "bytes_in,bytes_out", "-x"],
                    stdout=slave_fd, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, close_fds=True,
                    # Own session so we can killpg() the whole group
                    # cleanly, and so signals to our TUI don't interrupt
                    # nettop mid-read.
                    start_new_session=True,
                )
                os.close(slave_fd); slave_fd = None
                reader = os.fdopen(master_fd, "r", encoding="utf-8",
                                   errors="replace")
                master_fd = None
            except Exception as e:
                log(f"nettop spawn failed: {e}")
                for fd in (master_fd, slave_fd):
                    if fd is not None:
                        try: os.close(fd)
                        except OSError: pass
                time.sleep(5)
                continue

            batch: dict = {}
            first_header = False
            try:
                for line in reader:
                    if self._stop.is_set():
                        break
                    line = line.rstrip("\r\n")
                    if line.startswith(",bytes_in"):
                        if first_header:
                            with self.lock:
                                self.latest = batch
                                self.batch_ts = time.time()
                        batch = {}
                        first_header = True
                        continue
                    parts = line.split(",")
                    if len(parts) < 3:
                        continue
                    m = _pid_re.search(parts[0])
                    if not m:
                        continue
                    try:
                        pid = int(m.group(1))
                        bi = int(parts[1])
                        bo = int(parts[2])
                    except ValueError:
                        continue
                    prev = batch.get(pid, (0, 0))
                    batch[pid] = (prev[0] + bi, prev[1] + bo)
            except Exception as e:
                log(f"nettop read error: {e}")

            try: reader.close()
            except Exception: pass
            # Aggressive teardown: SIGTERM the whole group, escalate to
            # SIGKILL after 1s. Plain proc.terminate() previously let
            # orphan nettop survive when the parent itself was SIGKILL'd
            # before this cleanup could run.
            try: os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                try: proc.terminate()
                except Exception: pass
            try: proc.wait(timeout=1)
            except Exception:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    try: proc.kill()
                    except Exception: pass
                try: proc.wait(timeout=1)
                except Exception: pass

            if self._stop.is_set():
                return
            log("nettop subprocess ended; restarting in 2s")
            time.sleep(2)


def probe_ms(url):
    """TCP-connect RTT in ms via curl. Reflects the real network path
    (TUN/SOCKS/system proxy), unlike ICMP which TUN tools may short-circuit
    when they hijack DNS to a fake local IP."""
    try:
        out = subprocess.check_output(
            ["curl", "-o", "/dev/null", "--connect-timeout", "3",
             "--max-time", "5", "-s", "-w", "%{time_connect}", url],
            text=True, stderr=subprocess.DEVNULL, timeout=6,
        )
        sec = float(out.strip() or 0)
        return sec * 1000.0 if sec > 0 else None
    except Exception:
        return None


def read_state_all():
    sessions = {}
    now = time.time()
    for path in glob.glob(STATE_GLOB):
        try:
            mtime = os.path.getmtime(path)
            if now - mtime > STATE_TTL_SECS:
                try: os.unlink(path)
                except OSError: pass
                continue
            s = json.loads(Path(path).read_text())
            sid = s.get("sid") or "default"
            sessions[sid] = {
                "phase": s.get("phase", "idle"),
                "ts":    s.get("ts"),
                "pid":   int(s.get("pid") or 0),
            }
        except Exception:
            continue
    return sessions


class PidTracker:
    def __init__(self):
        self.last_in: dict = {}
        self.last_out: dict = {}
        self.last_ts: dict = {}
        self.out_burst: dict = {}
        self.in_burst: dict = {}
        self.vis_in: dict = {}
        self.vis_out: dict = {}

    def update(self, pid, bi, bo, now):
        prev_ts = self.last_ts.get(pid, 0)
        prev_in = self.last_in.get(pid, bi)
        prev_out = self.last_out.get(pid, bo)
        if prev_ts == 0:
            self.last_in[pid] = bi
            self.last_out[pid] = bo
            self.last_ts[pid] = now
            return 0.0, 0.0
        dt = max(now - prev_ts, 0.001)
        speed_in  = max(0.0, (bi - prev_in)  / dt)
        speed_out = max(0.0, (bo - prev_out) / dt)
        self.last_in[pid] = bi
        self.last_out[pid] = bo
        self.last_ts[pid] = now
        if speed_in > TRACK_BPS:
            self.in_burst[pid] = now
        if speed_out > TRACK_BPS:
            self.out_burst[pid] = now
        if speed_in > 0:
            self.vis_in[pid] = (speed_in, now)
        if speed_out > 0:
            self.vis_out[pid] = (speed_out, now)
        return speed_in, speed_out

    def visible(self, pid, now):
        si, ts_i = self.vis_in.get(pid, (0, 0))
        so, ts_o = self.vis_out.get(pid, (0, 0))
        vi = si if (now - ts_i) < VIS_HOLD_SECS else 0
        vo = so if (now - ts_o) < VIS_HOLD_SECS else 0
        return vi, vo

    def classify(self, pid, speed_in, speed_out, hook_active, now):
        last_out = self.out_burst.get(pid, 0)
        last_in  = self.in_burst.get(pid, 0)

        if speed_in > PHASE_IN_BPS:
            return "downloading", 0.0
        if speed_out > PHASE_OUT_BPS:
            return "uploading", 0.0

        recent = max(last_out, last_in)
        in_active_period = hook_active or (recent and now - recent < RECENT_BURST)
        if not in_active_period:
            return "idle", 0.0

        if speed_in > TRACK_BPS and speed_in >= speed_out:
            return "downloading", 0.0
        if speed_out > TRACK_BPS:
            return "uploading", 0.0

        if last_out > last_in and last_out > 0:
            gap_out = now - last_out
            if gap_out < WAIT_CAP_SECS:
                return "waiting", gap_out
        return "active", 0.0

    def forget(self, alive_pids):
        alive = set(alive_pids)
        for d in (self.last_in, self.last_out, self.last_ts,
                  self.out_burst, self.in_burst,
                  self.vis_in, self.vis_out):
            for pid in list(d.keys()):
                if pid not in alive:
                    d.pop(pid, None)


def write_status(data):
    try:
        tmp = STATUS_FILE.with_name(STATUS_FILE.name + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, STATUS_FILE)
    except Exception as e:
        log(f"write_status failed: {e}")


def main():
    # When run as a plugin Monitor, Claude Code manages the process — do not
    # daemonize. Stdout is consumed as notifications, so we keep it silent.
    sys.stdout = open(os.devnull, "w")

    if not acquire_lock():
        return 0

    reap_orphan_nettop()
    feed = NettopFeed()
    tracker = PidTracker()

    def shutdown(*_):
        feed.stop()
        write_status({"phase": "off", "ts": time.time()})
        log("shutdown")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log(f"start pid={os.getpid()}")

    last_probe_ts = 0.0
    last_probe_ms = None
    idle_since = time.time()

    while True:
        now = time.time()
        pids = claude_pids()
        tracker.forget(pids)

        if pids:
            idle_since = now
        elif now - idle_since > IDLE_EXIT_SECS:
            log("idle too long; exiting")
            shutdown()

        if now - last_probe_ts >= PROBE_INTERVAL:
            last_probe_ms = probe_ms(PROBE_URL)
            last_probe_ts = now

        per_pid_bytes, batch_ts = feed.snapshot()
        have_sample = batch_ts > 0

        hook_sessions = read_state_all()
        pid_to_session = {info["pid"]: sid
                          for sid, info in hook_sessions.items()
                          if info.get("pid")}

        pids_out: dict = {}
        agg_in = agg_out = 0
        agg_vis_in = agg_vis_out = 0
        for pid in pids:
            bi, bo = per_pid_bytes.get(pid, (0, 0))
            speed_in, speed_out = tracker.update(pid, bi, bo, now)
            agg_in  += speed_in
            agg_out += speed_out

            sid_for_pid = pid_to_session.get(pid)
            hook_info = hook_sessions.get(sid_for_pid) if sid_for_pid else None
            hook_active = bool(hook_info and hook_info["phase"] == "active")
            phase, _ = tracker.classify(pid, speed_in, speed_out,
                                        hook_active, now)

            last_out = tracker.out_burst.get(pid, 0)
            last_in  = tracker.in_burst.get(pid, 0)
            recent = max(last_out, last_in)
            in_active = hook_active or (recent and now - recent < RECENT_BURST)
            gap_out = (now - last_out) if last_out else 0
            wait_el = (gap_out if (in_active and 0 < gap_out < WAIT_CAP_SECS
                                   and last_out > last_in) else 0.0)

            elapsed = 0.0
            if hook_info and hook_info.get("ts"):
                elapsed = max(0.0, now - hook_info["ts"])

            vis_in, vis_out = tracker.visible(pid, now)
            agg_vis_in  += vis_in
            agg_vis_out += vis_out
            pids_out[str(pid)] = {
                "phase": phase,
                "speed_in":  int(speed_in),
                "speed_out": int(speed_out),
                "vis_in":  int(vis_in),
                "vis_out": int(vis_out),
                "elapsed":   elapsed,
                "wait_elapsed": wait_el,
                "session_id": sid_for_pid,
            }

        agg_phase = "idle"
        agg_wait = 0.0
        agg_elapsed = 0.0
        priority = {"downloading": 4, "uploading": 3, "waiting": 2,
                    "active": 1, "idle": 0}
        for _, info in pids_out.items():
            if priority.get(info["phase"], 0) > priority.get(agg_phase, 0):
                agg_phase = info["phase"]
                agg_wait = info["wait_elapsed"]
                agg_elapsed = info["elapsed"]

        sessions_out = {}
        active_n = 0
        for sid, info in hook_sessions.items():
            sessions_out[sid] = {
                "pid": info["pid"],
                "phase_hook": info["phase"],
                "ts": info.get("ts"),
            }
            if info["phase"] == "active":
                active_n += 1

        write_status({
            "ts": now,
            "ping_ms": last_probe_ms,
            "claude_running": bool(pids),
            "active_sessions": active_n,
            "total_sessions": len(sessions_out),
            "feed_age": (now - batch_ts) if have_sample else None,
            "pids": pids_out,
            "sessions": sessions_out,
            "aggregate": {
                "phase": agg_phase,
                "speed_in":  int(agg_in),
                "speed_out": int(agg_out),
                "vis_in":  int(agg_vis_in),
                "vis_out": int(agg_vis_out),
                "elapsed":   agg_elapsed,
                "wait_elapsed": agg_wait,
            },
        })

        time.sleep(WRITE_INTERVAL)


if __name__ == "__main__":
    sys.exit(main() or 0)
