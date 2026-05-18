"""Unit tests for PidTracker — the pure-Python state machine that classifies
each tracked PID's phase from byte deltas. nettop / subprocess / curl are not
exercised here; those are platform-dependent.

Run from repo root:
    python3 -m pytest tests/ -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "claude-link" / "lib"))

# Import the daemon module by file path (it's not a package).
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "cc_netd",
    Path(__file__).resolve().parent.parent / "plugins" / "claude-link" / "lib" / "cc-netd.py",
)
cc_netd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cc_netd)

PidTracker = cc_netd.PidTracker


def test_first_update_returns_zero_speeds():
    t = PidTracker()
    si, so = t.update(pid=100, bi=0, bo=0, now=1000.0)
    assert si == 0.0
    assert so == 0.0


def test_byte_delta_yields_speed():
    t = PidTracker()
    t.update(100, 0, 0, 1000.0)
    si, so = t.update(100, 1000, 2000, 1001.0)
    # 1000 bytes / 1 second = 1000 B/s
    assert si == 1000.0
    assert so == 2000.0


def test_counter_reset_clamped_to_zero():
    """If a process restarts, cumulative counter resets — should not produce
    negative speed."""
    t = PidTracker()
    t.update(100, 1_000_000, 1_000_000, 1000.0)
    si, so = t.update(100, 0, 0, 1001.0)
    assert si == 0.0
    assert so == 0.0


def test_burst_threshold_records_timestamp():
    """A speed_out exceeding TRACK_BPS should register an out_burst."""
    t = PidTracker()
    t.update(100, 0, 0, 1000.0)
    t.update(100, 0, 10_000, 1001.0)  # 10 KB/s out — well above TRACK_BPS
    assert t.out_burst.get(100) == 1001.0


def test_sub_track_traffic_doesnt_register_burst():
    """Below TRACK_BPS (50 B/s) shouldn't register a burst — keeps the
    waiting/active machine from being confused by keepalive noise."""
    t = PidTracker()
    t.update(100, 0, 0, 1000.0)
    t.update(100, 10, 10, 1001.0)  # 10 B/s — sub-noise floor
    assert 100 not in t.out_burst
    assert 100 not in t.in_burst


def test_classify_uploading():
    t = PidTracker()
    phase, wait = t.classify(pid=100, speed_in=0, speed_out=5_000,
                              hook_active=True, now=1000.0)
    assert phase == "uploading"
    assert wait == 0.0


def test_classify_downloading():
    t = PidTracker()
    phase, _ = t.classify(pid=100, speed_in=500, speed_out=0,
                          hook_active=True, now=1000.0)
    assert phase == "downloading"


def test_classify_slow_inbound_classified_as_downloading():
    """Sub-PHASE_IN_BPS but above TRACK_BPS inbound during active period
    should still classify as downloading, not waiting — token streams trickle."""
    t = PidTracker()
    # Prime the tracker with a small inbound burst
    t.update(100, 0, 0, 1000.0)
    t.update(100, 90, 0, 1001.0)  # 90 B/s in — above TRACK_BPS=50, below PHASE_IN_BPS=100
    phase, _ = t.classify(pid=100, speed_in=90, speed_out=0,
                          hook_active=True, now=1002.0)
    assert phase == "downloading"


def test_classify_waiting_after_send():
    """We sent recently, now nothing flowing — classify as waiting."""
    t = PidTracker()
    # Simulate an earlier upload burst
    t.update(100, 0, 0, 1000.0)
    t.update(100, 0, 50_000, 1001.0)  # big out burst
    assert t.out_burst.get(100) == 1001.0
    # Now both speeds zero some time later
    phase, wait = t.classify(pid=100, speed_in=0, speed_out=0,
                             hook_active=True, now=1003.5)
    assert phase == "waiting"
    assert wait == 2.5


def test_classify_idle_when_no_recent_burst_and_no_hook():
    t = PidTracker()
    phase, _ = t.classify(pid=100, speed_in=0, speed_out=0,
                          hook_active=False, now=1000.0)
    assert phase == "idle"


def test_visible_holds_last_speed_in_window():
    t = PidTracker()
    t.update(100, 0, 0, 1000.0)
    t.update(100, 0, 5_000, 1001.0)
    vi, vo = t.visible(100, now=1002.0)  # 1s after burst
    assert vo == 5000.0
    # After hold window expires
    vi, vo = t.visible(100, now=1010.0)
    assert vo == 0


def test_forget_drops_dead_pids():
    t = PidTracker()
    t.update(100, 0, 0, 1000.0)
    t.update(200, 0, 0, 1000.0)
    t.forget(alive_pids=[100])
    assert 100 in t.last_ts
    assert 200 not in t.last_ts
