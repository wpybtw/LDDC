# SPDX-FileCopyrightText: Copyright (C) 2026 wpybtw
# SPDX-License-Identifier: GPL-3.0-only
"""Offline CLI contract tests. Deliberately outside the GUI's autouse fixtures."""

import copy
import json
import os
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any, NoReturn

import pytest

from LDDC.cli import backend, config
from LDDC.cli.__main__ import main


def cli(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess, dict]:
    env = os.environ.copy()
    env.pop("LDDC_CLI_CONFIG", None)
    # Ensure no user's real configuration is loaded, even when tests run on a workstation.
    cfg = tmp_path / "config.toml"
    if not cfg.exists():
        cfg.write_text("", encoding="utf-8")
    process = subprocess.run(  # noqa: S603 - fixed Python module, test-owned arguments
        [sys.executable, "-m", "LDDC.cli", "--config", str(cfg), "--state-dir", str(tmp_path / "state"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
        timeout=30,
    )
    return process, json.loads(process.stdout)


def test_capabilities_does_not_initialize_state(tmp_path: Path) -> None:
    result, payload = cli(tmp_path, "capabilities")
    assert result.returncode == 0
    assert payload["schema_version"] == 1
    assert payload["data"]["match_writes_audio"] is False
    assert not (tmp_path / "state").exists()


def test_config_precedence_paths_and_no_rewrite(tmp_path: Path) -> None:
    path = tmp_path / "settings.toml"
    text = '[network]\nworkers = 1\n[output]\ndirectory = "exports"\n[match]\nmin_score = 80\n'
    path.write_text(text)
    settings, resolved = config.load(str(path), {"match": {"min_score": 90}})
    assert resolved == path
    assert settings["match"]["min_score"] == 90
    assert settings["network"]["workers"] == 1
    assert settings["output"]["directory"] == str(tmp_path / "exports")
    assert settings["search"]["sources"] == ["NE", "QM", "KG"]
    assert path.read_text() == text


@pytest.mark.parametrize(
    "text",
    [
        "[unknown]\na = 1",
        "[network]\ntimeout_second = 3",
        "[network]\nworkers = true",
        "[network]\ntimeout_seconds = nan",
        "[network]\ntimeout_seconds = inf",
        "[search]\nsources = []",
        '[search]\nsources = ["NE", "NE"]',
        '[search]\nsources = ["invalid"]',
        "[search]\nsources = [[]]",
        '[output]\nformat = "unknown"',
        '[network]\nproxy = "http://user:secret@host:80"',
        "[network]\nretries = -1",
        "[match]\nmin_score = 101",
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, text: str) -> None:
    path = tmp_path / "invalid.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError):
        config.load(str(path))


def test_invalid_argument_json(tmp_path: Path) -> None:
    process, result = cli(tmp_path, "search", "--query", "demo", "--timeout", "nan")
    assert process.returncode == 2
    assert result["errors"][0]["code"] == "INVALID_ARGUMENT"
    assert not (tmp_path / "state").exists()


def test_config_init_never_overwrites(tmp_path: Path) -> None:
    output = tmp_path / "example.toml"
    first, _ = cli(tmp_path, "config", "init", "--output", str(output))
    assert first.returncode == 0
    before = output.read_bytes()
    second, result = cli(tmp_path, "config", "init", "--output", str(output))
    assert second.returncode == 5
    assert result["errors"][0]["type"] == "FileExistsError"
    assert output.read_bytes() == before
    assert config.load(str(output))[0] == config.DEFAULTS


def test_convert_offline_and_protect_input(tmp_path: Path) -> None:
    source = tmp_path / "测试.lrc"
    source.write_text("[ti:CLI test]\n[00:01.00]An original test line\n[00:03.00]Another original test line\n", encoding="utf-8")
    original = source.read_bytes()
    target = tmp_path / "result.srt"
    result, data = cli(tmp_path, "convert", str(source), "--format", "srt", "--offset-ms", "500", "--output", str(target))
    assert result.returncode == 0, data
    assert "00:00:01,500" in target.read_text()
    assert source.read_bytes() == original
    repeated, _ = cli(tmp_path, "convert", str(source), "--format", "srt", "--output", str(target))
    assert repeated.returncode == 5
    same, _ = cli(tmp_path, "convert", str(source), "--output", str(source), "--overwrite")
    assert same.returncode == 2
    assert source.read_bytes() == original


def test_convert_same_timestamp_bilingual_and_format_config(tmp_path: Path) -> None:
    source = tmp_path / "bilingual.lrc"
    source.write_text("[00:01.00]A test sentence\n[00:01.00]一句测试文本\n[00:03.00]End of test\n[00:03.00]测试结束\n", encoding="utf-8")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[output]\nformat = "srt"\ndirectory = "export"\nlanguages = ["orig", "ts"]\n')
    process, result = cli(tmp_path, "convert", str(source))
    assert process.returncode == 0, result
    rendered = (tmp_path / "export" / "bilingual.srt").read_text()
    assert "A test sentence" in rendered
    assert "一句测试文本" in rendered


def test_audio_duration_preserves_milliseconds(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(1000)
        stream.writeframes(b"\0\0" * 1234)
    process, result = cli(tmp_path, "inspect", str(audio))
    assert process.returncode == 0, result
    assert result["data"]["items"][0]["duration"] == 1234


def test_inspect_partial_failure_and_recursive_config(tmp_path: Path) -> None:
    folder = tmp_path / "album"
    folder.mkdir()
    (folder / "broken.flac").write_text("not audio")
    nested = folder / "disc2"
    nested.mkdir()
    with wave.open(str(nested / "sample.wav"), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(1000)
        stream.writeframes(b"\0\0" * 1000)
    process, result = cli(tmp_path, "inspect", str(folder), "--recursive")
    assert process.returncode == 5
    assert result["status"] == "partial"
    assert len(result["data"]["items"]) == 1
    assert result["errors"][0]["code"] == "READ_FAILED"


def test_timeout_and_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = []

    def timeout(*args: Any, **kwargs: Any) -> NoReturn:
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(args[0], 0.1)

    monkeypatch.setattr(backend.subprocess, "run", timeout)
    settings = copy.deepcopy(config.DEFAULTS)
    settings["network"].update(timeout_seconds=0.1, retries=1)
    result = backend._source_search("NE", "demo", "SONG", 1, settings, tmp_path)  # noqa: SLF001 - deadline boundary
    assert result["error"]["code"] == "TIMEOUT"
    assert len(calls) == 2
    assert calls[0]["timeout"] == 0.1


def test_partial_source_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def request(source: str, *_args: Any) -> dict:
        if source == "NE":
            return {"source": source, "items": [{"source": "NE", "id": "test"}]}
        return {"source": source, "items": [], "error": {"code": "TIMEOUT", "message": "deadline"}}

    monkeypatch.setattr(backend, "_source_search", request)
    result = backend.search("test", "SONG", 1, config.DEFAULTS, tmp_path)
    assert len(result["items"]) == 1
    assert [error["source"] for error in result["errors"]] == ["QM", "KG"]


def test_ranking_exposes_album_and_duration_conflicts() -> None:
    local = {"source": "Local", "title": "Test", "artist": ["Singer"], "album": "Original", "duration": 100000}
    candidate = {**local, "source": "NE", "id": "1", "album": "Compilation", "duration": 101000}
    outside = {**candidate, "id": "2", "duration": 104001}
    ranked = backend.rank(local, [candidate, outside], config.DEFAULTS)
    assert ranked["candidates"][0]["score"] == 100
    assert "album_mismatch" in ranked["candidates"][0]["warnings"]
    assert ranked["candidates"][0]["duration_delta_ms"] == 1000
    assert ranked["candidates"][0]["audio_sync_verified"] is False
    assert ranked["rejected"][0]["reasons"] == ["duration_mismatch"]


def test_match_report_dry_run_and_partial_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("LDDC_CLI_CONFIG", str(tmp_path / "config.toml"))
    (tmp_path / "config.toml").write_text("")

    def matched(*_args: Any) -> dict:
        return {"dry_run": True, "items": [{"status": "no_match", "candidates": []}], "errors": []}

    monkeypatch.setattr(backend, "match", matched)
    output = tmp_path / "report.json"
    assert main(["match", str(tmp_path), "--output", str(output)]) == 3
    response = json.loads(capsys.readouterr().out)
    assert response == json.loads(output.read_text())
    assert response["data"]["dry_run"] is True


def test_atomic_no_clobber(tmp_path: Path) -> None:
    path = tmp_path / "file.txt"
    backend.atomic_write(path, "first")
    with pytest.raises(FileExistsError):
        backend.atomic_write(path, "second")
    assert path.read_text() == "first"
    backend.atomic_write(path, "third", overwrite=True)
    assert path.read_text() == "third"
    assert list(tmp_path.iterdir()) == [path]


def test_config_selection_and_missing_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_config = tmp_path / "environment.toml"
    explicit = tmp_path / "explicit.toml"
    env_config.write_text('[search]\nsources = ["NE"]\n')
    explicit.write_text('[search]\nsources = ["QM"]\n')
    monkeypatch.setenv("LDDC_CLI_CONFIG", str(env_config))
    assert config.load()[0]["search"]["sources"] == ["NE"]
    assert config.load(str(explicit))[0]["search"]["sources"] == ["QM"]
    env_config.unlink()
    with pytest.raises(config.ConfigError, match="does not exist"):
        config.load()
    explicit.write_text("[broken")
    with pytest.raises(config.ConfigError, match="Cannot read TOML"):
        config.load(str(explicit))


def test_runtime_state_isolated_from_gui(tmp_path: Path) -> None:
    state = tmp_path / "isolated"
    script = """
import json
import sys
from pathlib import Path
from LDDC.cli.config import DEFAULTS
from LDDC.cli.runtime import initialize
initialize(Path(sys.argv[1]), DEFAULTS)
from LDDC.common import paths
from PySide6.QtCore import QCoreApplication
print(json.dumps({
    "paths": [str(getattr(paths, name)) for name in ("config_dir", "data_dir", "cache_dir", "log_dir")],
    "application": QCoreApplication.instance().metaObject().className(),
    "gui_loaded": "LDDC.gui" in sys.modules,
}))
"""
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "fake_home")
    process = subprocess.run(  # noqa: S603 - fixed test script and temporary paths
        [sys.executable, "-c", script, str(state)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=10,
    )
    result = json.loads(process.stdout)
    assert result["application"] == "QCoreApplication"
    assert result["gui_loaded"] is False
    assert all(Path(path).parent == state for path in result["paths"])
    assert not (tmp_path / "fake_home" / "Library" / "Preferences" / "LDDC").exists()
    assert not (tmp_path / "fake_home" / ".config" / "LDDC").exists()


def test_hard_timeout_terminates_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    real_run = subprocess.run

    def sleeping_worker(_args: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        return real_run([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)

    monkeypatch.setattr(backend.subprocess, "run", sleeping_worker)
    settings = copy.deepcopy(config.DEFAULTS)
    settings["network"].update(timeout_seconds=0.1, retries=0)
    start = time.monotonic()
    result = backend._source_search("NE", "demo", "SONG", 1, settings, tmp_path)  # noqa: SLF001 - real subprocess deadline
    assert result["error"]["code"] == "TIMEOUT"
    assert time.monotonic() - start < 5


def test_convert_protects_hardlinked_input(tmp_path: Path) -> None:
    source = tmp_path / "original.lrc"
    source.write_text("[00:01.00]Original fixture text\n[00:02.00]Test complete\n")
    destination = tmp_path / "linked.srt"
    destination.hardlink_to(source)
    before = source.read_bytes()
    process, result = cli(tmp_path, "convert", str(source), "--format", "srt", "--output", str(destination), "--overwrite")
    assert process.returncode == 2, result
    assert source.read_bytes() == before


def test_conversion_cannot_create_missing_translation(tmp_path: Path) -> None:
    source = tmp_path / "original.lrc"
    source.write_text("[00:01.00]Original fixture text\n")
    process, result = cli(tmp_path, "convert", str(source), "--languages", "ts")
    assert process.returncode == 2, result
    assert result["errors"][0]["code"] == "INVALID_INPUT"


@pytest.mark.parametrize("suffix", ["(Remaster)", "(2011 Remastered)", "（Remastered 2011）"])
def test_short_title_remaster_retains_version_warning(suffix: str) -> None:
    local = {"source": "Local", "title": "遍路", "artist": ["中島みゆき"], "album": "Example", "duration": 308067}
    candidate = {**local, "source": "NE", "id": "1", "title": "遍路" + suffix, "duration": 308160}
    ranked = backend.rank(local, [candidate], config.DEFAULTS)
    best = ranked["candidates"][0]
    assert best["score"] == 100
    assert best["components"]["title_raw"] < best["components"]["title"]
    assert best["title_comparison"]["candidate"] == "遍路"
    assert "remaster_candidate" in best["warnings"]
    assert best["duration_delta_ms"] == 93
    assert best["audio_sync_verified"] is False


def test_title_comparison_preserves_live_version() -> None:
    local = {"source": "Local", "title": "Test", "artist": ["Singer"]}
    candidate = {**local, "source": "NE", "id": "1", "title": "Test (Live)"}
    ranked = backend.rank(local, [candidate], config.DEFAULTS)
    record = (ranked["candidates"] + ranked["rejected"])[0]
    assert record["title_comparison"]["candidate"] == "Test (Live)"
