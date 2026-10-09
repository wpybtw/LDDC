# SPDX-FileCopyrightText: Copyright (C) 2026 wpybtw
# SPDX-License-Identifier: GPL-3.0-only
"""Agent-friendly CLI: versioned JSON, explicit writes, isolated configuration."""

import argparse
import copy
import importlib.util
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

from LDDC import __version__

from . import config

EXIT_CODES = {"ok": 0, "internal": 1, "usage": 2, "no_match": 3, "provider": 4, "file": 5, "timeout": 6, "dependency": 7}


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise config.ConfigError(message)


def parser() -> argparse.ArgumentParser:
    root = Parser(description="LDDC CLI. All command results are schema-versioned JSON. No GUI is started.")
    root.add_argument("--config", help="TOML file; otherwise LDDC_CLI_CONFIG or the platform default")
    root.add_argument("--state-dir", help="Isolated cache/log directory (default: CLI config directory/state)")
    root.add_argument("--json", action="store_true", help="JSON is the default output format; accepted for explicit agent calls")
    root.add_argument("--version", action="version", version=f"LDDC {__version__} (CLI schema 1)")
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    commands.add_parser("capabilities", help="List commands, sources, formats, defaults and exit codes")
    commands.add_parser("doctor", help="Check local dependencies without making network requests")
    cfg = commands.add_parser("config", help="Initialize, show or validate CLI configuration")
    cfg.add_argument("action", choices=("init", "show", "validate"))
    cfg.add_argument("--output", type=Path, help="Destination for config init; existing files are never replaced")
    inspect = commands.add_parser("inspect", help="Read local audio metadata (no network or tag writes)")
    inspect.add_argument("path", type=Path)
    inspect.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=None)
    search = commands.add_parser("search", help="Search metadata; does not download or save lyrics")
    search.add_argument("--query", help="Explicit search query")
    search.add_argument("--title")
    search.add_argument("--artist")
    search.add_argument("--type", choices=("song", "album", "songlist"), default="song")
    search.add_argument("--page", type=int, default=1)
    match = commands.add_parser("match", help="Rank metadata candidates for a local file/folder; always read-only")
    match.add_argument("path", type=Path)
    match.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=None)
    match.add_argument("--dry-run", action="store_true", help="Matching is always a dry run in this version")
    match.add_argument("--min-score", type=float)
    match.add_argument("--duration-tolerance-ms", type=int)
    match.add_argument("--top-k", type=int)
    match.add_argument("--output", type=Path, help="Save the JSON candidate report, never overwrite an existing report")
    for command in (search, match):
        command.add_argument("--sources", nargs="+", choices=config.SOURCES)
        command.add_argument("--limit", type=int)
        command.add_argument("--timeout", type=float)
        command.add_argument("--retries", type=int)
        command.add_argument("--workers", type=int)
        command.add_argument("--proxy", help="HTTP(S) proxy without credentials")
    convert = commands.add_parser("convert", help="Convert an existing local lyric file offline")
    convert.add_argument("path", type=Path)
    convert.add_argument("--format", choices=config.FORMATS)
    convert.add_argument("--languages", nargs="+", choices=("orig", "ts", "roma"))
    convert.add_argument("--offset-ms", type=int)
    convert.add_argument("--output", type=Path, help="Destination; otherwise configured directory or JSON content")
    convert.add_argument("--overwrite", action=argparse.BooleanOptionalAction, default=None)
    return root


def overrides(args: argparse.Namespace) -> dict:
    result = {}
    for field, section, key in [
        ("sources", "search", "sources"),
        ("limit", "search", "limit"),
        ("timeout", "network", "timeout_seconds"),
        ("retries", "network", "retries"),
        ("workers", "network", "workers"),
        ("proxy", "network", "proxy"),
        ("min_score", "match", "min_score"),
        ("duration_tolerance_ms", "match", "duration_tolerance_ms"),
        ("top_k", "match", "top_k"),
        ("recursive", "files", "recursive"),
        ("format", "output", "format"),
        ("languages", "output", "languages"),
        ("offset_ms", "output", "offset_ms"),
        ("overwrite", "output", "overwrite"),
    ]:
        value = getattr(args, field, None)
        if value is not None:
            result.setdefault(section, {})[key] = value
    return result


def envelope(command: str, data: dict, code: int = 0, errors: list | None = None) -> dict:
    errors = errors if errors is not None else data.pop("errors", [])
    status = "ok" if code == 0 else "partial" if data.get("items") else "error"
    return {"schema_version": 1, "command": command, "status": status, "exit_code": code, "data": data, "errors": errors}


