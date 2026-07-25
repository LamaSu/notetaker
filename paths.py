"""Where notetaker keeps its data.

Override with the NOTETAKER_NOTES_DIR environment variable; otherwise sessions
land in ~/notes. Kept in one place so the poster, reader, and recorder cannot
disagree about it.
"""

from __future__ import annotations

import os
from pathlib import Path


def notes_root() -> Path:
    env = os.environ.get("NOTETAKER_NOTES_DIR")
    if env and env.strip():
        return Path(env).expanduser()
    return Path.home() / "notes"


def coordination_doc() -> Path:
    return notes_root() / "COORDINATION.md"


def reader_state() -> Path:
    return notes_root() / ".reader-state.json"
