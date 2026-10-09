# SPDX-FileCopyrightText: Copyright (C) 2026 wpybtw
# SPDX-License-Identifier: GPL-3.0-only
"""Download matched lyrics and optionally embed them in local audio files."""

import copy
from collections import Counter
from pathlib import Path

from . import backend, tagging
from .runtime import initialize


def sidecar_path(audio: Path, root: Path, settings: dict) -> Path:
    extension = {"srt": ".srt", "ass": ".ass", "json": ".json"}.get(settings["output"]["format"], ".lrc")
    if directory := settings["output"]["directory"]:
        relative = audio.relative_to(root) if root.is_dir() else Path(audio.name)
        return (Path(directory) / relative).with_suffix(extension)
    return audio.with_suffix(extension)


def needs_write(item: dict, settings: dict) -> tuple[bool, bool]:
    sidecar = item["sidecar"]
    if sidecar["status"] != "disabled" and Path(sidecar["path"]).exists() and not Path(sidecar["path"]).is_file():
        msg = "Sidecar destination exists but is not a file"
        raise OSError(msg)
    need_file = sidecar["status"] != "disabled" and (settings["output"]["overwrite"] or not Path(sidecar["path"]).exists())
    need_tags = item["tags"]["status"] != "disabled"
    if sidecar["status"] != "disabled" and not need_file:
        sidecar["status"] = "skipped_existing"
    if need_tags and not settings["tags"]["overwrite"] and tagging.lyrics_text(tagging.load_audio(Path(item["local"]["path"]))):
        item["tags"]["status"] = "skipped_existing"
        need_tags = False
    return need_file, need_tags


def fetch_candidates(candidates: list[dict], settings: dict, state_dir: Path) -> tuple[dict | None, dict, list]:
    attempts = []
    for candidate in candidates:
        song = candidate["song"]
        response = backend.provider_request({"operation": "download", "source": song["source"], "song": song}, settings, state_dir)
        if "error" not in response:
            return candidate, response["lyrics"], attempts
        attempts.append({"source": song["source"], "id": song.get("id"), **response["error"]})
    return None, {}, attempts


def process_item(item: dict, settings: dict, state_dir: Path, dry_run: bool) -> list:
    path = Path(item["local"]["path"])
    if item["local"].get("from_cue"):
        item["status"] = "error"
        return [{"code": "UNSUPPORTED_INPUT", "path": str(path), "message": "Split CUE tracks into separate audio files before downloading"}]
    need_file, need_tags = needs_write(item, settings)
    if not need_file and not need_tags:
        item["status"] = "skipped_existing"
        return []
    if not item["candidates"]:
        item["status"] = "no_match"
        return []
    if dry_run:
        item["status"] = "planned"
        item["selected_candidate"] = item["candidates"][0]
        return []
    # Do not render formats for destinations that will be skipped.
    effective = copy.deepcopy(settings)
    effective["download"].update(save_sidecar=need_file, write_tags=need_tags)
    selected, lyrics, attempts = fetch_candidates(item["candidates"], effective, state_dir)
    item["attempts"] = attempts
    if selected is None:
        item["status"] = "error"
        return [{"path": str(path), **error} for error in attempts]
    item["selected_candidate"] = selected
    item["available_languages"] = lyrics.get("available_languages", [])
    if need_file:
        target = Path(item["sidecar"]["path"])
        backend.atomic_write(target, lyrics["content"], settings["output"]["overwrite"])
        item["sidecar"]["status"] = "written"
    if need_tags:
        item["tags"] = tagging.write_tags(path, lyrics["tag_content"], settings)
    item["status"] = "downloaded"
    return []


def download(path: Path, settings: dict, state_dir: Path, dry_run: bool = False) -> dict:
    if not any(settings["download"].values()):
        msg = "Enable download.save_sidecar or download.write_tags"
        raise ValueError(msg)
    if settings["download"]["save_sidecar"] and settings["output"]["format"] == "json" and settings["output"]["offset_ms"]:
        msg = "JSON sidecars require output.offset_ms = 0"
        raise ValueError(msg)
    matched = backend.match(path, settings, state_dir)
    items = matched["items"]
    for item in items:
        audio = Path(item["local"]["path"])
        item["sidecar"] = (
            {"status": "planned", "path": str(sidecar_path(audio, path, settings))} if settings["download"]["save_sidecar"] else {"status": "disabled"}
        )
        item["tags"] = {"status": "planned" if settings["download"]["write_tags"] else "disabled", "path": str(audio)}
    destinations = Counter(item["sidecar"].get("path") for item in items if "path" in item["sidecar"])
    errors = list(matched["errors"])
    for item in items:
        if destinations.get(item["sidecar"].get("path"), 0) > 1:
            item["status"] = "error"
            error = {"path": item["local"]["path"], "code": "UNSUPPORTED_INPUT", "message": "Multiple audio files map to the same sidecar"}
            item["errors"].append(error)
            errors.append(error)
            continue
        try:
            item_errors = process_item(item, settings, state_dir, dry_run)
        except Exception as exc:
            item["status"] = "error"
            item_errors = [{"path": item["local"]["path"], "code": "WRITE_FAILED", "type": type(exc).__name__, "message": str(exc)}]
        item["errors"].extend(item_errors)
        errors.extend(item_errors)
    for item in items:
        if item["status"] in ("error", "no_match"):
            for destination in ("sidecar", "tags"):
                if item[destination]["status"] == "planned":
                    item[destination]["status"] = "not_written"
    return {"dry_run": dry_run, "score_is_probability": False, "items": items, "errors": errors}


def tag(path: Path, lyrics_path: Path, settings: dict, state_dir: Path, dry_run: bool = False) -> dict:
    initialize(state_dir, settings)
    from LDDC.core.api.lyrics.local import LocalAPI

    existing = tagging.lyrics_text(tagging.load_audio(path))
    if existing and not settings["tags"]["overwrite"]:
        return {"dry_run": dry_run, "tags": {"status": "skipped_existing", "path": str(path)}}
    lyrics = LocalAPI().get_lyrics(path=lyrics_path)
    rendered = backend.render_lyrics(lyrics, settings, for_tags=True)
    result = {"status": "planned", "path": str(path)} if dry_run else tagging.write_tags(path, rendered["content"], settings)
    return {"dry_run": dry_run, "tags": result, "available_languages": rendered["available_languages"]}