def result_code(data: dict, command: str) -> int:
    errors = data.get("errors", [])
    if errors:
        if all(error["code"] == "TIMEOUT" for error in errors):
            return EXIT_CODES["timeout"]
        return EXIT_CODES["file"] if command == "inspect" or all(error["code"] == "READ_FAILED" for error in errors) else EXIT_CODES["provider"]
    if command == "match" and (not data["items"] or any(item["status"] == "no_match" for item in data["items"])):
        return EXIT_CODES["no_match"]
    return 0


def execute(args: argparse.Namespace) -> dict:
    from .backend import atomic_write

    if args.command == "config" and args.action == "init":
        path = (args.output or config.config_path(args.config)).expanduser().resolve()
        atomic_write(path, config.EXAMPLE)
        return envelope("config", {"path": str(path)})
    settings, path = config.load(args.config, overrides(args))
    state = Path(args.state_dir).expanduser().resolve() if args.state_dir else path.parent / "state"
    if args.command == "config":
        return envelope("config", {"path": str(path), "exists": path.exists(), "settings": settings, "state_dir": str(state)})
    if args.command == "capabilities":
        return envelope(
            "capabilities",
            {
                "commands": ["config", "doctor", "inspect", "search", "match", "convert"],
                "sources": list(config.SOURCES),
                "formats": list(config.FORMATS),
                "defaults": copy.deepcopy(config.DEFAULTS),
                "exit_codes": EXIT_CODES,
                "match_writes_audio": False,
                "remote_lyrics_download": False,
                "resume": False,
            },
        )
    if args.command == "doctor":
        dependencies = {
            name: importlib.util.find_spec(name) is not None for name in ("PySide6", "httpx", "mutagen", "diskcache", "charset_normalizer", "pyaes")
        }
        code = 0 if all(dependencies.values()) else EXIT_CODES["dependency"]
        return envelope(
            "doctor",
            {"python": sys.version.split()[0], "dependencies": dependencies, "config_path": str(path), "state_dir": str(state), "network_tested": False},
            code,
        )
    from . import backend

    if args.command == "search":
        if args.page < 1:
            msg = "--page must be positive"
            raise config.ConfigError(msg)
        if args.query and (args.title or args.artist):
            msg = "Use --query or --title/--artist, not both"
            raise config.ConfigError(msg)
        query = args.query or " - ".join(part for part in (args.artist, args.title) if part)
        if not query or not query.strip():
            msg = "Provide --query, --title or --artist"
            raise config.ConfigError(msg)
        data = backend.search(query.strip(), args.type.upper(), args.page, settings, state)
    elif args.command == "inspect":
        data = backend.inspect(args.path.expanduser().resolve(), settings, state)
    elif args.command == "match":
        data = backend.match(args.path.expanduser().resolve(), settings, state)
    elif args.command == "convert":
        output = args.output.expanduser().resolve() if args.output else None
        data = backend.convert(args.path.expanduser().resolve(), output, settings, state)
    result = envelope(args.command, data, result_code(data, args.command))
    if args.command == "match" and args.output:
        atomic_write(args.output.expanduser().resolve(), json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    return result


def main(argv: list[str] | None = None) -> int:
    command = "unknown"
    try:
        args = parser().parse_args(argv)
        command = args.command
        with redirect_stdout(sys.stderr):
            result = execute(args)
    except config.ConfigError as exc:
        result = envelope(command, {}, EXIT_CODES["usage"], [{"code": "INVALID_ARGUMENT", "message": str(exc)}])
    except OSError as exc:
        result = envelope(command, {}, EXIT_CODES["file"], [{"code": "FILE_ERROR", "type": type(exc).__name__, "message": str(exc)}])
    except ImportError as exc:
        result = envelope(command, {}, EXIT_CODES["dependency"], [{"code": "MISSING_DEPENDENCY", "message": f"Missing Python dependency: {exc.name}"}])
    except (ValueError, NotImplementedError) as exc:
        result = envelope(command, {}, EXIT_CODES["usage"], [{"code": "INVALID_INPUT", "message": str(exc)}])
    except KeyboardInterrupt:
        result = envelope(command, {}, 130, [{"code": "INTERRUPTED", "message": "Operation interrupted"}])
    except Exception as exc:
        result = envelope(
            command,
            {},
            EXIT_CODES["internal"],
            [{"code": "INTERNAL_ERROR", "type": type(exc).__name__, "message": "Command failed; inspect the isolated state directory logs"}],
        )
    json.dump(result, sys.stdout, ensure_ascii=False, allow_nan=False)
    sys.stdout.write("\n")
    return result["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
