import asyncio
import contextlib
import os
from pathlib import Path
import subprocess
import sys
import time

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from bilibili_api import live, select_client

from llm_bot import LLMChatBot
from simple_bot import SimpleChatBot
from tts_engine import close_tts, describe_settings, synthesize_to_file


OUTPUT_PATH = Path("output.txt")
AUDIO_PATH = "output.mp3"


def _read_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _read_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


WELCOME_ENABLED = _read_bool("WELCOME_ENABLED", True)
WELCOME_MIN_INTERVAL_SECONDS = _read_float("WELCOME_MIN_INTERVAL_SECONDS", 8.0)
WELCOME_USER_COOLDOWN_SECONDS = _read_float("WELCOME_USER_COOLDOWN_SECONDS", 300.0)
WELCOME_MAX_AUDIO_QUEUE = _read_int("WELCOME_MAX_AUDIO_QUEUE", 2)
WELCOME_ON_FIRST_DANMU = _read_bool("WELCOME_ON_FIRST_DANMU", True)
WELCOME_DEBUG = _read_bool("WELCOME_DEBUG", False)
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


def extract_user_identity(payload: dict | None) -> tuple[str, str] | None:
    if not isinstance(payload, dict):
        return None

    user_name = ""
    for key in ("uname", "username", "user_name", "nick_name", "nick"):
        value = payload.get(key)
        if value:
            user_name = str(value).strip()
            break

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
    print(f"欢迎词：{'开启' if WELCOME_ENABLED else '关闭'}")


async def audio_worker(audio_queue: asyncio.Queue[str]) -> None:
    while True:
        response_text = await audio_queue.get()
        try:
            await synthesize_to_file(response_text, AUDIO_PATH)
            await asyncio.to_thread(
                subprocess.run,
                ["mpv.exe", AUDIO_PATH],
                check=False,
            )
        except Exception as error:
            print(f"[音频错误]：{error}")
        finally:
            audio_queue.task_done()


async def run(
    room_id: int | None = None,
    auto_disconnect_after: float | None = None,
) -> None:
    print_banner()
    select_client("aiohttp")

    if room_id is None:
        room_id = int(input("请输入直播间编号: "))

    room = live.LiveDanmaku(room_id)
    audio_queue: asyncio.Queue[str] = asyncio.Queue()
    worker_task = asyncio.create_task(audio_worker(audio_queue))
    last_welcome_time = 0.0
    last_welcome_by_user: dict[str, float] = {}
    known_users_in_session: set[str] = set()

    if auto_disconnect_after is not None:

        async def disconnect_later() -> None:
            await asyncio.sleep(auto_disconnect_after)
            if room.get_status() == room.STATUS_ESTABLISHED:
                await room.disconnect()

        asyncio.create_task(disconnect_later())

    async def maybe_welcome_user(user_key: str, user_name: str, source: str) -> bool:
        nonlocal last_welcome_time

        if not WELCOME_ENABLED:
            debug_welcome(f"skip disabled source={source} user={user_name}")
            return False

        now = time.monotonic()
        if now - last_welcome_time < WELCOME_MIN_INTERVAL_SECONDS:
            debug_welcome(f"skip rate_limit source={source} user={user_name}")
            return False
        if now - last_welcome_by_user.get(user_key, 0.0) < WELCOME_USER_COOLDOWN_SECONDS:
            debug_welcome(f"skip cooldown source={source} user={user_name}")
            return False
        if audio_queue.qsize() > WELCOME_MAX_AUDIO_QUEUE:
            debug_welcome(f"skip queue_busy source={source} user={user_name}")
            return False

        if len(last_welcome_by_user) > 200:
            cutoff = now - WELCOME_USER_COOLDOWN_SECONDS
            stale_keys = [
                key for key, timestamp in last_welcome_by_user.items() if timestamp < cutoff
            ]
            for key in stale_keys:
                last_welcome_by_user.pop(key, None)

        welcome_text = build_welcome_text(user_name)
        last_welcome_time = now
        last_welcome_by_user[user_key] = now
        known_users_in_session.add(user_key)

        line = f"[欢迎{user_name}]：{welcome_text}"
        print(line)
        append_output(line)
        debug_welcome(f"sent source={source} user={user_name}")

        await audio_queue.put(welcome_text)
        return True

    @room.on("DANMU_MSG")
    async def on_danmaku(event: dict) -> None:
        content = event["data"]["info"][1]
        user_name = event["data"]["info"][2][1]
        user_uid = event["data"]["info"][2][0]
        user_key = str(user_uid) if user_uid is not None else user_name
        print(f"[{user_name}]：{content}")

        if WELCOME_ON_FIRST_DANMU and user_key not in known_users_in_session:
            known_users_in_session.add(user_key)
            await maybe_welcome_user(user_key, user_name, "first_danmu")
        else:
            known_users_in_session.add(user_key)

        response = await bot.get_response(content, user_name)
        response_text = response.text
        line = f"[AI回复{user_name}]：{response_text}"
        print(line)
        append_output(line)

        await audio_queue.put(response_text)

    @room.on("INTERACT_WORD")
    @room.on("INTERACT_WORD_V2")
    @room.on("WELCOME")
    @room.on("WELCOME_GUARD")
    @room.on("ENTRY_EFFECT")
    @room.on("ENTRY_EFFECT_MUST_RECEIVE")
    async def on_user_enter(event: dict) -> None:
        user_info = extract_interact_user(event)
        if user_info is None:
            debug_welcome(f"skip no_user type={event.get('type')}")
            return

        user_key, user_name = user_info
        if user_key in known_users_in_session:
            debug_welcome(f"skip known_user type={event.get('type')} user={user_name}")
            return
        known_users_in_session.add(user_key)
        await maybe_welcome_user(user_key, user_name, str(event.get("type")))

    try:
        await room.connect()
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
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
