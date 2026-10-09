# SPDX-FileCopyrightText: Copyright (C) 2026 wpybtw
# SPDX-License-Identifier: GPL-3.0-only
"""Discovery, candidate ranking, provider isolation and local rendering."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

from .runtime import initialize, serialize

if TYPE_CHECKING:
    from LDDC.common.models import Lyrics

REMASTER_SUFFIX = re.compile(r"\s*[\[(（]\s*(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?\s*[\])）]\s*$", re.IGNORECASE)


def atomic_write(path: Path, content: str, overwrite: bool = False) -> None:
    """Publish complete output; an existing destination wins unless explicitly replaceable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        if overwrite:
            Path(temporary).replace(path)
        else:
            # Atomic no-clobber publication, including concurrent writers.
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def provider_request(payload: dict, settings: dict, state_dir: Path) -> dict:
    payload = {**payload, "settings": settings, "state_dir": str(state_dir)}
    env = os.environ.copy()
    if proxy := settings["network"]["proxy"]:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy
    error = {"code": "PROVIDER_ERROR", "message": "Provider request failed"}
    for _ in range(settings["network"]["retries"] + 1):
        try:
            process = subprocess.run(
                [sys.executable, "-m", "LDDC.cli._worker"],
                input=json.dumps(payload),
                text=True,
                encoding="utf-8",
                capture_output=True,
                env=env,
                timeout=settings["network"]["timeout_seconds"],
                check=False,
            )
            if process.returncode != 0:
                error = {"code": "WORKER_FAILED", "message": "Provider worker exited unsuccessfully", "exit_code": process.returncode}
                continue
            response = json.loads(process.stdout)
            if "error" not in response:
                return response
            error = response["error"]
        except subprocess.TimeoutExpired:
            error = {"code": "TIMEOUT", "message": "Source attempt exceeded its deadline"}
        except (ValueError, KeyError):
            error = {"code": "WORKER_PROTOCOL_ERROR", "message": "Provider worker returned invalid JSON"}
    return {"error": error}


def _source_search(source: str, query: str, search_type: str, page: int, settings: dict, state_dir: Path) -> dict:
    if source == "LRCLIB" and search_type != "SONG":
        return {"source": source, "items": [], "error": {"code": "UNSUPPORTED_SEARCH_TYPE", "message": "LRCLIB only supports song search"}}
    response = provider_request({"source": source, "query": query, "type": search_type, "page": page}, settings, state_dir)
    return {"source": source, "items": [], **response}


def search(query: str, search_type: str, page: int, settings: dict, state_dir: Path) -> dict:
    sources = settings["search"]["sources"]
    with ThreadPoolExecutor(max_workers=settings["network"]["workers"]) as pool:
        results = list(pool.map(lambda source: _source_search(source, query, search_type, page, settings, state_dir), sources))
    return {
        "query": query,
        "type": search_type.lower(),
        "page": page,
        "items": [item for result in results for item in result["items"]],
        "sources": [{"source": result["source"], "count": len(result["items"]), "ok": "error" not in result} for result in results],
        "errors": [{"source": result["source"], **result["error"]} for result in results if "error" in result],
    }


def inspect(path: Path, settings: dict, state_dir: Path) -> dict:
    initialize(state_dir, settings)
    from LDDC.core.song_info import AUDIO_FORMATS, get_audio_file_infos

    if not path.exists():
        raise FileNotFoundError(path)
    if path.is_dir():
        entries = path.rglob("*") if settings["files"]["recursive"] else path.glob("*")
        files = sorted(p for p in entries if p.is_file() and p.suffix.lower().removeprefix(".") in AUDIO_FORMATS)
    else:
        files = [path]
    items, errors = [], []
    for file in files:
        try:
            if file.suffix.lower() == ".cue":
                from LDDC.core.parser.cue import parse_cue

                infos = parse_cue(file).to_songinfos()
            else:
                infos = get_audio_file_infos(file)
            items.extend(serialize(info) for info in infos)
        except Exception as exc:  # noqa: PERF203 - isolate corrupt files in a batch
            errors.append({"path": str(file), "code": "READ_FAILED", "type": type(exc).__name__, "message": "Cannot read audio metadata"})
    return {"items": items, "errors": errors}


