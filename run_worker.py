import asyncio
from pathlib import Path
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BASE_DIR = Path(__file__).resolve().parent
for p in (str(BASE_DIR), str(BASE_DIR / "app")):
    if p not in sys.path:
        sys.path.insert(0, p)

from app.tasks import broker
from taskiq.api import run_receiver_task


async def main():
    await broker.startup()
    print("\n" + "=" * 50)
    print("[WORKER] Connected to Redis and ready to process tasks!")
    print("=" * 50 + "\n")
    try:
        await run_receiver_task(broker)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[WORKER] Shutting down...")
    finally:
        await broker.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass