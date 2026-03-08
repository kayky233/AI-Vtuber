import asyncio
import contextlib
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from bilibili_api import live, select_client, user as bilibili_user

from llm_bot import LLMChatBot
from simple_bot import SimpleChatBot
from tts_engine import close_tts, describe_settings, synthesize_to_file

try:
    from local_settings import SETTINGS as LOCAL_SETTINGS
except Exception:
    LOCAL_SETTINGS: dict[str, Any] = {}


PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = Path("output.txt")
TMP_AUDIO_DIR = Path("tmp_audio")


@dataclass(frozen=True)
class AudioTask:
    sequence: int
    trace_id: str
    label: str
    text: str
    created_at: float
    queued_at: float


@dataclass(frozen=True)
class PreparedAudioTask:
    task: AudioTask
    audio_path: Path | None
    synth_started_at: float
    synthesized_at: float


@dataclass(frozen=True)
class WelcomeTask:
    trace_id: str
    label: str
    text: str
    created_at: float


def _read_setting(name: str) -> Any:
    value = os.getenv(name)
    if value is not None and value != "":
        return value
    return LOCAL_SETTINGS.get(name)


def _read_bool(name: str, default: bool) -> bool:
    value = _read_setting(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _read_float(name: str, default: float) -> float:
    value = _read_setting(name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _read_int(name: str, default: int) -> int:
    value = _read_setting(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _read_str(name: str, default: str) -> str:
    value = _read_setting(name)
    if value is None:
        return default
    return str(value).strip()


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return PROJECT_DIR / path


WELCOME_ENABLED = _read_bool("WELCOME_ENABLED", True)
WELCOME_MIN_INTERVAL_SECONDS = _read_float("WELCOME_MIN_INTERVAL_SECONDS", 8.0)
WELCOME_USER_COOLDOWN_SECONDS = _read_float("WELCOME_USER_COOLDOWN_SECONDS", 300.0)
WELCOME_MAX_AUDIO_QUEUE = _read_int("WELCOME_MAX_AUDIO_QUEUE", 2)
WELCOME_ON_FIRST_DANMU = _read_bool("WELCOME_ON_FIRST_DANMU", True)
WELCOME_DEBUG = _read_bool("WELCOME_DEBUG", False)
AUDIO_SYNTH_WORKERS = max(_read_int("AUDIO_SYNTH_WORKERS", 2), 1)
MPV_VOLUME = _read_float("MPV_VOLUME", 100.0)
MPV_AUDIO_DEVICE = _read_str("MPV_AUDIO_DEVICE", "")
MPV_AF = _read_str("MPV_AF", "")
HEARTBEAT_ENABLED = _read_bool("HEARTBEAT_ENABLED", False)
HEARTBEAT_INTERVAL_SECONDS = max(_read_float("HEARTBEAT_INTERVAL_SECONDS", 180.0), 30.0)
HEARTBEAT_MAX_AUDIO_QUEUE = max(_read_int("HEARTBEAT_MAX_AUDIO_QUEUE", 1), 0)
SUBTITLE_AUTOSTART = _read_bool("SUBTITLE_AUTOSTART", False)
SUBTITLE_SCRIPT_PATH = _resolve_path(_read_str("SUBTITLE_SCRIPT_PATH", "live_subtitles.py"))
SUBTITLE_LOG_PATH = _resolve_path(_read_str("SUBTITLE_LOG_PATH", "live_subtitles.log"))
SUBTITLE_OVERLAY_AUTOSTART = _read_bool("SUBTITLE_OVERLAY_AUTOSTART", False)
SUBTITLE_OVERLAY_SCRIPT_PATH = _resolve_path(
    _read_str("SUBTITLE_OVERLAY_SCRIPT_PATH", "subtitle_overlay_server.py")
)
SUBTITLE_OVERLAY_LOG_PATH = _resolve_path(
    _read_str("SUBTITLE_OVERLAY_LOG_PATH", "subtitle_overlay.log")
)
SUBTITLE_OVERLAY_HOST = _read_str("SUBTITLE_OVERLAY_HOST", "127.0.0.1")
SUBTITLE_OVERLAY_PORT = max(_read_int("SUBTITLE_OVERLAY_PORT", 18082), 1)
RESOLVE_MASKED_USER_NAMES = _read_bool("RESOLVE_MASKED_USER_NAMES", True)
RESOLVE_USER_NAME_TIMEOUT_SECONDS = max(
    _read_float("RESOLVE_USER_NAME_TIMEOUT_SECONDS", 3.0),
    0.5,
)
HEARTBEAT_TEMPLATES = (
    "\u8fd9\u4f1a\u513f\u8282\u594f\u5148\u653e\u6162\u4e00\u70b9\uff0c\u6211\u8fd8\u5728\uff0c\u5927\u5bb6\u53ef\u4ee5\u6162\u6162\u804a\u3002",
    "\u5148\u7ed9\u5927\u5bb6\u7559\u4e2a\u5c0f\u7a7a\u6321\uff0c\u6211\u4e00\u8fb9\u542c\u97f3\u4e50\u4e00\u8fb9\u7b49\u4f60\u4eec\u7684\u65b0\u8bdd\u9898\u3002",
    "\u521a\u521a\u90a3\u6bb5\u6c14\u6c1b\u8fd8\u633a\u8212\u670d\u7684\uff0c\u7b49\u4e0b\u770b\u770b\u4f60\u4eec\u60f3\u804a\u4ec0\u4e48\u3002",
    "\u6211\u5148\u5728\u8fd9\u91cc\u5b88\u7740\uff0c\u4f60\u4eec\u8981\u662f\u6709\u60f3\u542c\u7684\u8bdd\u9898\uff0c\u53ef\u4ee5\u76f4\u63a5\u4e22\u8fc7\u6765\u3002",
    "\u8fd9\u4f1a\u513f\u5b89\u9759\u4e00\u70b9\u4e5f\u633a\u597d\uff0c\u521a\u597d\u9002\u5408\u628a\u76f4\u64ad\u95f4\u7684\u6c14\u6c1b\u6162\u6162\u70ed\u8d77\u6765\u3002",
)
WELCOME_TEMPLATES = {
    "morning": (
        "欢迎{user_name}来到直播间，早上好呀，今天也要慢慢开机。",
        "欢迎{user_name}，早安早安，先进来坐一会儿。",
        "欢迎{user_name}，新的一天先来这里报到啦。",
    ),
    "afternoon": (
        "欢迎{user_name}来到直播间，下午好呀，先来轻松一下。",
        "欢迎{user_name}，这个点刚刚好，进来一起聊聊天。",
        "欢迎{user_name}，下午场也要元气一点点。",
    ),
    "evening": (
        "欢迎{user_name}来到直播间，晚上好呀，今天也一起慢慢玩。",
        "欢迎{user_name}，夜晚档签到成功，先找个舒服的位置坐下。",
        "欢迎{user_name}，晚上的空气很适合一起发呆聊天。",
    ),
    "late_night": (
        "欢迎{user_name}，这么晚还在呀，先进来歇一会儿。",
        "欢迎{user_name}来到直播间，深夜也有人陪你聊天。",
        "欢迎{user_name}，夜深了，来这里安静待一会儿吧。",
    ),
}
local_bot = SimpleChatBot(
    "Xzai",
    database_path="db.sqlite3",
)
bot = LLMChatBot(local_fallback=local_bot)


def append_output(line: str) -> None:
    with OUTPUT_PATH.open("a", encoding="utf-8") as file:
        file.write(f"{line}\n")


def format_elapsed(seconds: float) -> str:
    return f"{seconds:.3f}s"


def print_timing(trace_id: str, label: str, **stages: float) -> None:
    parts = [f"{name}={format_elapsed(value)}" for name, value in stages.items()]
    print(f"[耗时][{trace_id}][{label}] {' '.join(parts)}")


def debug_welcome(message: str) -> None:
    if WELCOME_DEBUG:
        print(f"[欢迎调试] {message}")


def build_welcome_text(user_name: str) -> str:
    hour = time.localtime().tm_hour
    if 5 <= hour < 11:
        period = "morning"
    elif 11 <= hour < 18:
        period = "afternoon"
    elif 18 <= hour < 24:
        period = "evening"
    else:
        period = "late_night"

    templates = WELCOME_TEMPLATES[period]
    index = sum(ord(char) for char in user_name) % len(templates)
    return templates[index].format(user_name=user_name)


def build_spoken_user_name(user_name: str) -> str:
    raw_name = user_name.strip()
    if not raw_name:
        return "这位朋友"

    spoken_name = re.sub(r"\s+", "", raw_name)
    spoken_name = re.sub(r"[*＊#@]+", "", spoken_name)
    spoken_name = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9_-]", "", spoken_name)
    spoken_name = spoken_name.strip("_-")

    if not spoken_name:
        return "这位朋友"
    if re.fullmatch(r"\d+", spoken_name):
        return f"{spoken_name}号"
    return spoken_name


def is_masked_user_name(user_name: str) -> bool:
    return "*" in user_name or "＊" in user_name


def choose_best_user_name(*candidates: Any) -> str:
    names: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        candidate_text = str(candidate).strip()
        if candidate_text:
            names.append(candidate_text)

    if not names:
        return ""

    for name in names:
        if not is_masked_user_name(name):
            return name
    return names[0]


def extract_user_identity(payload: dict | None) -> tuple[str, str] | None:
    if not isinstance(payload, dict):
        return None

    user_info = payload.get("user_info", {})
    if not isinstance(user_info, dict):
        user_info = {}

    base_info = user_info.get("base", {})
    if not isinstance(base_info, dict):
        base_info = {}

    account_info = base_info.get("account_info", {})
    if not isinstance(account_info, dict):
        account_info = {}

    risk_ctrl_info = base_info.get("risk_ctrl_info", {})
    if not isinstance(risk_ctrl_info, dict):
        risk_ctrl_info = {}

    user_name = choose_best_user_name(
        payload.get("uname"),
        payload.get("username"),
        payload.get("user_name"),
        payload.get("nick_name"),
        payload.get("nick"),
        payload.get("name"),
        base_info.get("name"),
        account_info.get("name"),
        risk_ctrl_info.get("name"),
    )

    if not user_name:
        return None

    uid = payload.get("uid")
    if uid is None:
        uid = payload.get("user_id")
    if uid is None:
        uid = payload.get("target_uid")
    user_key = str(uid) if uid is not None else user_name
    return user_key, user_name


def extract_interact_user(event: dict) -> tuple[str, str] | None:
    data = event.get("data", {})
    for payload in (
        data.get("data", {}).get("pb_decoded", {}),
        data.get("data", {}),
        data,
    ):
        user_info = extract_user_identity(payload)
        if user_info is not None:
            return user_info
    return None


def print_banner() -> None:
    print("--------------------")
    print("作者：Xzai")
    print("QQ：2744601427")
    print("--------------------")
    print(f"回复链路：{bot.describe()}")
    print(f"语音：{describe_settings()}")
    print(f"音频流水线：合成并发 {AUDIO_SYNTH_WORKERS}")
    print(f"欢迎词：{'开启' if WELCOME_ENABLED else '关闭'}")
    print(
        f"subtitle sidecar: {'on' if SUBTITLE_AUTOSTART else 'off'} "
        f"path={SUBTITLE_SCRIPT_PATH}"
    )
    print(
        f"subtitle overlay: {'on' if SUBTITLE_OVERLAY_AUTOSTART else 'off'} "
        f"url=http://{SUBTITLE_OVERLAY_HOST}:{SUBTITLE_OVERLAY_PORT}/subtitle_overlay.html"
    )


def _audio_path_for(task: AudioTask) -> Path:
    TMP_AUDIO_DIR.mkdir(exist_ok=True)
    return TMP_AUDIO_DIR / f"{task.sequence:06d}_{task.trace_id}.mp3"


def describe_playback_settings() -> str:
    device = MPV_AUDIO_DEVICE or "default"
    af = MPV_AF or "off"
    return f"mpv volume={MPV_VOLUME:g} device={device} af={af}"


def build_mpv_command(audio_path: Path) -> list[str]:
    command = [
        "mpv.exe",
        "--no-video",
        f"--volume={MPV_VOLUME:g}",
    ]
    if MPV_AUDIO_DEVICE:
        command.append(f"--audio-device={MPV_AUDIO_DEVICE}")
    if MPV_AF:
        command.append(f"--af={MPV_AF}")
    command.append(str(audio_path))
    return command


def build_heartbeat_text(index: int) -> str:
    return HEARTBEAT_TEMPLATES[index % len(HEARTBEAT_TEMPLATES)]


def find_running_sidecar_pid(script_path: Path) -> int | None:
    if sys.platform != "win32":
        return None

    script_name = script_path.name.replace("'", "''")
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' -and $_.CommandLine -match "
        f"[regex]::Escape('{script_name}') }} | "
        "Select-Object -ExpandProperty ProcessId -First 1"
    )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
            check=False,
        )
    except Exception:
        return None

    if result.returncode != 0:
        return None

    pid_text = result.stdout.strip()
    if not pid_text:
        return None

    try:
        return int(pid_text.splitlines()[0].strip())
    except ValueError:
        return None


