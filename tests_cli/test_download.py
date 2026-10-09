# SPDX-FileCopyrightText: Copyright (C) 2026 wpybtw
# SPDX-License-Identifier: GPL-3.0-only
"""Download orchestration and real tag writes on generated audio only."""

import copy
import hashlib
import importlib
import json
import shutil
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

from LDDC.cli import _worker, backend, config, download, tagging
from LDDC.cli.__main__ import main, result_code
from LDDC.cli.runtime import initialize
from tests_cli.test_cli import cli

TEXT = "[00:00.100]An original fixture line\n[00:00.300]Test complete\n"


def wav(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\x01\x00\xff\xff" * 2000)
    return path


@pytest.fixture
def settings(tmp_path: Path) -> dict:
    settings = copy.deepcopy(config.DEFAULTS)
    initialize(tmp_path / "state", settings)
    return settings


def matched(path: Path, *, cue: bool = False) -> dict:
    return {
        "local": {"path": str(path), "from_cue": cue},
        "status": "candidates",
        "candidates": [{"song": {"source": "NE", "id": "1"}, "score": 100, "audio_sync_verified": False}],
        "errors": [],
    }


def fake_match(monkeypatch: pytest.MonkeyPatch, items: list) -> None:
    monkeypatch.setattr(backend, "match", lambda *_args: {"items": copy.deepcopy(items), "errors": []})


def fake_download(monkeypatch: pytest.MonkeyPatch) -> list:
    requests = []

    def request(payload: dict, *_args: Any) -> dict:
        requests.append(payload)
        return {"lyrics": {"content": TEXT, "tag_content": TEXT, "available_languages": ["orig"]}}

    monkeypatch.setattr(backend, "provider_request", request)
    return requests


def test_download_and_auto_tags_then_skip(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: dict) -> None:
    audio = wav(tmp_path / "track.wav")
    original = audio.read_bytes()
    settings["download"]["write_tags"] = True
    fake_match(monkeypatch, [matched(audio)])
    requests = fake_download(monkeypatch)
    first = download.download(audio, settings, tmp_path / "state")
    assert result_code(first, "download") == 0
    assert first["items"][0]["tags"]["verified"] is True
    assert audio.with_suffix(".lrc").read_text() == TEXT
    assert tagging.lyrics_text(tagging.load_audio(audio)) == TEXT
    assert Path(str(audio) + ".lddc.bak").read_bytes() == original
    second = download.download(audio, settings, tmp_path / "state")
    assert second["items"][0]["status"] == "skipped_existing"
    assert len(requests) == 1
    assert not list(tmp_path.glob(".lddc-*"))


def test_download_dry_run_never_fetches_or_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: dict) -> None:
    audio = wav(tmp_path / "track.wav")
    before = audio.read_bytes()
    settings["download"]["write_tags"] = True
    fake_match(monkeypatch, [matched(audio)])
    requests = fake_download(monkeypatch)
    result = download.download(audio, settings, tmp_path / "state", dry_run=True)
    assert result["items"][0]["status"] == "planned"
    assert not requests
    assert audio.read_bytes() == before
    assert not audio.with_suffix(".lrc").exists()
    assert not Path(str(audio) + ".lddc.bak").exists()


def test_fallback_and_partial_write_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: dict) -> None:
    audio = wav(tmp_path / "track.wav")
    before = audio.read_bytes()
    item = matched(audio)
    item["candidates"].append({"song": {"source": "QM", "id": "2"}})
    settings["download"]["write_tags"] = True
    fake_match(monkeypatch, [item])

    def request(payload: dict, *_args: Any) -> dict:
        if payload["source"] == "NE":
            return {"error": {"code": "TIMEOUT", "message": "deadline"}}
        return {"lyrics": {"content": TEXT, "tag_content": TEXT}}

    def fail(*_args: Any) -> NoReturn:
        message = "simulated tag failure"
        raise OSError(message)

    monkeypatch.setattr(backend, "provider_request", request)
    monkeypatch.setattr(tagging, "write_tags", fail)
    result = download.download(audio, settings, tmp_path / "state")
    assert result_code(result, "download") == 5
    assert result["items"][0]["sidecar"]["status"] == "written"
    assert result["items"][0]["selected_candidate"]["song"]["source"] == "QM"
    assert result["items"][0]["attempts"][0]["code"] == "TIMEOUT"
    assert audio.read_bytes() == before


