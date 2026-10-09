# SPDX-FileCopyrightText: Copyright (C) 2026 wpybtw
# SPDX-License-Identifier: GPL-3.0-only
"""Independent, strictly validated CLI configuration (no Qt imports)."""

import copy
import math
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tomllib
except ImportError:
    import tomli as tomllib

DEFAULTS = {
    "search": {"sources": ["NE", "QM", "KG"], "limit": 10},
    "network": {"timeout_seconds": 20.0, "retries": 1, "workers": 3, "proxy": ""},
    "match": {"min_score": 70.0, "duration_tolerance_ms": 4000, "top_k": 5},
    "files": {"recursive": False},
    "download": {"save_sidecar": True, "write_tags": False},
    "tags": {"overwrite": False, "backup": True, "id3_version": "v2.3"},
    "output": {
        "format": "lrc",
        "languages": ["orig", "ts"],
        "offset_ms": 0,
        "directory": "",
        "overwrite": False,
        "lrc_ms_digits": 3,
        "add_end_timestamp": False,
    },
}
SOURCES = ("NE", "QM", "KG", "LRCLIB")
FORMATS = ("lrc", "verbatim-lrc", "enhanced-lrc", "srt", "ass", "json")
EXAMPLE = """# LDDC CLI: command-line options override these settings.
# This file is independent of the desktop application's configuration.
[search]
sources = ["NE", "QM", "KG"]
limit = 10

[network]
# Deadline for EACH source attempt, including provider initialization.
timeout_seconds = 20.0
retries = 1
workers = 3
# Empty means inherit HTTP_PROXY / HTTPS_PROXY / ALL_PROXY.
proxy = ""

[match]
# Heuristic similarity, NOT a probability or synchronization guarantee.
min_score = 70.0
duration_tolerance_ms = 4000
top_k = 5

[files]
recursive = false

[download]
save_sidecar = true
write_tags = false

[tags]
overwrite = false
backup = true # Keep the first original audio as <filename>.lddc.bak.
id3_version = "v2.3"

[output]
format = "lrc"
languages = ["orig", "ts"] # orig: original, ts: translation, roma: romanization
offset_ms = 0
directory = "" # Relative paths are relative to this config file.
overwrite = false
lrc_ms_digits = 3
add_end_timestamp = false
"""


class ConfigError(ValueError):
    """An invalid CLI option or configuration value."""


def default_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "LDDC-CLI"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "LDDC-CLI"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lddc"


def config_path(explicit: str | None = None) -> Path:
    return Path(explicit or os.environ.get("LDDC_CLI_CONFIG") or default_root() / "config.toml").expanduser().resolve()


def validate(settings: dict) -> None:
    for section, values in settings.items():
        if section not in DEFAULTS or not isinstance(values, dict):
            msg = f"Unknown configuration section: {section}"
            raise ConfigError(msg)
        for key, value in values.items():
            if key not in DEFAULTS[section]:
                msg = f"Unknown setting: {section}.{key}"
                raise ConfigError(msg)
            expected = type(DEFAULTS[section][key])
            valid = type(value) is expected
            if expected is float:
                valid = type(value) in (int, float) and math.isfinite(value)
            if not valid:
                msg = f"Invalid type for {section}.{key}; expected {expected.__name__}"
                raise ConfigError(msg)
    for section, key, low, high in [
        ("search", "limit", 1, 100),
        ("network", "timeout_seconds", 0.1, 600),
        ("network", "retries", 0, 3),
        ("network", "workers", 1, 8),
        ("match", "min_score", 0, 100),
        ("match", "duration_tolerance_ms", 0, 60000),
        ("match", "top_k", 1, 100),
        ("output", "lrc_ms_digits", 2, 3),
        ("output", "offset_ms", -3600000, 3600000),
    ]:
        if not low <= settings[section][key] <= high:
            msg = f"{section}.{key} must be between {low} and {high}"
            raise ConfigError(msg)
    for section, key, allowed in [("search", "sources", SOURCES), ("output", "languages", ("orig", "ts", "roma"))]:
        values = settings[section][key]
        if not values or any(not isinstance(v, str) or v not in allowed for v in values) or len(values) != len(set(values)):
            msg = f"{section}.{key} must contain unique values from {', '.join(allowed)}"
            raise ConfigError(msg)
    if settings["output"]["format"] not in FORMATS:
        msg = f"output.format must be one of {', '.join(FORMATS)}"
        raise ConfigError(msg)
    if settings["tags"]["id3_version"] not in ("v2.3", "v2.4"):
        msg = "tags.id3_version must be v2.3 or v2.4"
        raise ConfigError(msg)
    proxy = settings["network"]["proxy"]
    if proxy:
        try:
            parts = urlsplit(proxy)
            valid = parts.scheme in ("http", "https") and bool(parts.hostname) and parts.port != 0
        except ValueError:
            valid = False
        if not valid or parts.username or parts.password:
            msg = "network.proxy must be an HTTP(S) proxy URL without credentials; use environment variables for credentials"
            raise ConfigError(msg)


def load(explicit: str | None = None, overrides: dict | None = None) -> tuple[dict, Path]:
    path = config_path(explicit)
    settings = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            with path.open("rb") as stream:
                supplied = tomllib.load(stream)
        except (OSError, ValueError) as exc:
            msg = f"Cannot read TOML configuration: {path} ({type(exc).__name__})"
            raise ConfigError(msg) from exc
        for section, values in supplied.items():
            if section not in settings or not isinstance(values, dict):
                msg = f"Unknown configuration section: {section}"
                raise ConfigError(msg)
            settings[section].update(values)
        validate(settings)
        directory = settings["output"]["directory"]
        if directory:
            directory = Path(directory).expanduser()
            settings["output"]["directory"] = str((path.parent / directory).resolve())
    elif explicit or os.environ.get("LDDC_CLI_CONFIG"):
        msg = f"Configuration file does not exist: {path}"
        raise ConfigError(msg)
    for section, values in (overrides or {}).items():
        settings[section].update(values)
    validate(settings)
    return settings, path
