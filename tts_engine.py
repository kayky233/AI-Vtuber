from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any

import httpx

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


def _read_bool(name: str, default: bool) -> bool:
    value = _read_setting(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _read_int(name: str, default: int) -> int:
    value = _read_setting(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _extract_uuid_or_raw(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        value,
    )
    if match:
        return match.group(1)
    return value

TTS_PROVIDER = _read_str("TTS_PROVIDER", "edge").lower()

EDGE_TTS_VOICE = _read_str("EDGE_TTS_VOICE", "zh-CN-XiaoyiNeural")
EDGE_TTS_RATE = _read_str("EDGE_TTS_RATE", "+0%")
EDGE_TTS_PITCH = _read_str("EDGE_TTS_PITCH", "-1Hz")
EDGE_TTS_VOLUME = _read_str("EDGE_TTS_VOLUME", "+0%")

VOCU_API_KEY = _read_str("VOCU_API_KEY", "")
VOCU_VOICE_ID = _read_str("VOCU_VOICE_ID", "")
VOCU_SHARE_ID = _extract_uuid_or_raw(_read_str("VOCU_SHARE_ID", ""))
VOCU_PROMPT_ID = _read_str("VOCU_PROMPT_ID", "default")
VOCU_PRESET = _read_str("VOCU_PRESET", "v2_balance")
VOCU_FLASH = _read_bool("VOCU_FLASH", False)
VOCU_BREAK_CLONE = _read_bool("VOCU_BREAK_CLONE", True)
VOCU_SHARPEN = _read_bool("VOCU_SHARPEN", False)
VOCU_RANDOMNESS = _read_int("VOCU_RANDOMNESS", 100)
VOCU_STABILITY_BOOST = _read_int("VOCU_STABILITY_BOOST", 1024)
VOCU_PROBABILITY_OPTIMIZATION = _read_int("VOCU_PROBABILITY_OPTIMIZATION", 100)
VOCU_SEED = _read_int("VOCU_SEED", -1)
VOCU_TASK_TIMEOUT_SECONDS = _read_int("VOCU_TASK_TIMEOUT_SECONDS", 90)
VOCU_TASK_POLL_INTERVAL_MS = _read_int("VOCU_TASK_POLL_INTERVAL_MS", 1000)

_VOCU_BASE_URL = "https://v1.vocu.ai/api"
_vocu_client: httpx.AsyncClient | None = None
_cached_vocu_voice_id: str | None = None


def describe_settings() -> str:
    if TTS_PROVIDER == "vocu":
        voice_ref = VOCU_VOICE_ID or VOCU_SHARE_ID or "unset"
        return (
            f"vocu ref={voice_ref} prompt={VOCU_PROMPT_ID} "
            f"preset={VOCU_PRESET} flash={VOCU_FLASH}"
        )
    return (
        f"edge {EDGE_TTS_VOICE} rate={EDGE_TTS_RATE} "
        f"pitch={EDGE_TTS_PITCH} volume={EDGE_TTS_VOLUME}"
    )


def _prepare_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text.strip())
    text = text.replace("...", "……")
    text = re.sub(r"([。！？!?；;…])", r"\1 ", text)
    text = re.sub(r"([，、：:])", r"\1 ", text)
    return text.strip()


def _run_edge_tts(text: str, audio_path: str) -> None:
    command = [
        "edge-tts",
        "--voice",
        EDGE_TTS_VOICE,
        f"--rate={EDGE_TTS_RATE}",
        f"--pitch={EDGE_TTS_PITCH}",
        f"--volume={EDGE_TTS_VOLUME}",
        "--text",
        _prepare_text(text),
        "--write-media",
        audio_path,
    ]
    audio_file = Path(audio_path)
    last_code = 0

    for _ in range(2):
        result = subprocess.run(command, check=False)
        last_code = result.returncode
        if result.returncode == 0 and audio_file.exists() and audio_file.stat().st_size > 0:
            return
        time.sleep(1)

    raise RuntimeError(f"edge-tts exited with code {last_code}")


def _auth_headers() -> dict[str, str]:
    if not VOCU_API_KEY:
        raise RuntimeError("未设置 VOCU_API_KEY")
    return {"Authorization": f"Bearer {VOCU_API_KEY}"}


def _extract_error_message(data: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if data.get("message"):
            return str(data["message"])
    return str(data)


def _has_vocu_api_error(status_code: int, data: Any) -> bool:
    if status_code >= 400:
        return True
    if isinstance(data, dict):
        api_status = data.get("status")
        if isinstance(api_status, int) and api_status >= 400:
            return True
    return False


def _build_vocu_simple_payload(voice_id: str, text: str) -> dict[str, Any]:
    return {
        "voiceId": voice_id,
        "text": text,
        "promptId": VOCU_PROMPT_ID,
        "preset": VOCU_PRESET,
        "randomness": VOCU_RANDOMNESS,
        "stability_boost": VOCU_STABILITY_BOOST,
        "probability_optimization": VOCU_PROBABILITY_OPTIMIZATION,
        "break_clone": VOCU_BREAK_CLONE,
        "sharpen": VOCU_SHARPEN,
        "flash": VOCU_FLASH,
        "stream": False,
        "srt": False,
        "seed": VOCU_SEED,
        "dictionary": [],
    }


def _build_vocu_async_payload(voice_id: str, text: str) -> dict[str, Any]:
    return {
        "contents": [
            {
                "voiceId": voice_id,
                "text": text,
                "promptId": VOCU_PROMPT_ID,
                "preset": VOCU_PRESET,
                "break_clone": VOCU_BREAK_CLONE,
                "language": "auto",
                "vivid": False,
                "emo_switch": [0, 0, 0, 0, 0],
                "speechRate": 1,
                "flash": VOCU_FLASH,
                "seed": VOCU_SEED,
            }
        ],
        "srt": False,
    }


def _extract_vocu_audio_url(data: Any) -> str:
    if not isinstance(data, dict):
        return ""

    contents = (
        data.get("data", {}).get("metadata", {}).get("contents")
        or data.get("metadata", {}).get("contents")
        or []
    )
    for item in contents:
        if isinstance(item, dict) and item.get("audio"):
            return str(item["audio"])
    return ""


async def _get_vocu_client() -> httpx.AsyncClient:
    global _vocu_client
    if _vocu_client is None:
        _vocu_client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))
    return _vocu_client


