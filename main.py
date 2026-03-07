import asyncio
import contextlib
import subprocess
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from bilibili_api import live, select_client

from llm_bot import LLMChatBot
from simple_bot import SimpleChatBot
from tts_engine import close_tts, describe_settings, synthesize_to_file


local_bot = SimpleChatBot(
    "Xzai",  # 聊天机器人名字
    database_path="db.sqlite3",  # 数据库文件，用于存储训练后的问答对
)
bot = LLMChatBot(local_fallback=local_bot)


def print_banner() -> None:
    # 版权信息，就别删了吧
    print("--------------------")
    print("作者：Xzai")
    print("QQ：2744601427")
    print("--------------------")
    print(f"回复链路：{bot.describe()}")
    print(f"语音：{describe_settings()}")


async def audio_worker(audio_queue: asyncio.Queue[str]) -> None:
    while True:
        response_text = await audio_queue.get()
        try:
            await synthesize_to_file(response_text, "output.mp3")
            await asyncio.to_thread(
                subprocess.run,
                ["mpv.exe", "output.mp3"],
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

    if auto_disconnect_after is not None:

        async def disconnect_later() -> None:
            await asyncio.sleep(auto_disconnect_after)
            if room.get_status() == room.STATUS_ESTABLISHED:
                await room.disconnect()

        asyncio.create_task(disconnect_later())

    @room.on("DANMU_MSG")  # 弹幕消息事件回调函数
    async def on_danmaku(event):
        """
        处理弹幕消息
        :param event: 弹幕消息事件
        """
        content = event["data"]["info"][1]  # 获取弹幕内容
        user_name = event["data"]["info"][2][1]  # 获取用户昵称
        print(f"[{user_name}]: {content}")

        response = await bot.get_response(content, user_name)
        response_text = response.text
        print(f"[AI回复{user_name}]：{response_text}")

        with open("./output.txt", "a", encoding="utf-8") as file:
            file.write(f"[AI回复{user_name}]：{response_text}\n")

        await audio_queue.put(response_text)

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
