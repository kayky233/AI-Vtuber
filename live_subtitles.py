from __future__ import annotations

import argparse
from collections import OrderedDict
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable
import warnings
import wave

import httpx
import numpy as np

from llm_bot import _build_providers, _extract_error_text, _extract_text
from settings_utils import (
    load_local_settings,
    read_bool as _shared_read_bool,
    read_float as _shared_read_float,
    read_int as _shared_read_int,
    read_setting as _shared_read_setting,
    read_str as _shared_read_str,
    resolve_path as _shared_resolve_path,
)
from subtitle_audio_relay import discover_livehime_browser_source_url, resolve_media_url

LOCAL_SETTINGS: dict[str, Any] = load_local_settings()

try:
    _original_fromstring = np.fromstring

    def _compat_fromstring(
        string: Any,
        dtype: Any = float,
        count: int = -1,
        sep: str = "",
        *,
        like: Any = None,
    ) -> np.ndarray:
        if sep == "":
            return np.frombuffer(string, dtype=dtype, count=count)
        return _original_fromstring(string, dtype=dtype, count=count, sep=sep)

    np.fromstring = _compat_fromstring  # type: ignore[assignment]
except Exception:
    pass

try:
    import soundcard as sc
except Exception:
    sc = None

PROJECT_DIR = Path(__file__).resolve().parent
TIMESTAMP_LINE_RE = re.compile(r"^\[(\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]$")
HTTP_ERROR_THRESHOLD = 400
WAV_HEADER_BYTES = 44
CHUNK_SETTLE_SECONDS = 1.5
MAX_TRANSLATION_ATTEMPTS = 4


def _read_setting(name: str) -> Any:
    return _shared_read_setting(name, LOCAL_SETTINGS)


def _read_str(name: str, default: str) -> str:
    return _shared_read_str(name, default, LOCAL_SETTINGS)


def _read_bool(name: str, default: bool) -> bool:
    return _shared_read_bool(name, default, LOCAL_SETTINGS)


def _read_int(name: str, default: int) -> int:
    return _shared_read_int(name, default, LOCAL_SETTINGS)


def _read_float(name: str, default: float) -> float:
    return _shared_read_float(name, default, LOCAL_SETTINGS)


def _resolve_path(raw_path: str) -> Path:
    return _shared_resolve_path(raw_path, PROJECT_DIR)


@dataclass(frozen=True)
class SubtitleConfig:
    asr_backend: str
    capture_backend: str
    loopback_speaker: str
    sample_rate: int
    ffmpeg_path: Path
    whisper_path: Path
    whisper_model_dir: Path
    audio_device: str
    source_language: str
    whisper_model: str
    segment_seconds: int
    recognition_window_seconds: int
    recognition_min_window_seconds: int
    history_lines: int
    translate_to_zh: bool
    whisper_device: str
    whisper_compute_type: str
    work_dir: Path
    chunks_dir: Path
    output_path: Path
    origin_output_path: Path
    translated_output_path: Path
    source_url: str
    source_url_file: Path | None
    source_url_reload_seconds: float
    translation_timeout_seconds: float
    transcribe_timeout_seconds: float
    translation_max_tokens: int
    translation_provider_order: tuple[str, ...]
    translation_pending_limit: int
    translation_workers: int
    recognition_holdback_seconds: float
    capture_stall_seconds: float
    capture_restart_cooldown_seconds: float


@dataclass(frozen=True)
class TranscriptionResult:
    text: str
    ok: bool
    segments: tuple["TranscribedSegment", ...] = ()


@dataclass(frozen=True)
class TranscribedSegment:
    start: float
    end: float
    text: str


@dataclass
class SubtitleEntry:
    entry_id: int
    origin: str
    translated: str = ""
    timestamp_text: str = ""


@dataclass(frozen=True)
class TranslationTask:
    entry_id: int
    text: str


@dataclass(frozen=True)
class TranslationResult:
    entry_id: int
    text: str


@dataclass
class TranslationMetrics:
    submitted: int = 0
    dropped: int = 0
    success: int = 0
    failed: int = 0
    retried: int = 0


@dataclass(frozen=True)
class PreparedChunk:
    chunk_index: int
    name: str
    prepared_path: Path
    silent: bool


class AudioCaptureHandle:
    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.thread: threading.Thread | None = None
        self.stop_event: threading.Event | None = None
        self.error_text = ""
        self.exit_code: int | None = None
        self.source_name = ""
        self.source_origin = ""

    def poll(self) -> int | None:
        if self.process is not None:
            return self.process.poll()
        if self.thread is not None and self.thread.is_alive():
            return None
        return self.exit_code

    def read_error(self) -> str:
        if self.process is not None:
            if self.process.stderr is None:
                return ""
            return self.process.stderr.read().decode("utf-8", errors="ignore")
        return self.error_text

    def terminate(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=5)
            if self.process.poll() is None:
                with contextlib.suppress(Exception):
                    self.process.kill()
                    self.process.wait(timeout=5)

        if self.stop_event is not None:
            self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)


def load_config() -> SubtitleConfig:
    work_dir = _resolve_path(_read_str("SUBTITLE_WORK_DIR", "tmp_subtitles"))
    chunks_dir = work_dir / "chunks"
    default_backend = "soundcard_loopback" if sc is not None else "ffmpeg_dshow"
    source_url_file_raw = _read_str("SUBTITLE_SOURCE_URL_FILE", "").strip()
    source_url_file = _resolve_path(source_url_file_raw) if source_url_file_raw else None
    return SubtitleConfig(
        asr_backend=_read_str(
            "SUBTITLE_ASR_BACKEND",
            "persistent_faster_whisper",
        ).lower(),
        capture_backend=_read_str("SUBTITLE_CAPTURE_BACKEND", default_backend).lower(),
        loopback_speaker=_read_str("SUBTITLE_LOOPBACK_SPEAKER", "default"),
        sample_rate=max(_read_int("SUBTITLE_SAMPLE_RATE", 48000), 8000),
        ffmpeg_path=_resolve_path(
            _read_str("SUBTITLE_FFMPEG_PATH", r"E:\KrillinAI-Kay\bin\ffmpeg.exe")
        ),
        whisper_path=_resolve_path(
            _read_str(
                "SUBTITLE_WHISPER_PATH",
                r"E:\KrillinAI-Kay\bin\faster-whisper\Faster-Whisper-XXL\faster-whisper-xxl.exe",
            )
        ),
        whisper_model_dir=_resolve_path(
            _read_str("SUBTITLE_WHISPER_MODEL_DIR", r"E:\KrillinAI-Kay\bin\models")
        ),
        audio_device=_read_str(
            "SUBTITLE_AUDIO_DEVICE",
            "麦克风 (Steam Streaming Microphone)",
        ),
        source_language=_read_str("SUBTITLE_SOURCE_LANGUAGE", "ja"),
        whisper_model=_read_str("SUBTITLE_WHISPER_MODEL", "large-v2"),
        segment_seconds=max(_read_int("SUBTITLE_SEGMENT_SECONDS", 1), 1),
        recognition_window_seconds=max(
            _read_int("SUBTITLE_RECOGNITION_WINDOW_SECONDS", 1),
            1,
        ),
        recognition_min_window_seconds=max(
            _read_int("SUBTITLE_RECOGNITION_MIN_WINDOW_SECONDS", 1),
            1,
        ),
        history_lines=max(_read_int("SUBTITLE_HISTORY_LINES", 2), 1),
        translate_to_zh=_read_bool("SUBTITLE_TRANSLATE_TO_ZH", True),
        whisper_device=_read_str("SUBTITLE_WHISPER_DEVICE", ""),
        whisper_compute_type=_read_str("SUBTITLE_WHISPER_COMPUTE_TYPE", ""),
        work_dir=work_dir,
        chunks_dir=chunks_dir,
        output_path=_resolve_path(_read_str("SUBTITLE_OUTPUT_PATH", "live_subtitle.txt")),
        origin_output_path=_resolve_path(
            _read_str("SUBTITLE_ORIGIN_OUTPUT_PATH", "live_subtitle_origin.txt")
        ),
        translated_output_path=_resolve_path(
            _read_str("SUBTITLE_TRANSLATED_OUTPUT_PATH", "live_subtitle_translated.txt")
        ),
        source_url=_read_str("SUBTITLE_SOURCE_URL", ""),
        source_url_file=source_url_file,
        source_url_reload_seconds=max(
            _read_float("SUBTITLE_SOURCE_URL_RELOAD_SECONDS", 2.0),
            0.5,
        ),
        translation_timeout_seconds=max(
            _read_float("SUBTITLE_TRANSLATION_TIMEOUT_SECONDS", 6.0),
            1.0,
        ),
        transcribe_timeout_seconds=max(
            _read_float("SUBTITLE_TRANSCRIBE_TIMEOUT_SECONDS", 12.0),
            3.0,
        ),
        translation_max_tokens=max(_read_int("SUBTITLE_TRANSLATION_MAX_TOKENS", 80), 16),
        translation_provider_order=tuple(
            item.strip().lower()
            for item in _read_str(
                "SUBTITLE_TRANSLATION_PROVIDER_ORDER",
                "glm,openai,deepseek",
            ).split(",")
            if item.strip()
        ),
        translation_pending_limit=max(_read_int("SUBTITLE_TRANSLATION_PENDING_LIMIT", 2), 1),
        translation_workers=max(_read_int("SUBTITLE_TRANSLATION_WORKERS", 2), 1),
        recognition_holdback_seconds=max(
            _read_float("SUBTITLE_RECOGNITION_HOLDBACK_SECONDS", 1.0),
            0.3,
        ),
        capture_stall_seconds=max(
            _read_float("SUBTITLE_CAPTURE_STALL_SECONDS", 12.0),
            3.0,
        ),
        capture_restart_cooldown_seconds=max(
            _read_float("SUBTITLE_CAPTURE_RESTART_COOLDOWN_SECONDS", 3.0),
            1.0,
        ),
    )


