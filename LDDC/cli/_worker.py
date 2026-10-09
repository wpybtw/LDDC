# SPDX-FileCopyrightText: Copyright (C) 2026 wpybtw
# SPDX-License-Identifier: GPL-3.0-only
"""Private subprocess protocol: isolate provider failures and enforce hard deadlines."""

import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from .runtime import initialize, serialize


def request(payload: dict) -> dict:
    initialize(Path(payload["state_dir"]), payload["settings"])
    from LDDC.common.models import SearchType

    source = payload["source"]
    # Import only the requested provider. The aggregate API initializes all providers.
    if source == "NE":
        from LDDC.core.api.lyrics.ne import NEAPI

        provider = NEAPI()
    elif source == "QM":
        from LDDC.core.api.lyrics.qm import QMAPI

        provider = QMAPI()
    elif source == "KG":
        from LDDC.core.api.lyrics.kg import KGAPI

        provider = KGAPI()
    elif source == "LRCLIB":
        from LDDC.core.api.lyrics.lrclib import LrclibAPI

        provider = LrclibAPI()
    else:
        msg = "Unsupported source"
        raise ValueError(msg)
    results = provider.search(payload["query"], SearchType[payload["type"]], payload["page"])
    return {"items": [serialize(item) for item in list(results)[: payload["settings"]["search"]["limit"]]]}


def main() -> None:
    payload = json.load(sys.stdin)
    try:
        with redirect_stdout(sys.stderr):
            response = request(payload)
    except Exception as exc:
        # Provider exceptions can contain headers, cookies or proxy URLs. Do not forward them.
        response = {"error": {"code": "PROVIDER_ERROR", "type": type(exc).__name__, "message": "Provider request failed"}}
    json.dump(response, sys.stdout, ensure_ascii=False, allow_nan=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
