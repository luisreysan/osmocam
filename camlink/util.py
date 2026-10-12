"""Small helpers shared by the apps: logging, per-user data folder, Windows timer resolution."""

from __future__ import annotations

import contextlib
import ctypes
import os
import time
from pathlib import Path


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def data_dir() -> Path:
    """Per-user folder for settings shared by both apps (linked cameras): %LOCALAPPDATA%\\osmo-pc."""
    base = os.environ.get("LOCALAPPDATA")
    folder = (Path(base) if base else Path.home() / "AppData" / "Local") / "osmo-pc"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@contextlib.contextmanager
def fine_timer():
    """Raise the Windows scheduler tick to 1 ms while the app runs, so short socket timeouts (the video
    frame flush) and sleeps fire on time instead of being rounded up to the default ~15.6 ms."""
    winmm = getattr(getattr(ctypes, "windll", None), "winmm", None)
    if winmm is not None:
        winmm.timeBeginPeriod(1)
    try:
        yield
    finally:
        if winmm is not None:
            winmm.timeEndPeriod(1)