def _sort_providers(
    providers: list[Any],
    preferred_order: tuple[str, ...],
) -> list[Any]:
    if not providers:
        return []

    order_index = {name: index for index, name in enumerate(preferred_order)}
    if order_index:
        filtered = [provider for provider in providers if provider.name in order_index]
        if filtered:
            providers = filtered
    return sorted(
        providers,
        key=lambda provider: (
            order_index.get(provider.name, len(order_index)),
            provider.name,
        ),
    )


class SubtitleTranslator:
    def __init__(self, config: SubtitleConfig) -> None:
        self.providers = _sort_providers(
            _build_providers(),
            config.translation_provider_order,
        )
        self.client = (
            httpx.Client(timeout=httpx.Timeout(config.translation_timeout_seconds))
            if self.providers
            else None
        )
        self.max_tokens = config.translation_max_tokens
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._cache_limit = 256
        self._provider_failures: dict[str, int] = {}
        self._provider_cooldown_until: dict[str, float] = {}

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    def describe(self) -> str:
        return " -> ".join(
            f"{provider.name}({provider.model})" for provider in self.providers
        )

    def _quick_translate(self, text: str) -> str:
        normalized = normalize_transcript_text(text).strip()
        if not normalized:
            return ""

        compact = normalized.replace(" ", "")
        compact = compact.replace("？", "?").replace("！", "!").replace("…", "...")
        quick_map = {
            "え?": "诶？",
            "えっ": "诶？",
            "あ": "啊",
            "あっ": "啊！",
            "あれ": "咦",
            "あれ?": "咦？",
            "うん": "嗯",
            "はい": "好",
            "よし": "好",
            "うわ": "哇",
            "うわっ": "哇！",
            "おい": "喂",
            "おい!": "喂！",
            "せーの": "预备",
            "いけ": "上！",
            "いけ!": "上！",
            "いく": "上了",
            "いくぞ": "上了！",
            "こい": "过来",
            "来い": "过来",
            "きた": "来了",
            "来た": "来了",
            "やばい": "糟了",
            "まずい": "糟了",
            "だめ": "不行",
            "ダメ": "不行",
            "痛い": "疼",
            "待って": "等等",
            "待て": "等等",
            "無理": "不行",
            "逃げろ": "快跑",
            "大丈夫": "没事",
            "大丈夫?": "没事吧？",
            "なんで": "为什么",
            "なんだ": "怎么回事",
            "なんでだ": "为什么啊",
            "いない": "不在",
            "いない!": "不在！",
            "やった": "成了",
            "やった!": "成了！",
            "よわい": "太弱了",
            "つよい": "太强了",
            "こわい": "好可怕",
            "はやく": "快点",
            "www": "笑",
            "ｗｗｗ": "笑",
        }
        return quick_map.get(compact, "")

    def _cache_get(self, text: str) -> str:
        key = normalize_transcript_text(text).strip()
        if not key:
            return ""
        cached = self._cache.get(key, "")
        if cached:
            self._cache.move_to_end(key)
        return cached

    def _cache_put(self, text: str, translated: str) -> None:
        key = normalize_transcript_text(text).strip()
        value = translated.strip()
        if not key or not value:
            return
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)

    def _provider_sequence(self) -> list[Any]:
        now = time.time()
        available = [
            provider
            for provider in self.providers
            if self._provider_cooldown_until.get(provider.name, 0.0) <= now
        ]
        return available or self.providers

    def _mark_provider_failure(self, provider_name: str) -> None:
        failures = self._provider_failures.get(provider_name, 0) + 1
        self._provider_failures[provider_name] = failures
        cooldown_seconds = min(4.0 * failures, 20.0)
        self._provider_cooldown_until[provider_name] = time.time() + cooldown_seconds

    def _mark_provider_success(self, provider_name: str) -> None:
        self._provider_failures.pop(provider_name, None)
        self._provider_cooldown_until.pop(provider_name, None)

    def translate_to_zh(self, text: str) -> str:
        clean_text = polish_subtitle_text(text)
        if not clean_text:
            return ""
        cached = self._cache_get(clean_text)
        if cached:
            return cached
        quick = self._quick_translate(clean_text)
        if quick:
            self._cache_put(clean_text, quick)
            return quick
        if self.client is None:
            return ""

        messages = [
            {
                "role": "system",
                "content": "Translate Japanese speech into concise natural Simplified Chinese subtitles. Return only Chinese subtitle text.",
            },
            {"role": "user", "content": clean_text},
        ]

        last_error = ""
        for provider in self._provider_sequence():
            try:
                response = self.client.post(
                    provider.endpoint,
                    headers={
                        "Authorization": f"Bearer {provider.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": provider.model,
                        "messages": messages,
                        "temperature": 0.0,
                        "stream": False,
                        "max_tokens": self.max_tokens,
                    },
                )
            except httpx.HTTPError as error:
                self._mark_provider_failure(provider.name)
                last_error = str(error)
                continue
            if response.status_code >= HTTP_ERROR_THRESHOLD:
                self._mark_provider_failure(provider.name)
                last_error = _extract_error_text(response)
                continue

            try:
                data = response.json()
            except ValueError:
                self._mark_provider_failure(provider.name)
                last_error = response.text[:200]
                continue

            translated_text = _extract_text(data).strip()
            if translated_text:
                self._mark_provider_success(provider.name)
                self._cache_put(clean_text, translated_text)
                return translated_text

        if last_error:
            print(f"[subtitle][translate] {last_error}")
        return ""