def test_recursive_paths_collisions_and_cue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: dict) -> None:
    root = tmp_path / "album"
    one = wav(root / "disc1" / "song.wav")
    two = wav(root / "disc2" / "song.wav")
    settings["output"]["directory"] = str(tmp_path / "exports")
    fake_match(monkeypatch, [matched(one), matched(two)])
    fake_download(monkeypatch)
    result = download.download(root, settings, tmp_path / "state")
    assert result_code(result, "download") == 0
    assert (tmp_path / "exports/disc1/song.lrc").exists()
    assert (tmp_path / "exports/disc2/song.lrc").exists()
    fake_match(monkeypatch, [matched(one), matched(one.with_suffix(".flac"))])
    assert result_code(download.download(root, settings, tmp_path / "state"), "download") == 2
    fake_match(monkeypatch, [matched(one, cue=True)])
    assert result_code(download.download(root, settings, tmp_path / "state"), "download") == 2


def test_all_providers_fail_and_no_match(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: dict) -> None:
    audio = wav(tmp_path / "track.wav")
    fake_match(monkeypatch, [matched(audio)])
    monkeypatch.setattr(backend, "provider_request", lambda *_args: {"error": {"code": "TIMEOUT", "message": "deadline"}})
    result = download.download(audio, settings, tmp_path / "state")
    assert result_code(result, "download") == 6
    assert not audio.with_suffix(".lrc").exists()
    item = matched(audio)
    item["candidates"] = []
    fake_match(monkeypatch, [item])
    assert result_code(download.download(audio, settings, tmp_path / "state"), "download") == 3


def test_failed_readback_preserves_original(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: dict) -> None:
    from LDDC.core import song_info

    audio = wav(tmp_path / "track.wav")
    before = audio.read_bytes()
    monkeypatch.setattr(song_info, "write_lyrics", lambda *_args: None)
    with pytest.raises(OSError, match="verification"):
        tagging.write_tags(audio, TEXT, settings)
    assert audio.read_bytes() == before
    assert not Path(str(audio) + ".lddc.bak").exists()
    assert not list(tmp_path.glob(".lddc-*"))


def test_tag_overwrite_keeps_first_backup_and_unrelated_tags(tmp_path: Path, settings: dict) -> None:
    from mutagen.id3 import SYLT, TIT2, USLT

    audio = wav(tmp_path / "track.wav")
    loaded = tagging.load_audio(audio)
    loaded.tags.add(TIT2(text=["Preserve title"]))
    loaded.tags.add(USLT(text="old", lang="eng", desc="old"))
    loaded.tags.add(SYLT(format=2, type=1, text=[("old", 100)]))
    loaded.save()
    before = audio.read_bytes()
    assert tagging.write_tags(audio, TEXT, settings)["status"] == "skipped_existing"
    settings["tags"]["overwrite"] = True
    tagging.write_tags(audio, TEXT, settings)
    changed = tagging.load_audio(audio)
    assert str(changed.tags["TIT2"]) == "Preserve title"
    assert not changed.tags.getall("SYLT")
    assert len(changed.tags.getall("USLT")) == 1
    tagging.write_tags(audio, TEXT + "extra", settings)
    assert Path(str(audio) + ".lddc.bak").read_bytes() == before


