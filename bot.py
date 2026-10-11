import asyncio
import html
import os
from pathlib import Path
import time
from dotenv import load_dotenv
import httpx
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env" if (BASE_DIR / ".env").exists() else BASE_DIR / "app" / ".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "PUT_YOUR_TOKEN_HERE_IF_NOT_IN_ENV")
API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

NODE_ICONS = {
    "START": "🚀 <i>Initializing the task...</i>",
    "generate_code": "🧠 <b>[1/4] Generating code via LLM (Groq)...</b>",
    "validate": "🔍 <b>AST structural validation...</b>",
    "audit": "🛡️ <b>[2/4] Semgrep security audit in Docker...</b>",
    "fixer": "🩹 <b>Fixing identified vulnerabilities...</b>",
    "generate_tests": "✍️ <b>[3/4] QA agent generates assert tests...</b>",
    "sandbox": "🧪 <b>[4/4] Executing Docker sandbox...</b>",
    "repair": "🛠️ <b>Self-healing: agent repairs the algorithm...</b>",
    "END": "🏁 <b>Processing complete!</b>",
}

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Hello! I am an autonomous AI agent for secure software development.</b>\n\n"
        "Send me a task specification, for example:\n"
        "<code>Write a function solve(expr: str) -> int that evaluates expressions like '2 + 2' without using eval.</code>\n\n"
        "I will write the code, check it for vulnerabilities with <b>Semgrep</b>, "
        "generate tests, and run them in a <b>Docker sandbox</b>!",
        parse_mode=ParseMode.HTML,
    )


async def poll_task_progress(chat_id: int, message_id: int, task_id: str):
    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0) as client:
        frame_idx = 0
        last_text = ""
        start_time = time.time()

        while True:
            if time.time() - start_time > 120.0:
                timeout_text = (
                    "⏱ <b>Task polling timed out (exceeded 120 seconds).</b>\n\n"
                    f"Task ID: <code>{task_id}</code>\n"
                    "The agent took too long to complete. Please verify server logs or retry later."
                )
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=timeout_text,
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                return

            await asyncio.sleep(1.5)
            frame_idx = (frame_idx + 1) % len(SPINNER_FRAMES)
            spinner = SPINNER_FRAMES[frame_idx]

            try:
                resp = await client.get(f"/api/v1/tasks/{task_id}")
                if resp.status_code != 200:
                    continue
                data = resp.json()
            except Exception:
                continue

            status = data.get("status")
            current_node = data.get("current_node", "START")
            stage_desc = NODE_ICONS.get(current_node, f"⚙️ Step: {current_node}")
            attempts = data.get("attempts", 0)

            if status == "waiting_for_approval":
                code_preview = html.escape(data.get("code") or "")
                text = (
                    f"⚠️ <b>CODE GENERATED AND VERIFIED WITH SEMGREP</b>\n\n"
                    f"🛡️ <i>Security findings: 0</i>\n"
                    f"Attempt: <b>{attempts} of 3</b>\n\n"
                    f"<b>Code preview:</b>\n"
                    f"<pre><code class=\"language-python\">{code_preview}</code></pre>\n"
                    f"Allow execution in an isolated Docker sandbox?"
                )

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="✅ Approve run", callback_data=f"resume:{task_id}:yes"),
                            InlineKeyboardButton(text="❌ Reject", callback_data=f"resume:{task_id}:no"),
                        ]
                    ]
                )

                if text != last_text:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        reply_markup=keyboard,
                        parse_mode=ParseMode.HTML,
                    )
                return

            if status == "completed":
                final_code = html.escape(data.get("code") or "")
                text = (
                    f"🎉 <b>SOLUTION SUCCESSFULLY CREATED AND VERIFIED!</b>\n\n"
                    f"✅ <i>All unit tests passed in Docker</i>\n"
                    f"🔄 Attempts used: <b>{attempts}</b>\n\n"
                    f"<b>Final verified code:</b>\n"
                    f"<pre><code class=\"language-python\">{final_code}</code></pre>"
                )
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
                return

            if status == "failed":
                err_msg = html.escape(data.get("error_message") or data.get("test_logs") or "Unknown error")
                text = (
                    f"❌ <b>FAILED TO COMPLETE THE TASK</b>\n\n"
                    f"Attempts exhausted: <b>{attempts}</b>\n"
                    f"<b>Error log:</b>\n<pre>{err_msg[:1000]}</pre>"
                )
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                )
                return

            text = (
                f"{spinner} <b>The agent pipeline is running...</b>\n\n"
                f"{stage_desc}\n"
                f"🔄 Current attempt: <b>{attempts}/3</b>"
            )

            if text != last_text:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                    )
                    last_text = text
                except Exception:
                    pass


@dp.message(F.text)
async def handle_user_prompt(message: types.Message):
    prompt_text = message.text.strip()
    status_msg = await message.answer("⏳ <i>Sending the task to the agent queue...</i>", parse_mode=ParseMode.HTML)

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=10.0) as client:
        try:
            resp = await client.post("/api/v1/tasks", json={"task": prompt_text, "entry_point": "solve"})
            if resp.status_code != 202:
                await status_msg.edit_text(f"❌ API error: {resp.text}")
                return
            task_id = resp.json()["task_id"]
        except Exception as e:
            await status_msg.edit_text(f"❌ Failed to connect to the API server: {e}")
            return

    asyncio.create_task(poll_task_progress(message.chat.id, status_msg.message_id, task_id))


@dp.callback_query(F.data.startswith("resume:"))
async def handle_approval(callback: types.CallbackQuery):
    _, task_id, decision = callback.data.split(":")
    approved = (decision == "yes")

    await callback.answer("Decision recorded!")

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=10.0) as client:
        await client.post(
            f"/api/v1/tasks/{task_id}/resume",
            json={"approved": approved, "feedback": None if approved else "Rejected by the user via Telegram"}
        )

    action_text = "✅ <b>Code approved!</b> Starting the Docker sandbox..." if approved else "❌ <b>Code rejected!</b> Sending back for revision..."
    await callback.message.edit_text(f"⏳ {action_text}", parse_mode=ParseMode.HTML)

    asyncio.create_task(poll_task_progress(callback.message.chat.id, callback.message.message_id, task_id))


async def main():
    print("\n[TELEGRAM BOT] Bot started and listening for messages...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())