class TranslationDispatcher:
    def __init__(
        self,
        translator: SubtitleTranslator,
        pending_limit: int,
        worker_count: int,
        result_handler: Callable[[TranslationResult], None] | None = None,
    ) -> None:
        self.translator = translator
        self.pending_limit = max(pending_limit, 1)
        self.worker_count = max(worker_count, 1)
        self.result_handler = result_handler
        self._condition = threading.Condition()
        self._pending: list[TranslationTask] = []
        self._results: list[TranslationResult] = []
        self._pending_ids: set[int] = set()
        self._active_ids: set[int] = set()
        self._stopped = False
        self._threads: list[threading.Thread] = []
        if self.enabled:
            for index in range(self.worker_count):
                thread = threading.Thread(
                    target=self._run,
                    name=f"subtitle-translate-{index + 1}",
                    daemon=True,
                )
                self._threads.append(thread)
                thread.start()

    @property
    def enabled(self) -> bool:
        return self.translator.client is not None

    def submit(self, entry_id: int, text: str) -> bool:
        if not self.enabled:
            return False

        with self._condition:
            if entry_id in self._pending_ids or entry_id in self._active_ids:
                return False
            while len(self._pending) >= self.pending_limit:
                dropped = self._pending.pop(0)
                self._pending_ids.discard(dropped.entry_id)
                print(
                    f"[subtitle][translate] backlog full; dropped stale entry {dropped.entry_id}"
                )
            self._pending.append(TranslationTask(entry_id=entry_id, text=text))
            self._pending_ids.add(entry_id)
            self._condition.notify()
            return True

    def drain_results(self) -> list[TranslationResult]:
        with self._condition:
            if not self._results:
                return []
            results = self._results[:]
            self._results.clear()
            return results

    def close(self) -> None:
        if not self.enabled:
            return

        with self._condition:
            self._stopped = True
            self._condition.notify_all()

        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=10)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._stopped and not self._pending:
                    self._condition.wait(timeout=0.5)
                if self._stopped:
                    return
                task = self._pending.pop()
                self._pending_ids.discard(task.entry_id)
                self._active_ids.add(task.entry_id)

            translated_text = self.translator.translate_to_zh(task.text)
            result = TranslationResult(entry_id=task.entry_id, text=translated_text)
            with self._condition:
                self._active_ids.discard(task.entry_id)
            if self.result_handler is not None:
                try:
                    self.result_handler(result)
                except Exception as error:
                    print(f"[subtitle][translate] result handler failed: {error}")
                continue

            with self._condition:
                self._results.append(result)


def ensure_paths(config: SubtitleConfig) -> None:
    config.work_dir.mkdir(exist_ok=True)
    config.chunks_dir.mkdir(exist_ok=True)
    (config.work_dir / "prepared").mkdir(exist_ok=True)
    for path in (
        config.output_path,
        config.origin_output_path,
        config.translated_output_path,
    ):
        parent = path.parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)


def validate_config(config: SubtitleConfig) -> bool:
    missing_paths: list[tuple[str, Path]] = []
    for label, path in (("model_dir", config.whisper_model_dir),):
        if not path.exists():
            missing_paths.append((label, path))
    if config.asr_backend == "whisper_cli" and not config.whisper_path.exists():
        missing_paths.append(("whisper", config.whisper_path))

    if config.capture_backend in {"ffmpeg_dshow", "ffmpeg_source_url"} and not config.ffmpeg_path.exists():
        missing_paths.append(("ffmpeg", config.ffmpeg_path))

    if config.capture_backend == "soundcard_loopback" and sc is None:
        print("[subtitle][config] soundcard backend selected but soundcard is unavailable")
        return False

    if config.capture_backend not in {
        "soundcard_loopback",
        "ffmpeg_dshow",
        "ffmpeg_source_url",
    }:
        print(f"[subtitle][config] unsupported capture backend: {config.capture_backend}")
        return False
    if config.asr_backend not in {"persistent_faster_whisper", "whisper_cli"}:
        print(f"[subtitle][config] unsupported asr backend: {config.asr_backend}")
        return False

    if not missing_paths:
        return True

    for label, path in missing_paths:
        print(f"[subtitle][config] missing {label}: {path}")
    return False


def clear_old_chunks(config: SubtitleConfig) -> None:
    for pattern in (
        "chunk_*.wav",
        "chunk_*.json",
        "chunk_*.srt",
        "chunk_*.txt",
        "chunk_*.vtt",
        "chunk_*.tsv",
        "chunk_*.lrc",
    ):
        for path in config.chunks_dir.glob(pattern):
            with contextlib.suppress(OSError):
                path.unlink()
    prepared_dir = config.work_dir / "prepared"
    for path in prepared_dir.glob("*"):
        with contextlib.suppress(OSError):
            path.unlink()


