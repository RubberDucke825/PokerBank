"""Config locations (override the config dir with PN_CONFIG_DIR)."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def config_dir() -> Path:
    env = os.environ.get("PN_CONFIG_DIR")
    return Path(env) if env else repo_root() / "config"
