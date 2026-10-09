# SPDX-FileCopyrightText: Copyright (C) 2026 wpybtw
# SPDX-License-Identifier: GPL-3.0-only
"""Initialize existing core without a GUI, using separate state and runtime settings."""

import os
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

_app = None


def initialize(state_dir: Path, settings: dict) -> None:
    global _app  # noqa: PLW0603 - keep the Qt application alive for the command lifetime
    os.environ["LDDC_STATE_DIR"] = str(state_dir.resolve())
    from PySide6.QtCore import QCoreApplication

    _app = QCoreApplication.instance() or QCoreApplication(["lddc-cli"])
    from LDDC.common.data.config import cfg

    # dict.update intentionally applies process-local options without persisting GUI settings.
    dict.update(
        cfg,
        {
            "langs_order": settings["output"]["languages"],
            "lrc_ms_digit_count": settings["output"]["lrc_ms_digits"],
            "add_end_timestamp_line": settings["output"]["add_end_timestamp"],
            "auto_check_update": False,
            "log_level": "ERROR",
        },
    )


def serialize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return serialize(asdict(value))
    if isinstance(value, dict):
        return {str(k): serialize(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [serialize(v) for v in value]
    return value
