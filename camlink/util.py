"""Small helpers shared by the apps: logging, per-user data folder, timer precision (Windows, macOS)."""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
import time
from pathlib import Path


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def data_dir() -> Path:
    """Per-user folder for settings shared by both apps (linked cameras): %LOCALAPPDATA%\\osmo-pc on
    Windows, ~/Library/Application Support/osmo-pc on macOS, ~/.local/share/osmo-pc on Linux."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    folder = root / "osmo-pc"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@contextlib.contextmanager
def fine_timer():
    """Keep short timers on time while the app runs: the video frame flush (5 ms) and the camera link's
    acknowledgements (every 25 ms). Windows: raise the scheduler tick to 1 ms (the default ~15.6 ms rounds
    them up). macOS: declare a latency-critical activity, so App Nap and timer coalescing don't slow the
    app down while its Terminal window is in the background. Linux needs nothing."""
    winmm = getattr(getattr(ctypes, "windll", None), "winmm", None)
    if winmm is not None:
        winmm.timeBeginPeriod(1)
    activity = _begin_macos_activity()
    try:
        yield
    finally:
        if winmm is not None:
            winmm.timeEndPeriod(1)
        if activity is not None:
            activity[0].endActivity_(activity[1])


def _begin_macos_activity():
    if sys.platform != "darwin":
        return None
    try:
        from Foundation import NSProcessInfo  # pyobjc, installed with bleak on macOS
    except ImportError:
        return None
    # NSActivityUserInitiatedAllowingIdleSystemSleep | NSActivityLatencyCritical: no App Nap, no timer
    # coalescing; the Mac may still go to sleep when idle.
    info = NSProcessInfo.processInfo()
    return info, info.beginActivityWithOptions_reason_(0x00EFFFFF | 0xFF00000000, "Live video from the camera")