def rank(local: dict, candidates: list[dict], settings: dict) -> dict:
    from LDDC.common.models import SongInfo
    from LDDC.core.algorithm import calculate_artist_score, calculate_title_score, text_difference

    info = SongInfo.from_dict(local)
    accepted, rejected = [], []
    for data in candidates:
        candidate = SongInfo.from_dict(data)
        raw_title = calculate_title_score(info.title or "", candidate.title or "")
        local_title = REMASTER_SUFFIX.sub("", info.title or "")
        candidate_title = REMASTER_SUFFIX.sub("", candidate.title or "")
        title = calculate_title_score(local_title, candidate_title)
        artist = calculate_artist_score(str(info.artist), str(candidate.artist)) if info.artist and candidate.artist else None
        album = text_difference(info.album.lower(), candidate.album.lower()) * 100 if info.album and candidate.album else None
        filename = None
        if not info.title and info.path:
            filename = max(
                text_difference(info.path.stem, text) * 100
                for text in (
                    candidate.title or "",
                    candidate.artist_title(),
                    f"{candidate.title} - {candidate.artist}",
                )
            )
            score = filename
        elif artist is not None:
            score = title * 0.5 + artist * 0.5
            if album is not None:
                score = max(score, title * 0.5 + artist * 0.35 + album * 0.15)
        elif album is not None:
            score = max(title * 0.7 + album * 0.3, title * 0.8)
        else:
            score = title
        if filename is None and title < 30:
            score = max(0, score - 35)
        delta = candidate.duration - info.duration if candidate.duration is not None and info.duration is not None else None
        reasons, warnings = [], []
        if delta is None:
            warnings.append("duration_unknown")
        elif abs(delta) > settings["match"]["duration_tolerance_ms"]:
            reasons.append("duration_mismatch")
        if score < settings["match"]["min_score"]:
            reasons.append("score_below_threshold")
        if album is not None and album < 60:
            warnings.append("album_mismatch")
        if album is None:
            warnings.append("album_unknown")
        if ("remaster" in candidate.full_title.lower() or "remaster" in (candidate.album or "").lower()) and "remaster" not in (
            info.full_title + (info.album or "")
        ).lower():
            warnings.append("remaster_candidate")
        record = {
            "song": data,
            "score": round(score, 3),
            "components": {"title": round(title, 3), "title_raw": round(raw_title, 3), "artist": artist, "album": album, "filename": filename},
            "title_comparison": {"local": local_title, "candidate": candidate_title},
            "duration_delta_ms": delta,
            "eligible": not reasons,
            "reasons": reasons,
            "warnings": warnings,
            "audio_sync_verified": False,
        }
        (rejected if reasons else accepted).append(record)
    # Stable source order breaks equal scores; never use a score as a calibrated probability.
    accepted.sort(key=lambda item: -item["score"])
    rejected.sort(key=lambda item: -item["score"])
    top_k = settings["match"]["top_k"]
    return {"candidates": accepted[:top_k], "rejected": rejected[:top_k], "eligible_count": len(accepted), "rejected_count": len(rejected)}


def match(path: Path, settings: dict, state_dir: Path) -> dict:
    inspected = inspect(path, settings, state_dir)
    items, errors = [], list(inspected["errors"])
    for local in inspected["items"]:
        title = local.get("title")
        artist = "/".join(local.get("artist") or [])
        query = f"{artist} - {title}" if artist and title else title or Path(local["path"]).stem
        found = search(query, "SONG", 1, settings, state_dir)
        ranked = rank(local, found["items"], settings)
        if not ranked["candidates"] and artist and title:
            fallback = search(title, "SONG", 1, settings, state_dir)
            seen = {(item.get("source"), item.get("id"), item.get("mid"), item.get("hash")) for item in found["items"]}
            combined = found["items"] + [
                item for item in fallback["items"] if (item.get("source"), item.get("id"), item.get("mid"), item.get("hash")) not in seen
            ]
            ranked = rank(local, combined, settings)
            found["errors"].extend(fallback["errors"])
        track_errors = [{"path": local.get("path"), **error} for error in found["errors"]]
        errors.extend(track_errors)
        items.append({"local": local, "query": query, "status": "candidates" if ranked["candidates"] else "no_match", "errors": track_errors, **ranked})
    return {"dry_run": True, "score_is_probability": False, "items": items, "errors": errors}


def render_lyrics(lyrics: Lyrics, settings: dict, for_tags: bool = False) -> dict:
    from LDDC.common.models import LyricsFormat
    from LDDC.core.converter import convert2

    formats = {
        "lrc": LyricsFormat.LINEBYLINELRC,
        "verbatim-lrc": LyricsFormat.VERBATIMLRC,
        "enhanced-lrc": LyricsFormat.ENHANCEDLRC,
        "srt": LyricsFormat.SRT,
        "ass": LyricsFormat.ASS,
        "json": LyricsFormat.JSON,
    }
    output_format = LyricsFormat.LINEBYLINELRC if for_tags else formats[settings["output"]["format"]]
    langs = None if output_format == LyricsFormat.JSON else settings["output"]["languages"]
    if langs is not None and not any(lyrics.get(lang) or (lang == "ts" and lyrics.get("LDDC_ts")) for lang in langs):
        msg = "No requested lyric languages are present"
        raise ValueError(msg)
    text = convert2(lyrics, langs, output_format, settings["output"]["offset_ms"])
    if not text:
        msg = "No requested lyric languages are present"
        raise ValueError(msg)
    return {"content": text, "extension": output_format.ext, "available_languages": list(lyrics), "types": serialize(lyrics.types)}


def convert(path: Path, output: Path | None, settings: dict, state_dir: Path) -> dict:
    initialize(state_dir, settings)
    # LocalAPI avoids initializing network providers for an entirely offline operation.
    from LDDC.core.api.lyrics.local import LocalAPI

    rendered = render_lyrics(LocalAPI().get_lyrics(path=path), settings)
    text = rendered.pop("content")
    extension = rendered.pop("extension")
    if output is None and settings["output"]["directory"]:
        output = Path(settings["output"]["directory"]).expanduser() / (path.stem + extension)
    result = {"format": settings["output"]["format"], **rendered}
    if output is None:
        result["content"] = text
    else:
        if output.resolve() == path.resolve() or (output.exists() and Path(path).samefile(output)):
            msg = "Input and output must be different files"
            raise ValueError(msg)
        atomic_write(output, text, settings["output"]["overwrite"])
        result["path"] = str(output.resolve())
    return result
