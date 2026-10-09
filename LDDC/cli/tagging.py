# SPDX-FileCopyrightText: Copyright (C) 2026 wpybtw
# SPDX-License-Identifier: GPL-3.0-only
"""Write lyric tags on a staged copy, verify, then replace the original file."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mutagen import FileType


def load_audio(path: Path) -> FileType:
    from mutagen import File, FileType, MutagenError
    from mutagen._vorbis import VCommentDict
    from mutagen.apev2 import APEv2
    from mutagen.asf import ASFTags
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Tags

    try:
        audio = File(path, easy=False)
    except MutagenError as exc:
        msg = f"Cannot read audio tags: {path}"
        raise OSError(msg) from exc
    if not isinstance(audio, FileType):
        msg = f"Unsupported audio file: {path}"
        raise ValueError(msg)  # noqa: TRY004 - invalid file content, not an invalid Python argument type
    if audio.tags is None:
        audio.add_tags()
    if not isinstance(audio.tags, (ID3, VCommentDict, APEv2, MP4Tags, ASFTags)):
        msg = f"Unsupported audio tags: {path}"
        raise ValueError(msg)  # noqa: TRY004 - unsupported on-disk tags
    return audio


def lyrics_text(audio: FileType) -> str:
    from mutagen.asf import ASFTags
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Tags

    if isinstance(audio.tags, ID3):
        for frame in audio.tags.getall("USLT"):
            if frame.text:
                return frame.text
        return "".join(text for frame in audio.tags.getall("SYLT") for text, _timestamp in frame.text)
    keys = ("©lyr",) if isinstance(audio.tags, MP4Tags) else ("WM/LYRICS", "Lyrics") if isinstance(audio.tags, ASFTags) else ("LYRICS",)
    for key in keys:
        value = audio.tags.get(key)
        if value:
            return "\n".join(str(item) for item in value) if isinstance(value, list) else str(value)
    return ""


@contextmanager
def staged_copy(path: Path) -> Iterator[Path]:
    descriptor, name = tempfile.mkstemp(prefix=".lddc-", suffix=path.suffix, dir=path.parent)
    os.close(descriptor)
    stage = Path(name)
    try:
        shutil.copy2(path, stage)
        yield stage
    finally:
        stage.unlink(missing_ok=True)


def stamp(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def write_tags(path: Path, content: str, settings: dict) -> dict:
    from mutagen import MutagenError
    from mutagen.id3 import ID3

    from LDDC.core.song_info import write_lyrics

    path = path.resolve(strict=True)
    original = stamp(path)
    if lyrics_text(load_audio(path)) and not settings["tags"]["overwrite"]:
        return {"status": "skipped_existing", "path": str(path)}
    if not content.strip():
        msg = "Cannot write empty lyrics"
        raise ValueError(msg)
    backup = path.with_name(path.name + ".lddc.bak") if settings["tags"]["backup"] else None
    with staged_copy(path) as stage:
        # Remove old language/description variants and stale synchronized ID3 frames.
        audio = load_audio(stage)
        if isinstance(audio.tags, ID3):
            audio.tags.delall("USLT")
            audio.tags.delall("SYLT")
            audio.save()
        try:
            write_lyrics(stage, content)
        except MutagenError as exc:
            msg = f"Cannot write audio tags: {path}"
            raise OSError(msg) from exc
        if lyrics_text(load_audio(stage)) != content:
            msg = "Lyric tag read-back verification failed"
            raise OSError(msg)
        with stage.open("rb") as stream:
            os.fsync(stream.fileno())
        if backup:
            if backup.is_symlink() or (backup.exists() and not backup.is_file()):
                msg = f"Backup path is not a regular file: {backup}"
                raise OSError(msg)
            if not backup.exists():
                with staged_copy(path) as saved:
                    with saved.open("rb") as stream:
                        os.fsync(stream.fileno())
                    # No-clobber publication; another writer cannot replace the first backup.
                    os.link(saved, backup)
        if stamp(path) != original:
            msg = "Audio changed during tag writing; original was not replaced"
            raise OSError(msg)
        stage.replace(path)
    return {"status": "written", "path": str(path), "verified": True, "backup": str(backup) if backup else None}
