"""Small JPEG previews of downloaded images and videos, made with ffmpeg and cached on disk.

A thumbnail is made once per file (keyed by path, size and modification time) and reused after that. ffmpeg runs
are limited to a couple at a time so browsing doesn't swamp a small machine. Folder listings for random picks are
cached briefly, so a page of source cards doesn't re-list every folder on a network share.
"""

import hashlib
import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from bdfrx.manager.candidates import file_kind

WIDTH = 360
FFMPEG_TIMEOUT = 30
LISTING_TTL = 600

_ffmpeg_slots = threading.BoundedSemaphore(2)
_listing_lock = threading.Lock()
_listings: dict[str, tuple[float, list[str]]] = {}


class ThumbnailError(Exception):
    pass


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def cache_path(cache_dir: Path, source: Path) -> Path:
    stat = source.stat()
    key = hashlib.sha1(f"{source}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()  # noqa: S324
    return cache_dir / key[:2] / f"{key}.jpg"


def _ffmpeg(source: Path, target: Path, seek: Optional[float]) -> bool:
    command = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
    if seek:
        command += ["-ss", str(seek)]
    command += ["-i", str(source), "-frames:v", "1", "-vf", f"scale='min({WIDTH},iw)':-2", "-q:v", "5", str(target)]
    try:
        result = subprocess.run(command, capture_output=True, timeout=FFMPEG_TIMEOUT, check=False)  # noqa: S603
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and target.is_file() and target.stat().st_size > 0


def thumbnail(source: Path, cache_dir: Path) -> Path:
    """The cached thumbnail of an image or video file, making it first if needed"""
    kind = file_kind(source.name)
    if kind not in ("image", "video"):
        raise ThumbnailError(f"{source.name} is not an image or video")
    try:
        target = cache_path(cache_dir, source)
    except OSError as e:
        raise ThumbnailError(f"{source.name}: {e}")
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.{os.getpid()}.{threading.get_ident()}.jpg")
    with _ffmpeg_slots:
        # A frame a second in skips fade-ins; very short videos have no such frame, so fall back to the first
        made = _ffmpeg(source, partial, 1.0 if kind == "video" else None) or (
            kind == "video" and _ffmpeg(source, partial, None)
        )
    if not made:
        partial.unlink(missing_ok=True)
        raise ThumbnailError(f"ffmpeg could not make a thumbnail of {source.name}")
    partial.replace(target)
    return target


def media_files(folder: Path) -> list[str]:
    """Names of the images and videos directly in a folder, cached for a few minutes"""
    key = str(folder)
    now = time.monotonic()
    with _listing_lock:
        cached = _listings.get(key)
        if cached and now - cached[0] < LISTING_TTL:
            return cached[1]
    try:
        names = [
            entry.name
            for entry in os.scandir(folder)
            if entry.is_file() and file_kind(entry.name) in ("image", "video")
        ]
    except OSError:
        names = []
    with _listing_lock:
        _listings[key] = (now, names)
    return names


def random_thumbnail(folder: Path, cache_dir: Path, attempts: int = 4) -> Optional[Path]:
    """A thumbnail of a random image or video in the folder, trying a few files if some can't be read"""
    names = media_files(folder)
    for name in random.sample(names, min(attempts, len(names))):
        try:
            return thumbnail(folder / name, cache_dir)
        except ThumbnailError:
            continue
    return None


def forget_listing(folder: Path) -> None:
    with _listing_lock:
        _listings.pop(str(folder), None)
