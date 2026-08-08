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
import fcntl
import glob
import json
import os
import pty
import re
import signal
import subprocess
import sys
import termios
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
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_ROTATE_INTERVAL = 60.0

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
_last_log_rotate_check = 0.0


def rotate_log_if_needed(now=None):
    """Rotate the daemon log when it grows too large.

    Keep the current log plus one rotated copy, and rate-limit the size check
    so the hot logging path does not stat on every call.
    """
    global _last_log_rotate_check
    now = time.time() if now is None else now
    if now - _last_log_rotate_check < LOG_ROTATE_INTERVAL:
        return
    _last_log_rotate_check = now
    try:
        if LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            os.replace(LOG_FILE, Path(str(LOG_FILE) + ".1"))
    except FileNotFoundError:
        pass


def log(msg):
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        rotate_log_if_needed()
        with LOG_FILE.open("a") as f:
            f.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
    except Exception:
        pass


def _is_daemon_argv(args):
    """True only for a python process actually running cc-netd.py.

    A bare `"cc-netd.py" in args` substring test also matches any process that
    merely *mentions* the script — a grep, an editor, a shell invoked with it
    on its command line. That is not cosmetic: acquire_lock() then refuses to
    start, and the periodic singleton check makes a healthy daemon shut itself
    down. Both fail silently, leaving the statusline dead.
    """
    toks = args.split()
    if not toks:
        return False
    if not any(os.path.basename(t) == "cc-netd.py" for t in toks):
        return False
    exe = os.path.basename(toks[0]).lower()
    return exe.startswith("python") or exe == "env"


def other_daemon_pids():
    """PIDs of OTHER live claude-link daemons (excludes self). The /tmp pidfile
    alone is an unreliable singleton: macOS periodic cleanup deletes a
    long-lived daemon's pidfile (untouched >3 days), after which a fresh start
    sees no pidfile and runs alongside the still-live one. Scanning ps for the
    script name is authoritative regardless of pidfile state."""
    me = os.getpid()
    try:
        out = subprocess.check_output(["ps", "-axo", "pid=,args="], text=True)
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == me:
            continue
        if _is_daemon_argv(parts[1]):
            pids.append(pid)
    return pids


def _release_pidfile():
    """Remove the pidfile only if it still points at us — never clobber a
    sibling that legitimately took over."""
    try:
        if PIDFILE.read_text().strip() == str(os.getpid()):
            PIDFILE.unlink(missing_ok=True)
    except Exception:
        pass


def acquire_lock():
    # Yield to any already-running daemon found via ps (authoritative), not just
    # via the deletable pidfile. Returns the same exit-0 path the Monitor
    # already gets from redundant launches; it just no longer lets a stale or
    # cleaned-up pidfile spawn a second live daemon.
    if other_daemon_pids():
        return False
    PIDFILE.write_text(str(os.getpid()))
    atexit.register(_release_pidfile)
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


def _take_ctty():
    """preexec_fn: make the PTY slave on fd 0 our controlling terminal.
    start_new_session=True has already called setsid(), so we have none yet."""
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    except OSError:
        pass


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
        self._proc_pid = None      # live nettop child, for kill-on-exit
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the feed AND kill the nettop child.

        Setting the event is not enough. The reader thread is a daemon thread
        parked in a blocking read on the PTY master, so it never reaches the
        teardown below once the main thread calls sys.exit() — the child is
        reparented to init and, with the master now closed (stdin permanently
        ready), goes straight back to the ~135% spin this PR set out to fix.
        """
        self._stop.set()
        pid = self._proc_pid
        if pid:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            self._proc_pid = None

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
                    # stdin MUST be a tty, not /dev/null: nettop installs a
                    # dispatch read source on stdin to watch for interactive
                    # keys. /dev/null is perpetually EOF-readable, so that
                    # source fires in a tight loop and nettop burns ~135% CPU
                    # for the daemon's whole lifetime. Handing it the PTY slave
                    # (a tty that blocks on read) drops it to ~0.5%. stdout is
                    # the same slave — the PTY already exists for line
                    # buffering; reusing it for stdin costs nothing.
                    stdin=slave_fd, stdout=slave_fd, stderr=subprocess.DEVNULL,
                    close_fds=True,
                    # Own session so we can killpg() the whole group
                    # cleanly, and so signals to our TUI don't interrupt
                    # nettop mid-read.
                    start_new_session=True,
                    # Adopt the PTY as the controlling terminal. Then if we die
                    # without cleaning up (SIGKILL, crash), closing the master
                    # makes the kernel SIGHUP nettop rather than leaving it to
                    # spin on an always-ready stdin.
                    preexec_fn=_take_ctty,
                )
                self._proc_pid = proc.pid
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
            self._proc_pid = None

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
    # Per-pid temp name: a shared "<name>.tmp" lets two daemons (e.g. a startup
    # overlap window) race on os.replace — one renames it away, the other's
    # replace ENOENTs every tick and floods the log. Keep it per-writer.
    try:
        tmp = STATUS_FILE.with_name(f"{STATUS_FILE.name}.{os.getpid()}.tmp")
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
    atexit.register(feed.stop)
    tracker = PidTracker()

    def shutdown(*_):
        feed.stop()
        write_status({"phase": "off", "ts": time.time()})
        log("shutdown")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGHUP, shutdown)

    log(f"start pid={os.getpid()}")

    last_probe_ts = 0.0
    last_probe_ms = None
    idle_since = time.time()

    last_singleton_check = 0.0
    while True:
        now = time.time()

        # Self-heal the singleton: if a sibling daemon is present (a startup
        # overlap, or a takeover after our pidfile was cleaned), the higher-pid
        # one yields so the fleet converges to one. The lowest-pid daemon never
        # yields, so there is no flapping. Also re-assert the pidfile if it went
        # missing, so reap/external tooling keeps working.
        if now - last_singleton_check >= 30.0:
            last_singleton_check = now
            others = other_daemon_pids()
            if others and min(others) < os.getpid():
                log(f"sibling daemon(s) {sorted(others)} present; yielding")
                shutdown()
            try:
                if PIDFILE.read_text().strip() != str(os.getpid()):
                    PIDFILE.write_text(str(os.getpid()))
            except OSError:
                PIDFILE.write_text(str(os.getpid()))

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
