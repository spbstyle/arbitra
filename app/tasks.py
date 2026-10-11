import asyncio
import os
from pathlib import Path
import sys
from typing import Optional
import uuid

APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
for p in (str(APP_DIR), str(PROJECT_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv

env_file = PROJECT_ROOT / ".env"

load_dotenv(dotenv_path=env_file)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.database import DATABASE_URL, async_session_maker
from app.graph import AgentState, workflow
from langfuse import get_client
from langfuse.langchain import CallbackHandler
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from app.models import Run, Task
from sqlalchemy import update
from taskiq_redis import ListQueueBroker

POSTGRES_URI = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

broker = ListQueueBroker(
    url=REDIS_URL,
    socket_timeout=None,
    socket_connect_timeout=None,
)


async def sync_task_to_db(task_id: str, new_status: str, state: Optional[dict] = None):
    task_uuid = uuid.UUID(task_id)
    async with async_session_maker() as session:
        await session.execute(
            update(Task).where(Task.id == task_uuid).values(status=new_status)
        )

        if state and new_status in ("completed", "failed"):
            run_record = Run(
                task_id=task_uuid,
                final_code=state.get("code"),
                tests_passed=bool(state.get("tests_passed")),
                attempts=state.get("attempts", 1),
                test_logs=state.get("test_logs"),
                error_message=state.get("error_message"),
            )
            session.add(run_record)

        await session.commit()


@broker.task(task_name="run_agent_task")
async def run_agent_task(task_id: str, prompt: str, entry_point: str = "solve"):
    print(f"\n[WORKER] Starting task {task_id}")
    langfuse_handler = CallbackHandler()

    config = {
        "configurable": {"thread_id": task_id},
        "callbacks": [langfuse_handler],
        "run_name": "worker_agent_task",
        "metadata": {"task_id": task_id},
    }

    initial_state: AgentState = {
        "task": prompt,
        "test_code": "",
        "entry_point": entry_point,
        "raw_response": None,
        "code": None,
        "is_valid": None,
        "error_message": None,
        "tests_passed": None,
        "test_logs": None,
        "attempts": 0,
        "max_attempts": 3,
        "findings": None,
    }

    async with AsyncPostgresSaver.from_conn_string(POSTGRES_URI) as checkpointer:
        app = workflow.compile(checkpointer=checkpointer, interrupt_before=["sandbox"])

        try:
            await app.ainvoke(initial_state, config=config)
        except Exception as exc:
            print(f"[WORKER ERROR {task_id}]: {exc}")
            await app.aupdate_state(config, {"error_message": f"Worker error: {exc}"})
        finally:
            get_client().flush()

        snapshot = await app.aget_state(config)
        state = snapshot.values or {}
        next_nodes = snapshot.next or ()

        if "sandbox" in next_nodes:
            print(f"[WORKER {task_id}] Paused for approval (WAITING_FOR_APPROVAL)")
            await sync_task_to_db(task_id, "waiting_for_approval")
        elif not next_nodes:
            final_status = "completed" if state.get("tests_passed") else "failed"
            print(f"[WORKER {task_id}] Finished with status: {final_status.upper()}")
            await sync_task_to_db(task_id, final_status, state)


@broker.task(task_name="resume_agent_task")
async def resume_agent_task(task_id: str, approved: bool, feedback: Optional[str] = None):
    print(f"\n[WORKER] Resuming task {task_id} (Approved: {approved})")
    langfuse_handler = CallbackHandler()

    config = {
        "configurable": {"thread_id": task_id},
        "callbacks": [langfuse_handler],
        "run_name": "worker_resume_task",
        "metadata": {"task_id": task_id},
    }

    async with AsyncPostgresSaver.from_conn_string(POSTGRES_URI) as checkpointer:
        app = workflow.compile(checkpointer=checkpointer, interrupt_before=["sandbox"])

        if not approved:
            feedback_text = feedback or "Code rejected by the reviewer."
            rejection_patch = {
                "test_logs": f"Human supervisor rejected: {feedback_text}",
                "is_valid": False,
            }
            await app.aupdate_state(config, rejection_patch)

        try:
            await app.ainvoke(None, config=config)
        except Exception as exc:
            print(f"[WORKER ERROR {task_id}]: {exc}")
            await app.aupdate_state(config, {"error_message": f"Worker error: {exc}"})
        finally:
            get_client().flush()

        snapshot = await app.aget_state(config)
        state = snapshot.values or {}
        next_nodes = snapshot.next or ()

        if "sandbox" in next_nodes:
            await sync_task_to_db(task_id, "waiting_for_approval")
        elif not next_nodes:
            final_status = "completed" if state.get("tests_passed") else "failed"
            print(f"[WORKER {task_id}] Finished with status: {final_status.upper()}")
            await sync_task_to_db(task_id, final_status, state)