def is_subtitle_overlay_alive() -> bool:
    url = f"http://{SUBTITLE_OVERLAY_HOST}:{SUBTITLE_OVERLAY_PORT}/healthz"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return int(getattr(response, "status", 0)) == 200
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def start_python_sidecar(
    *,
    enabled: bool,
    script_path: Path,
    log_path: Path,
    label: str,
) -> tuple[subprocess.Popen[bytes] | None, Any | None]:
    if not enabled:
        return None, None
    if not script_path.exists():
        print(f"[{label}] script not found: {script_path}")
        return None, None
    if label == "subtitle-overlay" and is_subtitle_overlay_alive():
        print(f"[{label}] overlay already reachable; skip autostart")
        return None, None
    existing_pid = find_running_sidecar_pid(script_path)
    if existing_pid is not None:
        print(f"[{label}] sidecar already running pid={existing_pid}; skip autostart")
        return None, None

    log_parent = log_path.parent
    if str(log_parent) not in {"", "."}:
        log_parent.mkdir(parents=True, exist_ok=True)

    log_file: Any | None = None
    stdout_target: Any = subprocess.DEVNULL
    stderr_target: Any = subprocess.DEVNULL
    try:
        log_file = log_path.open("a", encoding="utf-8")
    except OSError as error:
        print(f"[{label}] log unavailable, starting without file log: {error}")
    else:
        log_file.write(f"\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        log_file.flush()
        stdout_target = log_file
        stderr_target = subprocess.STDOUT

    try:
        process = subprocess.Popen(
            [sys.executable, "-u", str(script_path)],
            stdout=stdout_target,
            stderr=stderr_target,
            cwd=str(PROJECT_DIR),
        )
    except Exception as error:
        print(f"[{label}] failed to start sidecar: {error}")
        if log_file is not None:
            log_file.close()
        return None, None

    if log_file is not None:
        print(f"[{label}] sidecar started pid={process.pid} log={log_path}")
    else:
        print(f"[{label}] sidecar started pid={process.pid} log=disabled")
    return process, log_file


def stop_python_sidecar(
    process: subprocess.Popen[bytes] | None,
    log_file: Any | None,
) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.kill()
                process.wait(timeout=5)

    if log_file is not None:
        log_file.close()


async def synth_worker(
    audio_queue: asyncio.Queue[AudioTask],
    prepared_audio: dict[int, PreparedAudioTask],
    prepared_condition: asyncio.Condition,
) -> None:
    while True:
        task = await audio_queue.get()
        synth_started_at = time.perf_counter()
        try:
            audio_path = _audio_path_for(task)
            await synthesize_to_file(task.text, str(audio_path))
            synthesized_at = time.perf_counter()
            prepared_task = PreparedAudioTask(
                task=task,
                audio_path=audio_path,
                synth_started_at=synth_started_at,
                synthesized_at=synthesized_at,
            )
        except Exception as error:
            print(f"[音频错误]：{error}")
            failed_at = time.perf_counter()
            print_timing(
                task.trace_id,
                task.label,
                合成排队=synth_started_at - task.queued_at,
                失败前=failed_at - synth_started_at,
                总计=failed_at - task.created_at,
            )
            prepared_task = PreparedAudioTask(
                task=task,
                audio_path=None,
                synth_started_at=synth_started_at,
                synthesized_at=failed_at,
            )
        finally:
            async with prepared_condition:
                prepared_audio[task.sequence] = prepared_task
                prepared_condition.notify_all()
            audio_queue.task_done()


async def playback_worker(
    prepared_audio: dict[int, PreparedAudioTask],
    prepared_condition: asyncio.Condition,
) -> None:
    next_sequence = 1
    while True:
        async with prepared_condition:
            await prepared_condition.wait_for(lambda: next_sequence in prepared_audio)
            prepared_task = prepared_audio.pop(next_sequence)
        next_sequence += 1

        task = prepared_task.task
        if prepared_task.audio_path is None:
            continue

        play_started_at = time.perf_counter()
        try:
            await asyncio.to_thread(
                subprocess.run,
                build_mpv_command(prepared_task.audio_path),
                check=False,
            )
            finished_at = time.perf_counter()
            print_timing(
                task.trace_id,
                task.label,
                合成排队=prepared_task.synth_started_at - task.queued_at,
                合成=prepared_task.synthesized_at - prepared_task.synth_started_at,
                待播=play_started_at - prepared_task.synthesized_at,
                播放=finished_at - play_started_at,
                音频链路=finished_at - task.queued_at,
                总计=finished_at - task.created_at,
            )
        except Exception as error:
            print(f"[音频错误]：{error}")
            failed_at = time.perf_counter()
            print_timing(
                task.trace_id,
                task.label,
                合成排队=prepared_task.synth_started_at - task.queued_at,
                合成=prepared_task.synthesized_at - prepared_task.synth_started_at,
                待播=play_started_at - prepared_task.synthesized_at,
                失败前=failed_at - play_started_at,
                总计=failed_at - task.created_at,
            )
        finally:
            with contextlib.suppress(FileNotFoundError):
                prepared_task.audio_path.unlink()


async def run(
    room_id: int | None = None,
    auto_disconnect_after: float | None = None,
) -> None:
    print_banner()
    select_client("aiohttp")

    if room_id is None:
        room_id = int(input("请输入直播间编号: "))

    room = live.LiveDanmaku(room_id)
    audio_queue: asyncio.Queue[AudioTask] = asyncio.Queue()
    prepared_audio: dict[int, PreparedAudioTask] = {}
    prepared_condition = asyncio.Condition()
    synth_tasks = [
        asyncio.create_task(synth_worker(audio_queue, prepared_audio, prepared_condition))
        for _ in range(AUDIO_SYNTH_WORKERS)
    ]
    playback_task = asyncio.create_task(playback_worker(prepared_audio, prepared_condition))
    last_welcome_time = 0.0
    last_welcome_by_user: dict[str, float] = {}
    known_users_in_session: set[str] = set()
    resolved_user_names: dict[str, str] = {}
    failed_user_name_resolutions: dict[str, float] = {}
    last_live_activity_at = time.monotonic()
    trace_seq = 0
    audio_seq = 0
    heartbeat_seq = 0
    subtitle_process, subtitle_log_file = start_python_sidecar(
        enabled=SUBTITLE_AUTOSTART,
        script_path=SUBTITLE_SCRIPT_PATH,
        log_path=SUBTITLE_LOG_PATH,
        label="subtitle",
    )
    subtitle_overlay_process, subtitle_overlay_log_file = start_python_sidecar(
        enabled=SUBTITLE_OVERLAY_AUTOSTART,
        script_path=SUBTITLE_OVERLAY_SCRIPT_PATH,
        log_path=SUBTITLE_OVERLAY_LOG_PATH,
        label="subtitle-overlay",
    )
    sidecar_watch_tasks: list[asyncio.Task[None]] = []

    for label, process, log_path in (
        ("subtitle", subtitle_process, SUBTITLE_LOG_PATH),
        ("subtitle-overlay", subtitle_overlay_process, SUBTITLE_OVERLAY_LOG_PATH),
    ):
        if process is None:
            continue

        async def watch_sidecar(
            sidecar_label: str = label,
            sidecar_process: subprocess.Popen[bytes] = process,
            sidecar_log_path: Path = log_path,
        ) -> None:
            return_code = await asyncio.to_thread(sidecar_process.wait)
            print(
                f"[{sidecar_label}] sidecar exited rc={return_code} "
                f"log={sidecar_log_path}"
            )

        sidecar_watch_tasks.append(asyncio.create_task(watch_sidecar()))

    if auto_disconnect_after is not None:

        async def disconnect_later() -> None:
            await asyncio.sleep(auto_disconnect_after)
            if room.get_status() == room.STATUS_ESTABLISHED:
                await room.disconnect()

        asyncio.create_task(disconnect_later())

    def next_trace_id(prefix: str) -> str:
        nonlocal trace_seq
        trace_seq += 1
        return f"{prefix}{trace_seq:04d}"

    def mark_live_activity(at: float | None = None) -> None:
        nonlocal last_live_activity_at
        last_live_activity_at = time.monotonic() if at is None else at

    async def resolve_user_name(user_key: str, user_name: str) -> str:
        if not RESOLVE_MASKED_USER_NAMES:
            return user_name
        if not is_masked_user_name(user_name):
            return user_name

        cached_name = resolved_user_names.get(user_key)
        if cached_name:
            return cached_name

        last_failed_at = failed_user_name_resolutions.get(user_key, 0.0)
        if time.monotonic() - last_failed_at < 300.0:
            return user_name

        try:
            uid = int(user_key)
        except ValueError:
            return user_name

        try:
            info = await asyncio.wait_for(
                bilibili_user.User(uid).get_user_info(),
                timeout=RESOLVE_USER_NAME_TIMEOUT_SECONDS,
            )
        except Exception:
            failed_user_name_resolutions[user_key] = time.monotonic()
            return user_name

        resolved_name = str(info.get("name") or info.get("uname") or "").strip()
        if not resolved_name:
            failed_user_name_resolutions[user_key] = time.monotonic()
            return user_name

        resolved_user_names[user_key] = resolved_name
        return resolved_name

    async def enqueue_audio(
        trace_id: str,
        label: str,
        text: str,
        created_at: float,
    ) -> None:
        nonlocal audio_seq
        audio_seq += 1
        queued_at = time.perf_counter()
        await audio_queue.put(
            AudioTask(
                sequence=audio_seq,
                trace_id=trace_id,
                label=label,
                text=text,
                created_at=created_at,
                queued_at=queued_at,
            )
        )

    def prepare_welcome_user(
        user_key: str,
        user_name: str,
        source: str,
        started_at: float | None = None,
    ) -> WelcomeTask | None:
        nonlocal last_welcome_time
        if started_at is None:
            started_at = time.perf_counter()

        if not WELCOME_ENABLED:
            debug_welcome(f"skip disabled source={source} user={user_name}")
            return None

        now = time.monotonic()
        if now - last_welcome_time < WELCOME_MIN_INTERVAL_SECONDS:
            debug_welcome(f"skip rate_limit source={source} user={user_name}")
            return None
        if now - last_welcome_by_user.get(user_key, 0.0) < WELCOME_USER_COOLDOWN_SECONDS:
            debug_welcome(f"skip cooldown source={source} user={user_name}")
            return None
        if audio_queue.qsize() > WELCOME_MAX_AUDIO_QUEUE:
            debug_welcome(f"skip queue_busy source={source} user={user_name}")
            return None

        if len(last_welcome_by_user) > 200:
            cutoff = now - WELCOME_USER_COOLDOWN_SECONDS
            stale_keys = [
                key for key, timestamp in last_welcome_by_user.items() if timestamp < cutoff
            ]
            for key in stale_keys:
                last_welcome_by_user.pop(key, None)

        spoken_user_name = build_spoken_user_name(user_name)
        welcome_text = build_welcome_text(spoken_user_name)
        last_welcome_time = now
        last_welcome_by_user[user_key] = now
        known_users_in_session.add(user_key)
        trace_id = next_trace_id("W")

        line = f"[欢迎{user_name}]：{welcome_text}"
        print(line)
        append_output(line)
        debug_welcome(f"sent source={source} user={user_name}")
        print_timing(
            trace_id,
            f"欢迎{user_name}",
            准备=time.perf_counter() - started_at,
        )

        return WelcomeTask(
            trace_id=trace_id,
            label=f"欢迎{user_name}",
            text=welcome_text,
            created_at=started_at,
        )

    async def maybe_welcome_user(
        user_key: str,
        user_name: str,
        source: str,
        started_at: float | None = None,
    ) -> bool:
        welcome_task = prepare_welcome_user(user_key, user_name, source, started_at)
        if welcome_task is None:
            return False
        await enqueue_audio(
            welcome_task.trace_id,
            welcome_task.label,
            welcome_task.text,
            welcome_task.created_at,
        )
        return True

    async def heartbeat_worker() -> None:
        nonlocal heartbeat_seq
        while True:
            await asyncio.sleep(5)
            if not HEARTBEAT_ENABLED:
                continue
            if audio_queue.qsize() > HEARTBEAT_MAX_AUDIO_QUEUE:
                continue
            if prepared_audio:
                continue

            now = time.monotonic()
            if now - last_live_activity_at < HEARTBEAT_INTERVAL_SECONDS:
                continue

            heartbeat_seq += 1
            started_at = time.perf_counter()
            trace_id = next_trace_id("H")
            heartbeat_text = build_heartbeat_text(heartbeat_seq - 1)
            line = f"[AI heartbeat] {heartbeat_text}"
            print(line)
            append_output(line)
            mark_live_activity(now)
            await enqueue_audio(trace_id, f"heartbeat{heartbeat_seq}", heartbeat_text, started_at)

    @room.on("DANMU_MSG")
    async def on_danmaku(event: dict) -> None:
        started_at = time.perf_counter()
        mark_live_activity()
        content = event["data"]["info"][1]
        user_name = event["data"]["info"][2][1]
        user_uid = event["data"]["info"][2][0]
        user_key = str(user_uid) if user_uid is not None else user_name
        display_user_name = await resolve_user_name(user_key, user_name)
        print(f"[{display_user_name}]：{content}")

        pending_welcome: WelcomeTask | None = None
        if WELCOME_ON_FIRST_DANMU and user_key not in known_users_in_session:
            known_users_in_session.add(user_key)
            pending_welcome = prepare_welcome_user(
                user_key,
                display_user_name,
                "first_danmu",
                started_at,
            )
        else:
            known_users_in_session.add(user_key)

        llm_started_at = time.perf_counter()
        response = await bot.get_response(content, user_key)
        llm_elapsed = time.perf_counter() - llm_started_at
        response_text = response.text
        spoken_text = response_text
        if pending_welcome is not None:
            spoken_text = f"{pending_welcome.text}{response_text}"
        line = f"[AI回复{display_user_name}]：{response_text}"
        print(line)
        append_output(line)
        trace_id = next_trace_id("R")
        print_timing(
            trace_id,
            f"回复{display_user_name}",
            文本生成=llm_elapsed,
            入队前=time.perf_counter() - started_at,
        )

        await enqueue_audio(trace_id, f"回复{display_user_name}", spoken_text, started_at)

    @room.on("INTERACT_WORD")
    @room.on("INTERACT_WORD_V2")
    @room.on("WELCOME")
    @room.on("WELCOME_GUARD")
    @room.on("ENTRY_EFFECT")
    @room.on("ENTRY_EFFECT_MUST_RECEIVE")
    async def on_user_enter(event: dict) -> None:
        started_at = time.perf_counter()
        mark_live_activity()
        user_info = extract_interact_user(event)
        if user_info is None:
            debug_welcome(f"skip no_user type={event.get('type')}")
            return

        user_key, user_name = user_info
        display_user_name = await resolve_user_name(user_key, user_name)
        if user_key in known_users_in_session:
            debug_welcome(f"skip known_user type={event.get('type')} user={display_user_name}")
            return
        known_users_in_session.add(user_key)
        await maybe_welcome_user(
            user_key,
            display_user_name,
            str(event.get("type")),
            started_at,
        )

    heartbeat_task = asyncio.create_task(heartbeat_worker())

    try:
        await room.connect()
    finally:
        for task in sidecar_watch_tasks:
            task.cancel()
        for task in synth_tasks:
            task.cancel()
        playback_task.cancel()
        heartbeat_task.cancel()
        for task in synth_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        with contextlib.suppress(asyncio.CancelledError):
            await playback_task
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        for task in sidecar_watch_tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        stop_python_sidecar(subtitle_process, subtitle_log_file)
        stop_python_sidecar(subtitle_overlay_process, subtitle_overlay_log_file)
        for audio_file in TMP_AUDIO_DIR.glob("*.mp3"):
            with contextlib.suppress(OSError):
                audio_file.unlink()
        await close_tts()
        await bot.aclose()


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n已停止监听。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
