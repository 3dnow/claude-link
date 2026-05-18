# claude-link architecture & three Unix traps

A statusline plugin for Claude Code that distinguishes "the model is generating a response right now" from "the model is thinking, but nothing's flowing" from "your link is dead" looks deceptively simple. The pieces:

```
┌─────────────────┐   ┌──────────────────┐   ┌─────────────────────┐
│ UserPromptSubmit│   │       Stop       │   │  background daemon  │
│  hook (per sid) │   │  hook (per sid)  │   │  (plugin monitor)   │
│        ↓        │   │        ↓         │   │          ↓          │
│ /tmp/claude-link-state-<sid>.json     │   │  nettop streaming   │
│  (pid resolved by parent-walk:        │   │  via PTY (1Hz batch)│
│   comm=claude OR args contain         │   │     +               │
│   "/share/claude/versions/"           │   │  curl HTTPS RTT     │
│   OR "claude daemon")                 │   │  probe (every 15s)  │
└────────────────────┬──────────────────┘   └─────────┬───────────┘
                     │                                 │
                     ▼                                 ▼
              ┌──────────────────────────────────────────────┐
              │     /tmp/claude-link-status.json (1Hz)       │
              │  { pids: { 32746: { phase, speed_in, ... }} │
              │    sessions: { sid → pid mapping }           │
              │    aggregate: { ... } }                      │
              └──────────────────────┬───────────────────────┘
                                     │
                                     ▼
                       claude-link-fragment (statusline)
                          renders ⬆/⬇/⏳/● using
                          $CC_NET_SESSION_ID to pick view
```

Building it took two days. The first day was the architecture above. The second day was three classic Unix traps, each of which I'd told myself I'd "never fall for." Documented here because they're the kind of thing that's obvious only in hindsight.

## Trap 1: ICMP is meaningless under DNS-hijacking TUN proxies

Initial latency measurement: `ping api.anthropic.com`. It returned `0.3 ms` on a wifi-only laptop with no special routing.

The link wasn't that fast. The hostname resolved to `198.18.0.42` — a fake IP from Quantumult X's TUN pool. ICMP went out to the local `utun` interface, echoed back in microseconds, and never touched the network.

This is well-known to anyone who works on transparent proxies, but easy to forget when you reach for `ping` as a "quick sanity check." The fix is to measure a metric that *requires* the real network path:

```bash
curl -o /dev/null -s --connect-timeout 3 -w '%{time_connect}' https://api.anthropic.com/
```

`time_connect` is the elapsed time from connection start to TCP handshake completion, including whatever proxy hops are in between. Under a TUN proxy with a remote endpoint, this is the real RTT (proxy → endpoint round-trip plus proxy overhead). Under no proxy, it's the direct TCP handshake.

We probe this every 15 seconds. It's the value shown in the statusline as `● Nms`.

## Trap 2: stdio buffering flips between line and block depending on isatty()

`nettop -L 0 -s 1 -P -J bytes_in,bytes_out -x` produces a stream of comma-separated rows, one batch per second. Plumbing it into the daemon via `subprocess.Popen(..., stdout=PIPE)` and reading lines should be straightforward.

It wasn't. We'd see ~30 seconds of silence and then a flood of 30 batches at once.

The reason: glibc / Apple libc switch stdout to **block-buffered mode** when it's not a terminal. Each row from nettop is ~30 bytes; the default stdout buffer is 4 KB; nothing flushes until the buffer fills, which takes ~half a minute at our rate. When the child *is* writing to a tty, stdio switches to **line-buffered mode** and flushes on every `\n`.

`stdbuf -oL` would normally override this, but on macOS it's restricted (it relies on `LD_PRELOAD` which can't be applied to system tools). The portable fix is to give the child a tty:

```python
import pty, subprocess

master_fd, slave_fd = pty.openpty()
proc = subprocess.Popen(
    ["nettop", "-P", "-L", "0", "-s", "1", "-J", "bytes_in,bytes_out", "-x"],
    stdout=slave_fd,
    stderr=subprocess.DEVNULL,
    stdin=subprocess.DEVNULL,
    start_new_session=True,
    close_fds=True,
)
os.close(slave_fd)
reader = os.fdopen(master_fd, "r", encoding="utf-8", errors="replace")
for line in reader:
    ...
```

