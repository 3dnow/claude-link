"""Regression tests for the nettop child's lifecycle.

Two failure modes are covered, both of which left a `nettop` spinning a core
long after Claude Code had exited:

1. `NettopFeed.stop()` only set an Event. The reader is a daemon thread parked
   in a blocking read, so its teardown never ran once the main thread called
   sys.exit() — the child outlived us, and with the PTY master closed its stdin
   was permanently ready, i.e. straight back to the ~135% spin.
2. The singleton scan matched any process whose command line merely *mentioned*
   cc-netd.py, so a grep or an editor could stop the daemon from starting — or
   make a healthy one shut itself down.

No `nettop` here: these use `sleep`/`cat` so they run on Linux CI too.
"""

import importlib.util
import os
import pty
import signal
import subprocess
import sys
import time
from pathlib import Path

# Stand-in for nettop: holds the PTY slave open but never reads it, so — like
# nettop — it does NOT exit merely because the master closed. Only a signal
# stops it. `cat` would be the wrong model: it exits on the read error itself
# and would pass these tests even without a controlling terminal.
SLEEPER = [sys.executable, "-c", "import time\nwhile True: time.sleep(0.2)"]

import pytest

_spec = importlib.util.spec_from_file_location(
    "cc_netd",
    Path(__file__).resolve().parent.parent / "plugins" / "claude-link" / "lib" / "cc-netd.py",
)
cc_netd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc_netd)


def _exited(proc, timeout=5.0):
    """True if the child exits within the timeout. proc.wait() rather than a ps
    check: a killed child lingers as a zombie until it is reaped, and ps still
    lists it."""
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


# --- singleton matcher -----------------------------------------------------

@pytest.mark.parametrize("args", [
    "/usr/local/bin/Python /Users/me/.claude/cc-net/cc-netd.py",
    "/usr/bin/env python3 /opt/claude-link/lib/cc-netd.py",
    "python3 ./cc-netd.py",
])
def test_matches_real_daemon_processes(args):
    assert cc_netd._is_daemon_argv(args) is True


@pytest.mark.parametrize("args", [
    "zsh -c ps -axo pid,args | grep cc-netd.py",     # a shell mentioning it
    "grep -E nettop|cc-netd.py",                     # the grep itself
    "vim /Users/me/.claude/cc-net/cc-netd.py",       # an editor
    "tail -f /Users/me/.claude/cc-net/cc-netd.log",  # neighbouring file
    "",
])
def test_ignores_processes_that_merely_mention_the_script(args):
    assert cc_netd._is_daemon_argv(args) is False


# --- child teardown --------------------------------------------------------

def test_stop_kills_the_tracked_child():
    """stop() must kill the child itself rather than trust the reader thread."""
    feed = object.__new__(cc_netd.NettopFeed)      # no real nettop spawn
    feed._stop = cc_netd.threading.Event()
    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    feed._proc_pid = proc.pid

    feed.stop()

    assert feed._stop.is_set()
    assert _exited(proc), "child survived stop()"


def test_stop_is_safe_with_no_child():
    feed = object.__new__(cc_netd.NettopFeed)
    feed._stop = cc_netd.threading.Event()
    feed._proc_pid = None
    feed.stop()                                    # must not raise


def test_pty_child_dies_when_the_master_closes():
    """_take_ctty is the last line of defence: if we are SIGKILL'd and can run
    no cleanup at all, closing the PTY master must still SIGHUP the child."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        SLEEPER, stdin=slave, stdout=slave, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True, preexec_fn=cc_netd._take_ctty,
    )
    os.close(slave)
    time.sleep(0.5)
    assert proc.poll() is None, "child died early"

    os.close(master)                               # == the daemon exiting

    assert _exited(proc), "child survived the master closing"


def test_pty_child_without_ctty_would_survive():
    """Contrast case, and the reason _take_ctty exists: with no controlling
    terminal the child is untouched by the master closing."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        SLEEPER, stdin=slave, stdout=slave, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True,    # no preexec_fn
    )
    os.close(slave)
    time.sleep(0.5)
    os.close(master)

    survived = not _exited(proc, timeout=1.5)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    proc.wait()
    assert survived, "expected the un-ctty'd child to outlive its parent"