def cleanup_stale_worker_processes(config: SubtitleConfig) -> None:
    if os.name != "nt":
        return

    script = """
$names = @('ffmpeg', 'faster-whisper-xxl')
foreach ($name in $names) {
    $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
    foreach ($proc in $procs) {
        try {
            Stop-Process -Id $proc.Id -Force -ErrorAction Stop
            Write-Output ("[subtitle][cleanup] killed {0} pid={1}" -f $proc.ProcessName, $proc.Id)
        } catch {
        }
    }
}
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=12,
        )
    except Exception as error:
        print(f"[subtitle][cleanup] failed: {error}")
        return

    for line in result.stdout.splitlines():
        line = line.strip()
        if line:
            print(line)
    if result.returncode != 0:
        stderr_text = result.stderr.strip()
        if stderr_text:
            print(f"[subtitle][cleanup] {stderr_text}")


def list_audio_devices(config: SubtitleConfig) -> int:
    printed_any = False

    if sc is not None:
        printed_any = True
        default_speaker = sc.default_speaker()
        print("[soundcard speakers]")
        for speaker in sc.all_speakers():
            marker = "*" if default_speaker is not None and speaker.id == default_speaker.id else " "
            print(f" {marker} {speaker.name}")
        print("[soundcard microphones]")
        for microphone in sc.all_microphones(include_loopback=True):
            print(f"   {microphone.name}")

    if config.ffmpeg_path.exists():
        printed_any = True
        print("[ffmpeg dshow devices]")
        command = [
            str(config.ffmpeg_path),
            "-hide_banner",
            "-list_devices",
            "true",
            "-f",
            "dshow",
            "-i",
            "dummy",
        ]
        result = subprocess.run(command, capture_output=True)
        sys.stderr.write(result.stderr.decode("utf-8", errors="ignore"))
        return 0

    if not printed_any:
        print("[subtitle][config] no capture backends available")
        return 2

    return 0


def resolve_loopback_microphone(config: SubtitleConfig) -> tuple[Any, str]:
    if sc is None:
        raise RuntimeError("soundcard is unavailable")

    speaker_name = config.loopback_speaker.strip()
    if not speaker_name or speaker_name.lower() == "default":
        speaker = sc.default_speaker()
        if speaker is None:
            raise RuntimeError("no default speaker available")
        return sc.get_microphone(id=str(speaker.id), include_loopback=True), speaker.name

    speakers = list(sc.all_speakers())
    for speaker in speakers:
        if speaker.name == speaker_name:
            return sc.get_microphone(id=str(speaker.id), include_loopback=True), speaker.name

    lowered_name = speaker_name.lower()
    for speaker in speakers:
        if lowered_name in speaker.name.lower():
            return sc.get_microphone(id=str(speaker.id), include_loopback=True), speaker.name

    available = ", ".join(speaker.name for speaker in speakers)
    raise RuntimeError(f"loopback speaker not found: {speaker_name} (available: {available})")


def write_wave_file(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    mono_samples = samples
    if mono_samples.ndim > 1:
        mono_samples = mono_samples.mean(axis=1)
    clipped = np.clip(mono_samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())


def start_soundcard_capture(config: SubtitleConfig) -> AudioCaptureHandle:
    handle = AudioCaptureHandle()
    handle.stop_event = threading.Event()
    microphone, speaker_name = resolve_loopback_microphone(config)
    handle.source_name = speaker_name

    def worker() -> None:
        chunk_frames = config.sample_rate * config.segment_seconds
        block_frames = max(min(config.sample_rate // 5, chunk_frames), 2048)
        buffered: list[np.ndarray] = []
        buffered_frames = 0
        chunk_index = 1

        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="data discontinuity in recording",
                )
                with microphone.recorder(samplerate=config.sample_rate) as recorder:
                    while handle.stop_event is not None and not handle.stop_event.is_set():
                        samples = recorder.record(numframes=block_frames)
                        samples_array = np.asarray(samples, dtype=np.float32)
                        if samples_array.size == 0:
                            continue

                        buffered.append(samples_array)
                        buffered_frames += samples_array.shape[0]

                        if buffered_frames < chunk_frames:
                            continue

                        merged = np.concatenate(buffered, axis=0)
                        current_chunk = merged[:chunk_frames]
                        remainder = merged[chunk_frames:]
                        output_path = config.chunks_dir / f"chunk_{chunk_index:06d}.wav"
                        write_wave_file(output_path, current_chunk, config.sample_rate)
                        chunk_index += 1

                        buffered = [remainder] if remainder.size else []
                        buffered_frames = remainder.shape[0] if remainder.size else 0

                if buffered_frames >= config.sample_rate and buffered:
                    merged = np.concatenate(buffered, axis=0)
                    output_path = config.chunks_dir / f"chunk_{chunk_index:06d}.wav"
                    write_wave_file(output_path, merged, config.sample_rate)

            handle.exit_code = 0
        except Exception as error:
            handle.error_text = str(error)
            handle.exit_code = 1

    handle.thread = threading.Thread(target=worker, name="subtitle-capture", daemon=True)
    handle.thread.start()
    return handle


def start_ffmpeg_capture(config: SubtitleConfig) -> AudioCaptureHandle:
    handle = AudioCaptureHandle()
    handle.source_name = config.audio_device
    output_pattern = str(config.chunks_dir / "chunk_%06d.wav")
    command = [
        str(config.ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "dshow",
        "-i",
        f"audio={config.audio_device}",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "segment",
        "-segment_time",
        str(config.segment_seconds),
        "-reset_timestamps",
        "1",
        output_pattern,
    ]
    handle.process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return handle


def _read_source_url_from_file(source_url_file: Path | None) -> str:
    if source_url_file is None:
        return ""
    try:
        content = source_url_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception:
        return ""
    content = content.lstrip("\ufeff")
    for raw_line in content.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        return line.strip("\"'")
    return ""


def resolve_capture_source_url(
    config: SubtitleConfig,
    *,
    allow_discovery: bool,
) -> tuple[str, str]:
    file_url = _read_source_url_from_file(config.source_url_file)
    if file_url:
        return file_url, "source_url_file"

    static_url = config.source_url.strip()
    if static_url:
        return static_url, "source_url_setting"

    if allow_discovery:
        discovered_url = discover_livehime_browser_source_url()
        if discovered_url:
            return discovered_url, "livehime_discovery"
    return "", ""


def start_source_url_capture(config: SubtitleConfig) -> AudioCaptureHandle:
    handle = AudioCaptureHandle()
    source_url, source_origin = resolve_capture_source_url(config, allow_discovery=True)
    if not source_url:
        handle.error_text = "no subtitle source url found"
        handle.exit_code = 1
        return handle

    media_url = resolve_media_url(source_url)
    if not media_url:
        handle.error_text = f"failed to resolve subtitle source url: {source_url}"
        handle.exit_code = 1
        return handle

    handle.source_name = source_url
    handle.source_origin = source_origin
    output_pattern = str(config.chunks_dir / "chunk_%06d.wav")
    command = [
        str(config.ffmpeg_path),
        "-hide_banner",
        "-loglevel",
        "error",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "5",
        "-re",
        "-i",
        media_url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "segment",
        "-segment_time",
        str(config.segment_seconds),
        "-reset_timestamps",
        "1",
        output_pattern,
    ]
    handle.process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return handle


def start_audio_capture(config: SubtitleConfig) -> AudioCaptureHandle:
    if config.capture_backend == "soundcard_loopback":
        return start_soundcard_capture(config)
    if config.capture_backend == "ffmpeg_source_url":
        return start_source_url_capture(config)
    return start_ffmpeg_capture(config)


def build_whisper_command(
    config: SubtitleConfig,
    audio_path: Path,
    *,
    output_dir: Path | None = None,
    chunk_length_seconds: int | None = None,
) -> list[str]:
    resolved_output_dir = output_dir or audio_path.parent
    command = [
        str(config.whisper_path),
        "--model_dir",
        str(config.whisper_model_dir),
        "--model",
        config.whisper_model,
        "--one_word",
        "2",
        "--task",
        "transcribe",
        "--language",
        config.source_language,
        "--output_dir",
        str(resolved_output_dir),
        "--output_format",
        "json",
        "--vad_filter",
        "True",
        "--chunk_length",
        str(chunk_length_seconds or config.segment_seconds),
        str(audio_path),
    ]
    if config.whisper_device:
        command.extend(["--device", config.whisper_device])
    if config.whisper_compute_type:
        command.extend(["--compute_type", config.whisper_compute_type])
    return command


def resolve_whisper_model_reference(config: SubtitleConfig) -> str:
    model_name = config.whisper_model.strip()
    explicit_path = Path(model_name)
    if explicit_path.exists():
        return str(explicit_path)

    candidates = (
        config.whisper_model_dir / f"faster-whisper-{model_name}",
        config.whisper_model_dir / model_name,
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return model_name


def load_faster_whisper_model_class() -> type[Any]:
    sentinel = object()
    previous_torch = sys.modules.get("torch", sentinel)
    sys.modules["torch"] = None
    try:
        from faster_whisper import WhisperModel
    finally:
        if previous_torch is sentinel:
            sys.modules.pop("torch", None)
        else:
            sys.modules["torch"] = previous_torch
    return WhisperModel


def transcribe_chunk_cli(
    config: SubtitleConfig,
    audio_path: Path,
    *,
    chunk_length_seconds: int | None = None,
) -> TranscriptionResult:
    output_dir = audio_path.parent
    command = build_whisper_command(
        config,
        audio_path,
        output_dir=output_dir,
        chunk_length_seconds=chunk_length_seconds,
    )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=config.transcribe_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        print(
            "[subtitle][whisper] timeout "
            f"after {config.transcribe_timeout_seconds:.1f}s: {audio_path.name}"
        )
        cleanup_chunk_files(audio_path)
        return TranscriptionResult(text="", ok=False, segments=())
    json_path = output_dir / f"{audio_path.stem}.json"
    stderr_text = result.stderr.decode("utf-8", errors="ignore").strip()
    stdout_text = result.stdout.decode("utf-8", errors="ignore").strip()
    detail = stderr_text or stdout_text

    if result.returncode != 0 and not json_path.exists():
        if detail:
            print(f"[subtitle][whisper] {detail[-300:]}")
        return TranscriptionResult(text="", ok=False, segments=())

    if not json_path.exists():
        return TranscriptionResult(text="", ok=False, segments=())

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as error:
        print(f"[subtitle][json] {error}")
        return TranscriptionResult(text="", ok=False, segments=())

    segments = data.get("segments") or []
    full_text = str(data.get("text") or "").strip()
    text_parts: list[str] = []
    parsed_segments: list[TranscribedSegment] = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        text = str(segment.get("text") or "").strip()
        if text:
            text_parts.append(text)
            try:
                start = float(segment.get("start") or 0.0)
            except (TypeError, ValueError):
                start = 0.0
            try:
                end = float(segment.get("end") or start)
            except (TypeError, ValueError):
                end = start
            if end < start:
                end = start
            parsed_segments.append(TranscribedSegment(start=start, end=end, text=text))
    merged_text = " ".join(text_parts).strip()
    if not parsed_segments and full_text:
        parsed_segments.append(
            TranscribedSegment(
                start=0.0,
                end=float(chunk_length_seconds or config.segment_seconds),
                text=full_text,
            )
        )
    return TranscriptionResult(
        text=merged_text or full_text,
        ok=True,
        segments=tuple(parsed_segments),
    )


class SubtitleAsrTranscriber:
    def __init__(self, config: SubtitleConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._persistent_model: Any | None = None
        self._backend = "whisper_cli"
        self._model_reference = ""
        self._load_error = ""

        if config.asr_backend == "persistent_faster_whisper":
            try:
                self._initialize_persistent_backend()
            except Exception as error:
                self._load_error = str(error)
                print(
                    "[subtitle][asr] persistent_faster_whisper unavailable; "
                    f"falling back to whisper_cli: {error}"
                )

    def _initialize_persistent_backend(self) -> None:
        WhisperModel = load_faster_whisper_model_class()
        model_reference = resolve_whisper_model_reference(self.config)
        kwargs: dict[str, Any] = {}
        if self.config.whisper_device:
            kwargs["device"] = self.config.whisper_device
        if self.config.whisper_compute_type:
            kwargs["compute_type"] = self.config.whisper_compute_type
        self._persistent_model = WhisperModel(model_reference, **kwargs)
        self._backend = "persistent_faster_whisper"
        self._model_reference = model_reference

    def describe(self) -> str:
        if self._backend == "persistent_faster_whisper":
            return (
                "persistent_faster_whisper("
                f"{Path(self._model_reference).name or self._model_reference}"
                ")"
            )
        return "whisper_cli"

    def transcribe(
        self,
        audio_path: Path,
        *,
        chunk_length_seconds: int | None = None,
    ) -> TranscriptionResult:
        if self._backend == "persistent_faster_whisper" and self._persistent_model is not None:
            try:
                return self._transcribe_persistent(
                    audio_path,
                    chunk_length_seconds=chunk_length_seconds,
                )
            except Exception as error:
                print(f"[subtitle][asr] persistent failed: {error}")
        return transcribe_chunk_cli(
            self.config,
            audio_path,
            chunk_length_seconds=chunk_length_seconds,
        )

    def _transcribe_persistent(
        self,
        audio_path: Path,
        *,
        chunk_length_seconds: int | None = None,
    ) -> TranscriptionResult:
        chunk_length = chunk_length_seconds or self.config.segment_seconds
        with self._lock:
            segments, _info = self._persistent_model.transcribe(
                str(audio_path),
                language=self.config.source_language or None,
                task="transcribe",
                beam_size=1,
                best_of=1,
                condition_on_previous_text=False,
                without_timestamps=False,
                word_timestamps=False,
                vad_filter=True,
                chunk_length=chunk_length,
            )
            decoded_segments = list(segments)

        text_parts: list[str] = []
        parsed_segments: list[TranscribedSegment] = []
        for segment in decoded_segments:
            text = normalize_transcript_text(getattr(segment, "text", ""))
            if not text:
                continue
            text_parts.append(text)
            try:
                start = float(getattr(segment, "start", 0.0) or 0.0)
            except (TypeError, ValueError):
                start = 0.0
            try:
                end = float(getattr(segment, "end", start) or start)
            except (TypeError, ValueError):
                end = start
            if end < start:
                end = start
            parsed_segments.append(
                TranscribedSegment(start=start, end=end, text=text)
            )

        merged_text = normalize_transcript_text(" ".join(text_parts))
        return TranscriptionResult(
            text=merged_text,
            ok=bool(merged_text or parsed_segments),
            segments=tuple(parsed_segments),
        )

    def close(self) -> None:
        self._persistent_model = None


def write_text(path: Path, text: str) -> None:
    parent = path.parent
    if str(parent) not in {"", "."}:
        parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            temp_path.write_text(text, encoding="utf-8")
            temp_path.replace(path)
            return
        except OSError as error:
            last_error = error
            time.sleep(0.05 * (attempt + 1))

    with contextlib.suppress(OSError):
        temp_path.unlink()

    try:
        path.write_text(text, encoding="utf-8")
        return
    except OSError as error:
        last_error = error

    if last_error is not None:
        raise last_error


def format_timestamp_text(timestamp_value: float | None = None) -> str:
    if timestamp_value is None:
        timestamp_value = time.time()
    return time.strftime("%m-%d %H:%M:%S", time.localtime(timestamp_value))


def split_timestamped_block(block: str) -> tuple[str, list[str]]:
    lines = [line.rstrip() for line in block.splitlines() if line.strip()]
    if not lines:
        return "", []

    match = TIMESTAMP_LINE_RE.fullmatch(lines[0].strip())
    if match is None:
        return "", lines
    return match.group(1), lines[1:]


def render_block(lines: list[str], timestamp_text: str = "") -> str:
    payload = [line.strip() for line in lines if line.strip()]
    if not payload:
        return ""
    if timestamp_text:
        return "\n".join([f"[{timestamp_text}]"] + payload)
    return "\n".join(payload)


def cleanup_chunk_files(audio_path: Path) -> None:
    for suffix in (".wav", ".json", ".srt", ".txt", ".vtt", ".tsv", ".lrc"):
        with contextlib.suppress(OSError):
            audio_path.with_suffix(suffix).unlink()


def prepare_chunk_audio(config: SubtitleConfig, audio_path: Path) -> Path | None:
    prepared_dir = config.work_dir / "prepared"
    prepared_path = prepared_dir / audio_path.name
    with contextlib.suppress(OSError):
        prepared_path.unlink()

    if config.ffmpeg_path.exists():
        command = [
            str(config.ffmpeg_path),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(prepared_path),
        ]
        result = subprocess.run(command, capture_output=True)
        if result.returncode != 0:
            with contextlib.suppress(OSError):
                prepared_path.unlink()
            return None
    else:
        try:
            shutil.copyfile(audio_path, prepared_path)
        except OSError:
            return None

    try:
        if prepared_path.stat().st_size <= WAV_HEADER_BYTES:
            with contextlib.suppress(OSError):
                prepared_path.unlink()
            return None
    except OSError:
        return None

    return prepared_path


def parse_chunk_index(path: Path) -> int:
    try:
        return int(path.stem.split("_")[-1])
    except (TypeError, ValueError):
        return 0


@dataclass
class ChunkCollector:
    next_index: int = 0
    last_rescan_at: float = 0.0
    rescan_interval_seconds: float = 4.0

    def reset(self) -> None:
        self.next_index = 0
        self.last_rescan_at = 0.0

    def collect_candidates(
        self,
        chunks_dir: Path,
        retry_after: dict[str, float],
    ) -> list[Path]:
        now = time.time()
        candidates: list[Path] = []

        while True:
            path = chunks_dir / f"chunk_{self.next_index:06d}.wav"
            if not path.exists():
                break
            if retry_after.get(path.name, 0.0) > now:
                break
            candidates.append(path)
            self.next_index += 1

        if candidates:
            return candidates

        if now - self.last_rescan_at < self.rescan_interval_seconds:
            return []

        self.last_rescan_at = now
        for path in sorted(chunks_dir.glob("chunk_*.wav")):
            if retry_after.get(path.name, 0.0) > now:
                continue
            chunk_index = parse_chunk_index(path)
            if chunk_index < self.next_index:
                continue
            if chunk_index > self.next_index:
                break
            candidates.append(path)
            self.next_index = chunk_index + 1
        return candidates


def merge_prepared_chunks(
    config: SubtitleConfig,
    chunks: list[PreparedChunk],
    window_index: int,
) -> Path | None:
    if not chunks:
        return None

    samples_list: list[np.ndarray] = []
    sample_rate: int | None = None
    for chunk in chunks:
        try:
            with wave.open(str(chunk.prepared_path), "rb") as wav_file:
                current_rate = wav_file.getframerate()
                frames = wav_file.readframes(wav_file.getnframes())
        except Exception:
            return None

        if not frames:
            continue
        if sample_rate is None:
            sample_rate = current_rate
        elif current_rate != sample_rate:
            return None

        chunk_samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if chunk_samples.size:
            samples_list.append(chunk_samples)

    if not samples_list or sample_rate is None:
        return None

    merged_samples = np.concatenate(samples_list)
    window_path = config.work_dir / "prepared" / f"window_{window_index:06d}.wav"
    with contextlib.suppress(OSError):
        window_path.unlink()
    write_wave_file(window_path, merged_samples, sample_rate)
    return window_path


def normalize_transcript_text(text: str) -> str:
    return " ".join(text.strip().split())


def longest_overlap_suffix_prefix(previous: str, current: str) -> int:
    max_len = min(len(previous), len(current))
    for length in range(max_len, 0, -1):
        if previous[-length:] == current[:length]:
            return length
    return 0


def trim_incremental_text(text: str) -> str:
    return text.lstrip(" \t\r\n,，。！？!?:：、")


def _dedupe_repeated_ngrams(tokens: list[str], max_ngram: int = 4) -> list[str]:
    if len(tokens) < 2:
        return tokens

    collapsed = True
    current = tokens[:]
    while collapsed:
        collapsed = False
        next_tokens: list[str] = []
        index = 0
        while index < len(current):
            matched = False
            max_size = min(max_ngram, (len(current) - index) // 2)
            for size in range(max_size, 0, -1):
                left = current[index : index + size]
                right = current[index + size : index + size * 2]
                if left != right:
                    continue
                next_tokens.extend(left)
                index += size * 2
                collapsed = True
                matched = True
                break
            if not matched:
                next_tokens.append(current[index])
                index += 1
        current = next_tokens
    return current


def polish_subtitle_text(text: str) -> str:
    normalized = normalize_transcript_text(text)
    if not normalized:
        return ""

    tokens = [token for token in normalized.split(" ") if token]
    if not tokens:
        return ""

    tokens = _dedupe_repeated_ngrams(tokens, max_ngram=4)
    return normalize_transcript_text(" ".join(tokens))


def extract_incremental_text(previous: str, current: str) -> str:
    previous_text = normalize_transcript_text(previous)
    current_text = normalize_transcript_text(current)
    if not current_text:
        return ""
    if not previous_text:
        return current_text
    if current_text == previous_text or current_text in previous_text:
        return ""
    if previous_text in current_text:
        return trim_incremental_text(current_text[len(previous_text) :].strip())

    overlap = longest_overlap_suffix_prefix(previous_text, current_text)
    if overlap > 0:
        return trim_incremental_text(current_text[overlap:].strip())

    if len(current_text) <= len(previous_text):
        return ""
    return current_text


def render_history(lines: list[str], keep: int) -> str:
    filtered = [line.strip() for line in lines if line.strip()]
    return "\n\n".join(filtered[-keep:])


def read_history(path: Path, keep: int) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    chunks = [chunk.strip() for chunk in text.split("\n\n") if chunk.strip()]
    return chunks[-keep:]


def read_subtitle_entries(config: SubtitleConfig) -> list[SubtitleEntry]:
    display_history = read_history(config.output_path, config.history_lines)
    entries: list[SubtitleEntry] = []

    if display_history:
        for index, block in enumerate(display_history, start=1):
            timestamp_text, raw_lines = split_timestamped_block(block)
            lines = [line.strip() for line in raw_lines if line.strip()]
            if not lines:
                continue
            if timestamp_text:
                translated = lines[0] if len(lines) >= 2 else ""
                origin = "\n".join(lines[1:] if translated else lines).strip()
            else:
                origin = lines[0]
                translated = "\n".join(lines[1:]).strip()
            entries.append(
                SubtitleEntry(
                    entry_id=index,
                    origin=origin,
                    translated=translated,
                    timestamp_text=timestamp_text,
                )
            )
        return entries[-config.history_lines :]

    origin_history = read_history(config.origin_output_path, config.history_lines)
    translated_history = read_history(config.translated_output_path, config.history_lines)
    max_len = max(len(origin_history), len(translated_history))
    for index in range(max_len):
        origin_timestamp, origin_lines = (
            split_timestamped_block(origin_history[index])
            if index < len(origin_history)
            else ("", [])
        )
        translated_timestamp, translated_lines = (
            split_timestamped_block(translated_history[index])
            if index < len(translated_history)
            else ("", [])
        )
        origin = "\n".join(line.strip() for line in origin_lines if line.strip()).strip()
        translated = "\n".join(
            line.strip() for line in translated_lines if line.strip()
        ).strip()
        if not origin and not translated:
            continue
        entries.append(
            SubtitleEntry(
                entry_id=len(entries) + 1,
                origin=origin or translated,
                translated=translated if origin else "",
                timestamp_text=origin_timestamp or translated_timestamp,
            )
        )
    return entries[-config.history_lines :]


def render_entry(entry: SubtitleEntry) -> str:
    origin = entry.origin.strip()
    translated = entry.translated.strip()
    if origin and translated:
        return render_block([translated, origin], entry.timestamp_text)
    return render_block([translated or origin], entry.timestamp_text)


def render_origin_entry(entry: SubtitleEntry) -> str:
    origin = entry.origin.strip()
    if not origin:
        return ""
    return render_block([origin], entry.timestamp_text)


def render_translated_entry(entry: SubtitleEntry) -> str:
    translated = entry.translated.strip()
    if not translated:
        return ""
    return render_block([translated], entry.timestamp_text)


def write_entry_outputs(config: SubtitleConfig, entries: list[SubtitleEntry]) -> None:
    recent_entries = [
        entry
        for entry in entries[-config.history_lines :]
        if entry.origin.strip() or entry.translated.strip()
    ]
    for entry in recent_entries:
        if not entry.timestamp_text:
            entry.timestamp_text = format_timestamp_text()
    display_history = [render_entry(entry) for entry in recent_entries if render_entry(entry)]
    origin_history = [
        render_origin_entry(entry) for entry in recent_entries if entry.origin.strip()
    ]
    translated_history = [
        render_translated_entry(entry)
        for entry in recent_entries
        if entry.translated.strip()
    ]
    write_text(config.output_path, render_history(display_history, config.history_lines))
    write_text(
        config.origin_output_path,
        render_history(origin_history, config.history_lines),
    )
    write_text(
        config.translated_output_path,
        render_history(translated_history, config.history_lines),
    )


def measure_audio_level(audio_path: Path) -> tuple[float, float] | None:
    try:
        with wave.open(str(audio_path), "rb") as wav_file:
            frames = wav_file.readframes(wav_file.getnframes())
    except Exception:
        return None

    if not frames:
        return 0.0, 0.0

    samples = np.frombuffer(frames, dtype=np.int16)
    if samples.size == 0:
        return 0.0, 0.0

    float_samples = samples.astype(np.float32) / 32768.0
    peak = float(np.max(np.abs(float_samples)))
    rms = float(np.sqrt(np.mean(float_samples**2)))
    return peak, rms


def translation_retry_delay(attempt: int) -> float:
    normalized_attempt = max(attempt, 1)
    return min(0.8 * (2 ** (normalized_attempt - 1)), 8.0)


def transcribe_window_incremental(
    config: SubtitleConfig,
    asr_transcriber: SubtitleAsrTranscriber,
    window_chunks: list[PreparedChunk],
    *,
    direct_chunk_mode: bool,
    last_origin_text: str,
    last_emitted_segment_end: float,
) -> tuple[TranscriptionResult, str, float]:
    if direct_chunk_mode:
        active_chunk = window_chunks[-1]
        result = asr_transcriber.transcribe(
            active_chunk.prepared_path,
            chunk_length_seconds=config.segment_seconds,
        )
        return result, polish_subtitle_text(result.text), last_emitted_segment_end

    window_audio_path = merge_prepared_chunks(
        config,
        window_chunks,
        window_chunks[-1].chunk_index,
    )
    if window_audio_path is None:
        return TranscriptionResult(text="", ok=False, segments=()), "", last_emitted_segment_end

    result = asr_transcriber.transcribe(
        window_audio_path,
        chunk_length_seconds=config.recognition_window_seconds,
    )
    cleanup_chunk_files(window_audio_path)
    window_start_seconds = max(window_chunks[0].chunk_index, 0) * float(config.segment_seconds)
    window_end_seconds = (max(window_chunks[-1].chunk_index, 0) + 1) * float(
        config.segment_seconds
    )
    stable_cutoff_seconds = max(
        window_start_seconds,
        window_end_seconds - config.recognition_holdback_seconds,
    )
    emitted_texts: list[str] = []
    emitted_end_time = last_emitted_segment_end
    for segment in result.segments:
        segment_text = normalize_transcript_text(segment.text)
        if not segment_text:
            continue
        global_end = window_start_seconds + max(segment.end, segment.start)
        if global_end <= last_emitted_segment_end + 0.05:
            continue
        if global_end > stable_cutoff_seconds:
            continue
        emitted_texts.append(segment_text)
        emitted_end_time = max(emitted_end_time, global_end)

    incremental_text = polish_subtitle_text(" ".join(emitted_texts))
    if not incremental_text and result.text.strip():
        fallback_text = polish_subtitle_text(result.text)
        incremental_text = extract_incremental_text(last_origin_text, fallback_text)
        if not incremental_text and fallback_text != last_origin_text:
            incremental_text = fallback_text

    return result, incremental_text, emitted_end_time


def run(config: SubtitleConfig) -> int:
    if not validate_config(config):
        return 2

    ensure_paths(config)
    cleanup_stale_worker_processes(config)
    clear_old_chunks(config)

    asr_transcriber = SubtitleAsrTranscriber(config)
    translator = SubtitleTranslator(config)
    capture_handle = start_audio_capture(config)
    processed_files: set[str] = set()
    chunk_collector = ChunkCollector()
    entries = read_subtitle_entries(config)
    state_lock = threading.Lock()
    window_chunk_limit = max(
        (config.recognition_window_seconds + config.segment_seconds - 1)
        // config.segment_seconds,
        1,
    )
    direct_chunk_mode = window_chunk_limit == 1
    min_window_chunks = min(
        window_chunk_limit,
        max(
            (config.recognition_min_window_seconds + config.segment_seconds - 1)
            // config.segment_seconds,
            1,
        ),
    )
    window_chunks: list[PreparedChunk] = []
    translation_attempts: dict[int, int] = {}
    translation_retry_after: dict[int, float] = {}
    translation_metrics = TranslationMetrics()
    last_translation_metrics_at = time.time()
    last_capture_activity = time.time()
    last_capture_restart_at = 0.0
    last_source_reload_check_at = 0.0

    def restart_capture(reason: str) -> None:
        nonlocal capture_handle
        nonlocal processed_files
        nonlocal retry_after
        nonlocal window_chunks
        nonlocal last_emitted_segment_end
        nonlocal last_capture_activity
        nonlocal last_capture_restart_at

        print(f"[subtitle][capture] restarting: {reason}")
        capture_handle.terminate()
        clear_old_chunks(config)
        for chunk in window_chunks:
            cleanup_chunk_files(chunk.prepared_path)
        processed_files.clear()
        chunk_collector.reset()
        retry_after.clear()
        window_chunks.clear()
        last_emitted_segment_end = 0.0
        capture_handle = start_audio_capture(config)
        last_capture_activity = time.time()
        last_capture_restart_at = last_capture_activity
        if capture_handle.source_name:
            print(f"[subtitle][capture] resumed source: {capture_handle.source_name}")
            if capture_handle.source_origin:
                print(
                    f"[subtitle][capture] resumed source mode: {capture_handle.source_origin}"
                )

    def submit_translation(entry_id: int, origin_text: str) -> bool:
        attempts = translation_attempts.get(entry_id, 0)
        if attempts >= MAX_TRANSLATION_ATTEMPTS:
            return False
        if not translation_dispatcher.submit(entry_id, origin_text):
            translation_metrics.dropped += 1
            return False
        attempts += 1
        translation_attempts[entry_id] = attempts
        translation_retry_after[entry_id] = time.time() + translation_retry_delay(attempts)
        translation_metrics.submitted += 1
        if attempts > 1:
            translation_metrics.retried += 1
        return True

    def apply_translation_result(result: TranslationResult) -> None:
        translated_text = result.text.strip()
        if not translated_text:
            translation_metrics.failed += 1
            attempts = translation_attempts.get(result.entry_id, 1)
            if attempts < MAX_TRANSLATION_ATTEMPTS:
                translation_retry_after[result.entry_id] = time.time() + translation_retry_delay(
                    attempts + 1
                )
            return

        with state_lock:
            entry_updated = False
            for entry in entries:
                if entry.entry_id != result.entry_id:
                    continue
                if entry.translated != translated_text:
                    entry.translated = translated_text
                    entry_updated = True
                break

            if entry_updated:
                write_entry_outputs(config, entries)

        translation_attempts.pop(result.entry_id, None)
        translation_retry_after.pop(result.entry_id, None)
        translation_metrics.success += 1
        timestamp_text = ""
        for entry in entries:
            if entry.entry_id == result.entry_id:
                timestamp_text = entry.timestamp_text
                break
        if timestamp_text:
            print(f"[subtitle][zh][{timestamp_text}] {translated_text}")
        else:
            print(f"[subtitle][zh] {translated_text}")

    translation_dispatcher = TranslationDispatcher(
        translator,
        config.translation_pending_limit,
        config.translation_workers,
        result_handler=apply_translation_result,
    )
    last_origin_text = entries[-1].origin if entries else ""
    last_emitted_segment_end = 0.0
    next_entry_id = entries[-1].entry_id + 1 if entries else 1
    retry_after: dict[str, float] = {}

    with state_lock:
        write_entry_outputs(config, entries)

    print(f"[subtitle] capture backend: {config.capture_backend}")
    if config.source_url_file is not None:
        print(f"[subtitle] source url file: {config.source_url_file}")
    if capture_handle.source_name:
        print(f"[subtitle] capture source: {capture_handle.source_name}")
        if capture_handle.source_origin:
            print(f"[subtitle] capture source mode: {capture_handle.source_origin}")
    print(f"[subtitle] output file: {config.output_path}")
    print(f"[subtitle][asr] backend: {asr_transcriber.describe()}")
    print(
        "[subtitle] pseudo-stream "
        f"chunk={config.segment_seconds}s "
        f"window={config.recognition_min_window_seconds}-{config.recognition_window_seconds}s"
    )
    if direct_chunk_mode:
        print("[subtitle] direct single-window mode enabled")
    if config.translate_to_zh and translator.client is None:
        print("[subtitle][translate] no online LLM provider configured; showing original text only")
    elif config.translate_to_zh:
        print(
            "[subtitle][translate] async enabled "
            f"providers={translator.describe()} "
            f"timeout={config.translation_timeout_seconds:.1f}s "
            f"pending_limit={config.translation_pending_limit} "
            f"workers={config.translation_workers}"
        )
        for entry in entries:
            if entry.origin.strip() and not entry.translated.strip():
                submit_translation(entry.entry_id, entry.origin)

    try:
        while True:
            now = time.time()
            capture_exit_code = capture_handle.poll()
            if capture_exit_code is not None:
                stderr_text = capture_handle.read_error()
                if now - last_capture_restart_at >= config.capture_restart_cooldown_seconds:
                    detail = stderr_text.strip()
                    summary = f"process exited ({capture_exit_code})"
                    if detail:
                        summary = f"{summary}: {detail[-180:]}"
                    restart_capture(summary)
                    time.sleep(1.0)
                    continue

            if (
                config.capture_backend == "ffmpeg_source_url"
                and config.source_url_file is not None
                and now - last_source_reload_check_at >= config.source_url_reload_seconds
            ):
                last_source_reload_check_at = now
                target_source_url, _ = resolve_capture_source_url(
                    config,
                    allow_discovery=False,
                )
                if target_source_url and target_source_url != capture_handle.source_name:
                    restart_capture(
                        "source url changed "
                        f"({capture_handle.source_name} -> {target_source_url})"
                    )
                    time.sleep(1.0)
                    continue

            window_dirty = False
            latest_chunk_mtime = 0.0
            for audio_path in sorted(config.chunks_dir.glob("chunk_*.wav")):
                if audio_path.name in processed_files:
                    continue
                if retry_after.get(audio_path.name, 0.0) > time.time():
                    continue
                try:
                    stat = audio_path.stat()
                except FileNotFoundError:
                    continue
                latest_chunk_mtime = max(latest_chunk_mtime, stat.st_mtime)
                if stat.st_size <= WAV_HEADER_BYTES:
                    retry_after[audio_path.name] = time.time() + CHUNK_SETTLE_SECONDS
                    continue
                if time.time() - stat.st_mtime < CHUNK_SETTLE_SECONDS:
                    continue

                prepared_audio_path = prepare_chunk_audio(config, audio_path)
                if prepared_audio_path is None:
                    retry_after[audio_path.name] = time.time() + 2.0
                    continue

                silent = False
                levels = measure_audio_level(prepared_audio_path)
                if levels is not None:
                    peak, rms = levels
                    if peak < 0.002 and rms < 0.0005:
                        silent = True
                        print(
                            f"[subtitle][silent] {audio_path.name} peak={peak:.4f} rms={rms:.4f}"
                        )
                processed_files.add(audio_path.name)
                if len(processed_files) > 20_000:
                    processed_files = set(
                        sorted(processed_files)[-10_000:]
                    )
                retry_after.pop(audio_path.name, None)
                cleanup_chunk_files(audio_path)
                last_capture_activity = max(last_capture_activity, time.time(), stat.st_mtime)
                window_chunks.append(
                    PreparedChunk(
                        chunk_index=parse_chunk_index(audio_path),
                        name=audio_path.name,
                        prepared_path=prepared_audio_path,
                        silent=silent,
                    )
                )
                while len(window_chunks) > window_chunk_limit:
                    stale_chunk = window_chunks.pop(0)
                    cleanup_chunk_files(stale_chunk.prepared_path)
                window_dirty = True
                if not silent:
                    print(f"[subtitle][chunk] {audio_path.name}")

            if window_dirty and len(window_chunks) >= min_window_chunks:
                if all(chunk.silent for chunk in window_chunks):
                    time.sleep(0.2)
                    continue

                result, incremental_text, last_emitted_segment_end = transcribe_window_incremental(
                    config,
                    asr_transcriber,
                    window_chunks,
                    direct_chunk_mode=direct_chunk_mode,
                    last_origin_text=last_origin_text,
                    last_emitted_segment_end=last_emitted_segment_end,
                )

                if not result.ok:
                    latest_chunk_name = window_chunks[-1].name if window_chunks else "unknown"
                    print(
                        f"[subtitle][skip] {latest_chunk_name} reason=transcribe_failed"
                    )
                    time.sleep(0.2)
                    continue
                latest_chunk_name = window_chunks[-1].name if window_chunks else "unknown"
                if not incremental_text:
                    print(f"[subtitle][skip] {latest_chunk_name} reason=no_incremental_text")
                    time.sleep(0.2)
                    continue
                if incremental_text == last_origin_text:
                    print(f"[subtitle][skip] {latest_chunk_name} reason=duplicate_text")
                    time.sleep(0.2)
                    continue

                last_origin_text = incremental_text
                entry = SubtitleEntry(
                    entry_id=next_entry_id,
                    origin=incremental_text,
                    timestamp_text=format_timestamp_text(),
                )
                next_entry_id += 1
                with state_lock:
                    entries.append(entry)
                    if len(entries) > config.history_lines:
                        del entries[:-config.history_lines]
                    write_entry_outputs(config, entries)
                print(f"[subtitle][{entry.timestamp_text}] {incremental_text}")

                if config.translate_to_zh:
                    submit_translation(entry.entry_id, incremental_text)

            if config.translate_to_zh and translation_dispatcher.enabled:
                now = time.time()
                for entry in entries:
                    if not entry.origin.strip() or entry.translated.strip():
                        continue
                    attempts = translation_attempts.get(entry.entry_id, 0)
                    if attempts >= MAX_TRANSLATION_ATTEMPTS:
                        continue
                    if translation_retry_after.get(entry.entry_id, 0.0) > now:
                        continue
                    submit_translation(entry.entry_id, entry.origin)

            if config.translate_to_zh and time.time() - last_translation_metrics_at >= 30.0:
                print(
                    "[subtitle][translate][stats] "
                    f"submitted={translation_metrics.submitted} "
                    f"success={translation_metrics.success} "
                    f"failed={translation_metrics.failed} "
                    f"retried={translation_metrics.retried} "
                    f"dropped={translation_metrics.dropped}"
                )
                last_translation_metrics_at = time.time()

            if latest_chunk_mtime > 0:
                last_capture_activity = max(last_capture_activity, latest_chunk_mtime)
            if (
                time.time() - last_capture_activity >= config.capture_stall_seconds
                and time.time() - last_capture_restart_at
                >= config.capture_restart_cooldown_seconds
            ):
                restart_capture(
                    f"no new chunks for {time.time() - last_capture_activity:.1f}s"
                )
                time.sleep(1.0)
                continue

            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[subtitle] stopped")
        return 0
    finally:
        if config.translate_to_zh:
            print(
                "[subtitle][translate][final] "
                f"submitted={translation_metrics.submitted} "
                f"success={translation_metrics.success} "
                f"failed={translation_metrics.failed} "
                f"retried={translation_metrics.retried} "
                f"dropped={translation_metrics.dropped}"
            )
        asr_transcriber.close()
        translation_dispatcher.close()
        translator.close()
        capture_handle.terminate()
        clear_old_chunks(config)


def main() -> int:
    parser = argparse.ArgumentParser(description="Live subtitle sidecar for browser audio.")
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="List available capture devices and exit.",
    )
    parser.add_argument(
        "--source-url",
        default="",
        help="Override subtitle source URL for this run only.",
    )
    parser.add_argument(
        "--source-url-file",
        default="",
        help="Read subtitle source URL from this text file (first non-empty line).",
    )
    args = parser.parse_args()

    config = load_config()
    source_url_file = str(args.source_url_file or "").strip()
    source_url = str(args.source_url or "").strip()
    if source_url_file:
        config = replace(config, source_url_file=_resolve_path(source_url_file))
    if source_url:
        config = replace(config, source_url=source_url)
    if args.list_devices:
        return list_audio_devices(config)
    return run(config)


if __name__ == "__main__":
    raise SystemExit(main())
