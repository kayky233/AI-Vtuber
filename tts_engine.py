from __future__ import annotations

import asyncio
from pathlib import Path
import re
import subprocess
import time
from typing import Any

import httpx

from settings_utils import load_local_settings, read_bool, read_int, read_str

LOCAL_SETTINGS: dict[str, Any] = load_local_settings()


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

TTS_PROVIDER = read_str("TTS_PROVIDER", "edge", LOCAL_SETTINGS).lower()

EDGE_TTS_VOICE = read_str("EDGE_TTS_VOICE", "zh-CN-XiaoyiNeural", LOCAL_SETTINGS)
EDGE_TTS_RATE = read_str("EDGE_TTS_RATE", "+0%", LOCAL_SETTINGS)
EDGE_TTS_PITCH = read_str("EDGE_TTS_PITCH", "-1Hz", LOCAL_SETTINGS)
EDGE_TTS_VOLUME = read_str("EDGE_TTS_VOLUME", "+0%", LOCAL_SETTINGS)
EDGE_TTS_TIMEOUT_SECONDS = max(read_int("EDGE_TTS_TIMEOUT_SECONDS", 25, LOCAL_SETTINGS), 3)

VOCU_API_KEY = read_str("VOCU_API_KEY", "", LOCAL_SETTINGS)
VOCU_VOICE_ID = read_str("VOCU_VOICE_ID", "", LOCAL_SETTINGS)
VOCU_SHARE_ID = _extract_uuid_or_raw(read_str("VOCU_SHARE_ID", "", LOCAL_SETTINGS))
VOCU_PROMPT_ID = read_str("VOCU_PROMPT_ID", "default", LOCAL_SETTINGS)
VOCU_PRESET = read_str("VOCU_PRESET", "v2_balance", LOCAL_SETTINGS)
VOCU_FLASH = read_bool("VOCU_FLASH", False, LOCAL_SETTINGS)
VOCU_BREAK_CLONE = read_bool("VOCU_BREAK_CLONE", True, LOCAL_SETTINGS)
VOCU_SHARPEN = read_bool("VOCU_SHARPEN", False, LOCAL_SETTINGS)
VOCU_RANDOMNESS = read_int("VOCU_RANDOMNESS", 100, LOCAL_SETTINGS)
VOCU_STABILITY_BOOST = read_int("VOCU_STABILITY_BOOST", 1024, LOCAL_SETTINGS)
VOCU_PROBABILITY_OPTIMIZATION = read_int("VOCU_PROBABILITY_OPTIMIZATION", 100, LOCAL_SETTINGS)
VOCU_SEED = read_int("VOCU_SEED", -1, LOCAL_SETTINGS)
VOCU_TASK_TIMEOUT_SECONDS = read_int("VOCU_TASK_TIMEOUT_SECONDS", 90, LOCAL_SETTINGS)
VOCU_TASK_POLL_INTERVAL_MS = read_int("VOCU_TASK_POLL_INTERVAL_MS", 1000, LOCAL_SETTINGS)

_VOCU_BASE_URL = "https://v1.vocu.ai/api"
HTTP_ERROR_THRESHOLD = 400
HTTP_FORBIDDEN = 403
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
        try:
            result = subprocess.run(
                command,
                check=False,
                timeout=EDGE_TTS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            last_code = -1
            print(f"[edge-tts] timeout after {EDGE_TTS_TIMEOUT_SECONDS}s: {error}")
            time.sleep(1)
            continue
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
    if status_code >= HTTP_ERROR_THRESHOLD:
        return True
    if isinstance(data, dict):
        api_status = data.get("status")
        if isinstance(api_status, int) and api_status >= HTTP_ERROR_THRESHOLD:
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
    if response.status_code >= HTTP_ERROR_THRESHOLD:
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
        if response.status_code == HTTP_FORBIDDEN or data.get("status") == HTTP_FORBIDDEN:
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
