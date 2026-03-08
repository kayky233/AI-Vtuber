from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

try:
    from local_settings import SETTINGS as LOCAL_SETTINGS
except Exception:
    LOCAL_SETTINGS: dict[str, Any] = {}


def _read_setting(name: str) -> Any:
    value = os.getenv(name)
    if value is not None and value != "":
        return value
    return LOCAL_SETTINGS.get(name)


def _read_str(name: str, default: str) -> str:
    value = _read_setting(name)
    if value is None:
        return default
    return str(value).strip()


SOURCE_URL = _read_str("SUBTITLE_SOURCE_URL", "")
MPV_PATH = _read_str("SUBTITLE_RELAY_MPV_PATH", str(Path("mpv.exe")))
YTDLP_PATH = _read_str("SUBTITLE_RELAY_YTDLP_PATH", r"E:\KrillinAI-Kay\bin\yt-dlp.exe")
RELAY_AUDIO_DEVICE = _read_str(
    "SUBTITLE_RELAY_AUDIO_DEVICE",
    "wasapi/{f9e303a0-b953-4cdf-abcc-e7a121ad7840}",
)


def discover_livehime_browser_source_url() -> str:
    base = Path(os.getenv("LOCALAPPDATA", "")) / "bililive" / "User Data"
    candidates: list[tuple[float, str]] = []
    for path in base.glob("*/Scene Collection/*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        sources = data.get("sources") or []
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            if source.get("id") != "browser_source":
                continue
            url = str((source.get("settings") or {}).get("url") or "").strip()
            if not url:
                continue
            score = path.stat().st_mtime
            if "youtube.com" in url or "youtu.be" in url:
                score += 10_000_000
            candidates.append((score, url))

    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def resolve_media_url(source_url: str) -> str:
    if not source_url:
        return ""
    if not Path(YTDLP_PATH).exists():
        return source_url

    command = [YTDLP_PATH, "-g", "-f", "bestaudio/best", source_url]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if result.returncode != 0:
        stderr_text = result.stderr.strip()
        if stderr_text:
            print(f"[relay][yt-dlp] {stderr_text.splitlines()[-1]}")
        return source_url

    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate.startswith("http"):
            return candidate
    return source_url


def build_mpv_command(media_url: str) -> list[str]:
    return [
        MPV_PATH,
        "--no-video",
        "--force-window=no",
        f"--audio-device={RELAY_AUDIO_DEVICE}",
        media_url,
    ]


def run() -> int:
    source_url = SOURCE_URL or discover_livehime_browser_source_url()
    if not source_url:
        print("[relay] no source url found")
        return 2

    print(f"[relay] source url: {source_url}")
    print(f"[relay] audio device: {RELAY_AUDIO_DEVICE}")

    while True:
        media_url = resolve_media_url(source_url)
        if not media_url:
            print("[relay] failed to resolve media url")
            time.sleep(5)
            continue

        print(f"[relay] starting playback")
        started_at = time.monotonic()
        process = subprocess.Popen(
            build_mpv_command(media_url),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        try:
            return_code = process.wait()
        except KeyboardInterrupt:
            with contextlib.suppress(Exception):
                process.terminate()
                process.wait(timeout=5)
            print("\n[relay] stopped")
            return 0

        print(f"[relay] mpv exited rc={return_code}")
        if return_code != 0 and process.stderr is not None:
            stderr_text = process.stderr.read().strip()
            if stderr_text:
                print(f"[relay][mpv] {stderr_text.splitlines()[-1]}")
        if time.monotonic() - started_at > 15:
            time.sleep(2)
        else:
            time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(run())