async def _ensure_vocu_voice_id() -> str:
    global _cached_vocu_voice_id

    if VOCU_VOICE_ID:
        return VOCU_VOICE_ID
    if _cached_vocu_voice_id:
        return _cached_vocu_voice_id
    if not VOCU_SHARE_ID:
        raise RuntimeError("Vocu 需要 VOCU_VOICE_ID，或可导入的 VOCU_SHARE_ID")

    client = await _get_vocu_client()
    response = await client.post(
        f"{_VOCU_BASE_URL}/voice/byShareId",
        headers=_auth_headers(),
        json={"shareId": VOCU_SHARE_ID},
    )
    data = response.json()
    if response.status_code >= 400:
        raise RuntimeError(_extract_error_message(data))

    voice_id = data.get("voiceId") or data.get("data", {}).get("voiceId")
    if not voice_id:
        raise RuntimeError(f"导入 Vocu 角色失败：{data}")
    _cached_vocu_voice_id = str(voice_id)
    return _cached_vocu_voice_id


async def _download_vocu_audio(audio_url: str, audio_path: str) -> None:
    client = await _get_vocu_client()
    audio_response = await client.get(audio_url)
    audio_response.raise_for_status()
    Path(audio_path).write_bytes(audio_response.content)


async def _run_vocu_async_tts(text: str, audio_path: str, voice_id: str) -> None:
    client = await _get_vocu_client()
    create_response = await client.post(
        f"{_VOCU_BASE_URL}/tts/generate",
        headers=_auth_headers(),
        json=_build_vocu_async_payload(voice_id, text),
    )
    create_data = create_response.json()
    if _has_vocu_api_error(create_response.status_code, create_data):
        raise RuntimeError(_extract_error_message(create_data))

    task_id = create_data.get("data", {}).get("id")
    if not task_id:
        raise RuntimeError(f"Vocu 未返回任务 ID：{create_data}")

    deadline = time.monotonic() + max(VOCU_TASK_TIMEOUT_SECONDS, 1)
    last_data: Any = create_data

    while time.monotonic() < deadline:
        await asyncio.sleep(max(VOCU_TASK_POLL_INTERVAL_MS, 200) / 1000)
        detail_response = await client.get(
            f"{_VOCU_BASE_URL}/tts/generate/{task_id}",
            headers=_auth_headers(),
        )
        detail_data = detail_response.json()
        last_data = detail_data
        if _has_vocu_api_error(detail_response.status_code, detail_data):
            raise RuntimeError(_extract_error_message(detail_data))

        task = detail_data.get("data", {})
        status = str(task.get("status", "")).lower()

        if status in {"generated", "completed", "success", "succeeded"}:
            audio_url = _extract_vocu_audio_url(detail_data)
            if not audio_url:
                raise RuntimeError(f"Vocu 未返回音频地址：{detail_data}")
            await _download_vocu_audio(audio_url, audio_path)
            return

        if status in {"failed", "error", "cancelled", "canceled"}:
            raise RuntimeError(_extract_error_message(task))

    raise RuntimeError(f"Vocu 语音生成超时：{last_data}")


async def _run_vocu_tts(text: str, audio_path: str) -> None:
    voice_id = await _ensure_vocu_voice_id()
    if voice_id.startswith("market:"):
        await _run_vocu_async_tts(text, audio_path, voice_id)
        return

    client = await _get_vocu_client()
    response = await client.post(
        f"{_VOCU_BASE_URL}/tts/simple-generate",
        headers=_auth_headers(),
        json=_build_vocu_simple_payload(voice_id, text),
    )
    data = response.json()
    if _has_vocu_api_error(response.status_code, data):
        if response.status_code == 403 or data.get("status") == 403:
            await _run_vocu_async_tts(text, audio_path, voice_id)
            return
        raise RuntimeError(_extract_error_message(data))

    audio_url = data.get("data", {}).get("audio")
    if not audio_url:
        raise RuntimeError(f"Vocu 未返回音频地址：{data}")

    await _download_vocu_audio(audio_url, audio_path)


async def synthesize_to_file(text: str, audio_path: str) -> None:
    if TTS_PROVIDER == "vocu":
        await _run_vocu_tts(text, audio_path)
        return
    await asyncio.to_thread(_run_edge_tts, text, audio_path)


async def close_tts() -> None:
    global _vocu_client
    if _vocu_client is not None:
        await _vocu_client.aclose()
        _vocu_client = None
