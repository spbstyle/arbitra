import asyncio
from contextlib import asynccontextmanager
import sys
from typing import Any, Dict, List, Literal, Optional
import uuid

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv

env_file = ".env"

load_dotenv(dotenv_path=env_file)

from app.database import DATABASE_URL, async_session_maker
from fastapi import FastAPI, HTTPException, status
from app.graph import workflow
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from app.models import Task
from pydantic import BaseModel, Field
from app.tasks import broker, resume_agent_task, run_agent_task

POSTGRES_URI = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
langgraph_app = None

from app.database import engine
from app.models import Base


@asynccontextmanager
async def lifespan(app: FastAPI):
    global langgraph_app

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await broker.startup()

    async with AsyncPostgresSaver.from_conn_string(POSTGRES_URI) as checkpointer:
        await checkpointer.setup()
        langgraph_app = workflow.compile(
            checkpointer=checkpointer, interrupt_before=["sandbox"]
        )
        yield

    await broker.shutdown()


app = FastAPI(
    title="Secure Code Generation Agent API",
    version="1.0.0",
    lifespan=lifespan,
)


class CreateTaskRequest(BaseModel):
    task: str = Field(..., description="Task specification for code generation")
    entry_point: Optional[str] = Field("solve", description="Target function name")


class TaskStatusResponse(BaseModel):
    task_id: str
    status: Literal["running", "waiting_for_approval", "completed", "failed"]
    current_node: Optional[str]
    attempts: int
    code: Optional[str]
    findings: Optional[List[Dict[str, Any]]]
    test_code: Optional[str]
    tests_passed: Optional[bool]
    error_message: Optional[str]
    test_logs: Optional[str] = None


class ResumeTaskRequest(BaseModel):
    approved: bool = Field(..., description="Approve running the code in the sandbox?")
    feedback: Optional[str] = Field(None, description="Comment in case of rejection")


@app.post(
    "/api/v1/tasks",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=Dict[str, str],
)
async def create_task(req: CreateTaskRequest):
    task_id = str(uuid.uuid4())

    async with async_session_maker() as session:
        new_task = Task(
            id=uuid.UUID(task_id),
            prompt=req.task,
            entry_point=req.entry_point or "solve",
            status="running",
        )
        session.add(new_task)
        await session.commit()

    await run_agent_task.kiq(
        task_id=task_id,
        prompt=req.task,
        entry_point=req.entry_point or "solve",
    )

    return {
        "task_id": task_id,
        "status": "running",
        "message": "Task added to the Redis queue.",
    }


@app.get("/api/v1/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    config = {"configurable": {"thread_id": task_id}}
    snapshot = await langgraph_app.aget_state(config)

    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Task not found")

    state = snapshot.values
    next_nodes = snapshot.next or ()

    if "sandbox" in next_nodes:
        task_status = "waiting_for_approval"
    elif next_nodes:
        task_status = "running"
    else:
        task_status = "completed" if state.get("tests_passed") else "failed"

    return TaskStatusResponse(
        task_id=task_id,
        status=task_status,
        current_node=next_nodes[0] if next_nodes else "END",
        attempts=state.get("attempts", 0),
        code=state.get("code"),
        findings=state.get("findings"),
        test_code=state.get("test_code"),
        tests_passed=state.get("tests_passed"),
        error_message=state.get("error_message"),
        test_logs=state.get("test_logs"),
    )


@app.post("/api/v1/tasks/{task_id}/resume", response_model=Dict[str, str])
async def resume_task(task_id: str, req: ResumeTaskRequest):
    config = {"configurable": {"thread_id": task_id}}
    snapshot = await langgraph_app.aget_state(config)

    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Task not found")

    if not snapshot.next:
        raise HTTPException(status_code=400, detail="Task already completed.")

    async with async_session_maker() as session:
        task_record = await session.get(Task, uuid.UUID(task_id))
        if task_record:
            task_record.status = "running"
            await session.commit()

    await resume_agent_task.kiq(
        task_id=task_id,
        approved=req.approved,
        feedback=req.feedback,
    )

    return {
        "task_id": task_id,
        "status": "running",
        "message": "Command queued to the worker via Redis.",
    }