nettop sees a tty on its stdout, switches itself to line-buffered, and we get one row per `\n`. Latency drops from 30s to <1s.

## Trap 3: subprocess outlives parent on SIGKILL → orphan burning 130% CPU

The daemon is a long-running Python process that spawns nettop and reads from it. Killing the daemon should kill nettop too.

It didn't. Killing the daemon with `kill -9` (SIGKILL) left the nettop child reparented to PID 1 and continuing to consume CPU forever. nettop happily writes to its pty slave; the master side is closed but the slave doesn't immediately notice (PTYs don't deliver SIGPIPE on the writer side the way regular pipes do). So nettop never exits.

Across a few daemon restarts during development, the machine accumulated several orphan nettop processes, each at ~130% CPU. Aggregate: hundreds of percent of CPU, none of it visibly attributable to anything.

Three layers of defense, ordered from "graceful" to "self-healing":

### 3a. `start_new_session=True` on the child

```python
proc = subprocess.Popen([...], start_new_session=True, ...)
```

This makes nettop the leader of its own process group. We can now `os.killpg(proc.pid, ...)` to target the whole group, which is necessary in case nettop itself ever fork-spawns a child (it doesn't today, but defending in depth is cheap).

### 3b. Graceful teardown that actually escalates

The naive `proc.terminate(); proc.wait(timeout=2)` doesn't help when the pty-writer is wedged. We do:

```python
try:
    os.killpg(proc.pid, signal.SIGTERM)
except Exception:
    proc.terminate()
try:
    proc.wait(timeout=1)
except subprocess.TimeoutExpired:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()
    proc.wait(timeout=1)
```

SIGTERM the group, give it 1s, then SIGKILL the group. This handles the normal shutdown path correctly.

### 3c. Reap orphans at next startup (the actual self-healing bit)

If the daemon itself dies via SIGKILL — say, an OS killer triggered, or someone running `pkill -9` — no cleanup code runs at all. The child is now orphaned and there's no one to kill it.

The fix is at the *other* end of the lifecycle: when a daemon starts, scan for orphaned nettop processes from any previous incarnation and reap them.

```python
def reap_orphan_nettop():
    out = subprocess.check_output(["ps", "-axo", "pid=,ppid=,args="], text=True)
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3: continue
        pid, ppid, args = int(parts[0]), int(parts[1]), parts[2]
        if ppid != 1 or "nettop" not in args: continue
        # Match our specific spawn signature to avoid killing a user's
        # interactive `nettop`.
        if "-P" in args and "-L 0" in args and "bytes_in" in args:
            try: os.kill(pid, signal.SIGKILL)
            except OSError: pass
```

Match by `ppid=1` (orphaned) + argv signature (avoid killing a different `nettop`). Call this once at daemon startup, before spawning our own child.

End-to-end verification: SIGKILL the daemon, observe the orphan nettop, restart the daemon, watch the log:

```
2026-05-18T13:48:15 reaped 1 orphan nettop process(es) from a prior run
2026-05-18T13:48:15 start pid=55240
```

Self-healing.

## Why this matters beyond claude-link

Each of these three traps maps directly to a class of bug that comes up across systems work:

- **Trap 1** is the same problem you face when you write a monitoring tool that assumes "is the link up" can be answered by a single primitive. It can't. Different probes measure different things, and a transparent proxy can short-circuit any of them.
- **Trap 2** is why every tool you write that wraps another tool needs you to think carefully about how the wrapped tool detects its environment. Half of debugging "why is my pipeline silent for 30s" is the same root cause.
- **Trap 3** is process lifecycle management — the inverse of "how do I write malware that survives parent death." If you've ever written an EDR rule about orphaned processes inheriting from PID 1, you know exactly what shape this looks like.

The plugin itself is small. The traps are the actual content.