def test_tag_cli_config_and_dry_run(tmp_path: Path) -> None:
    audio = wav(tmp_path / "track.wav")
    source = tmp_path / "source.lrc"
    source.write_text(TEXT)
    (tmp_path / "config.toml").write_text("[tags]\nbackup = false\n[output]\noffset_ms = 100\n")
    original = audio.read_bytes()
    process, result = cli(tmp_path, "tag", str(audio), "--lyrics", str(source), "--dry-run")
    assert process.returncode == 0, result
    assert audio.read_bytes() == original
    process, result = cli(tmp_path, "tag", str(audio), "--lyrics", str(source))
    assert process.returncode == 0, result
    assert "[00:00.200]" in tagging.lyrics_text(tagging.load_audio(audio))
    assert not Path(str(audio) + ".lddc.bak").exists()


@pytest.mark.parametrize("extension", ["flac", "mp3", "m4a", "ogg", "opus", "wma"])
def test_tag_formats_preserve_decoded_audio(tmp_path: Path, settings: dict, extension: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is needed to generate audio fixtures")
    source = wav(tmp_path / "source.wav")
    audio = tmp_path / f"track.{extension}"
    codec = ["-c:a", "vorbis", "-strict", "-2", "-ac", "2", "-ar", "44100"] if extension == "ogg" else []
    subprocess.run(  # noqa: S603 - generated fixture
        [ffmpeg, "-v", "error", "-i", str(source), *codec, "-metadata", "title=Fixture", str(audio)],
        check=True,
        timeout=15,
    )

    def pcm() -> str:
        result = subprocess.run(  # noqa: S603 - fixed executable and generated fixture
            [ffmpeg, "-v", "error", "-i", str(audio), "-f", "s16le", "-"],
            capture_output=True,
            check=True,
            timeout=15,
        )
        return hashlib.sha256(result.stdout).hexdigest()

    before = pcm()
    result = tagging.write_tags(audio, TEXT, settings)
    assert result["verified"] is True
    assert tagging.lyrics_text(tagging.load_audio(audio)) == TEXT
    assert pcm() == before


@pytest.mark.parametrize(
    ("source", "module", "name"),
    [("NE", "ne", "NEAPI"), ("QM", "qm", "QMAPI"), ("KG", "kg", "KGAPI"), ("LRCLIB", "lrclib", "LrclibAPI")],
)
def test_worker_download_routing_and_rendering(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    settings: dict,
    source: str,
    module: str,
    name: str,
) -> None:
    from LDDC.core.api.lyrics.local import LocalAPI

    lyrics = LocalAPI().get_lyrics(data=TEXT)
    received = []

    def get_lyrics(info: Any) -> Any:
        received.append(info)
        return lyrics

    monkeypatch.setattr(importlib.import_module(f"LDDC.core.api.lyrics.{module}"), name, lambda: SimpleNamespace(get_lyrics=get_lyrics))
    settings["download"]["write_tags"] = True
    settings["output"].update(format="srt", offset_ms=100)
    result = _worker.request(
        {"source": source, "operation": "download", "song": {"source": source, "id": "1"}, "settings": settings, "state_dir": str(tmp_path)},
    )
    assert received[0].source.name == source
    assert "00:00:00,200" in result["lyrics"]["content"]
    assert "[00:00.200]" in result["lyrics"]["tag_content"]
    settings["download"]["save_sidecar"] = False
    result = _worker.request(
        {"source": source, "operation": "download", "song": {"source": source, "id": "1"}, "settings": settings, "state_dir": str(tmp_path)},
    )
    assert "content" not in result["lyrics"]
    assert result["lyrics"]["available_languages"] == ["orig"]


@pytest.mark.usefixtures("settings")
def test_download_cli_end_to_end_with_fixture_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    from LDDC.core.api.lyrics import ne
    from LDDC.core.api.lyrics.local import LocalAPI

    audio = wav(tmp_path / "track.wav")
    lyrics = LocalAPI().get_lyrics(data=TEXT)
    monkeypatch.setattr(ne, "NEAPI", lambda: SimpleNamespace(get_lyrics=lambda _info: lyrics))
    monkeypatch.setattr(backend, "search", lambda *_args: {"items": [{"source": "NE", "id": "1", "title": "track", "duration": 500}], "errors": []})
    monkeypatch.setattr(backend, "provider_request", lambda payload, cfg, state: _worker.request({**payload, "settings": cfg, "state_dir": str(state)}))
    cfg = tmp_path / "config.toml"
    cfg.write_text("[download]\nwrite_tags = true\n[tags]\nbackup = false\n")
    assert main(["--config", str(cfg), "download", str(audio)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["data"]["items"][0]["tags"]["verified"] is True
    assert audio.with_suffix(".lrc").exists()
    assert not Path(str(audio) + ".lddc.bak").exists()


def test_download_disabled_and_invalid_json_offset_fail_before_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: dict) -> None:
    requests = fake_download(monkeypatch)
    settings["download"].update(write_tags=False, save_sidecar=False)
    with pytest.raises(ValueError, match="Enable"):
        download.download(tmp_path, settings, tmp_path / "state")
    settings["download"]["save_sidecar"] = True
    settings["output"].update(format="json", offset_ms=500)
    with pytest.raises(ValueError, match="JSON"):
        download.download(tmp_path, settings, tmp_path / "state")
    assert not requests


def test_backup_failure_leaves_audio_unchanged(tmp_path: Path, settings: dict) -> None:
    audio = wav(tmp_path / "track.wav")
    original = audio.read_bytes()
    Path(str(audio) + ".lddc.bak").mkdir()
    with pytest.raises(OSError, match="Backup"):
        tagging.write_tags(audio, TEXT, settings)
    assert audio.read_bytes() == original
    assert not list(tmp_path.glob(".lddc-*"))


def test_concurrent_audio_change_is_not_replaced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: dict) -> None:
    from LDDC.core import song_info

    audio = wav(tmp_path / "track.wav")
    externally_modified = audio.read_bytes() + b"external edit"
    writer = song_info.write_lyrics
    settings["tags"]["backup"] = False

    def concurrent_edit(stage: Path, content: str) -> None:
        writer(stage, content)
        audio.write_bytes(externally_modified)

    monkeypatch.setattr(song_info, "write_lyrics", concurrent_edit)
    with pytest.raises(OSError, match="changed during"):
        tagging.write_tags(audio, TEXT, settings)
    assert audio.read_bytes() == externally_modified
    assert not list(tmp_path.glob(".lddc-*"))


def test_tag_only_and_sidecar_overwrite_are_independent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, settings: dict) -> None:
    audio = wav(tmp_path / "track.wav")
    sidecar = audio.with_suffix(".lrc")
    sidecar.write_text("keep me")
    fake_match(monkeypatch, [matched(audio)])
    fake_download(monkeypatch)
    settings["download"].update(write_tags=True, save_sidecar=False)
    result = download.download(audio, settings, tmp_path / "state")
    assert result["items"][0]["tags"]["status"] == "written"
    assert sidecar.read_text() == "keep me"
    settings["download"]["save_sidecar"] = True
    settings["output"]["overwrite"] = True
    result = download.download(audio, settings, tmp_path / "state")
    assert result["items"][0]["tags"]["status"] == "skipped_existing"
    assert sidecar.read_text() == TEXT


def test_missing_translation_never_writes_header_only_tags(tmp_path: Path, settings: dict) -> None:
    audio = wav(tmp_path / "track.wav")
    before = audio.read_bytes()
    source = tmp_path / "original.lrc"
    source.write_text("[ti:Fixture title]\n[ar:Test author]\n" + TEXT)
    settings["output"]["languages"] = ["ts"]
    with pytest.raises(ValueError, match="No requested"):
        download.tag(audio, source, settings, tmp_path / "state")
    assert audio.read_bytes() == before
