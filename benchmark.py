import asyncio
import gzip
import json
from pathlib import Path
import sys
import time
import urllib.request
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


env_file = PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=env_file)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.database import DATABASE_URL
from app.graph import AgentState, workflow
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from app.sandbox import run_in_docker_sandbox

POSTGRES_URI = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")

HUMANEVAL_URL = "https://github.com/openai/human-eval/raw/master/data/HumanEval.jsonl.gz"
DATASET_PATH = Path("humaneval.jsonl.gz")


def download_humaneval_if_needed():
    if not DATASET_PATH.exists():
        print(f"[DATASET] Downloading HumanEval from GitHub ({HUMANEVAL_URL})...")
        urllib.request.urlretrieve(HUMANEVAL_URL, DATASET_PATH)
        print("[DATASET] Download complete.")


def load_humaneval_tasks(limit: int = 1) -> list[dict]:
    download_humaneval_if_needed()
    tasks = []
    with gzip.open(DATASET_PATH, "rt", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if idx >= limit:
                break
            tasks.append(json.loads(line))
    return tasks


async def run_benchmark(num_tasks: int = 5):
    print("=" * 70)
    print(f"START BENCHMARK ON {num_tasks} TASKS FROM HUMANEVAL (POSTGRESQL)")
    print("=" * 70 + "\n")

    tasks = load_humaneval_tasks(limit=num_tasks)
    results = []
    session_id = int(time.time())

    stats = {
        "total": len(tasks),
        "pass_at_1": 0,
        "pass_final": 0,
        "security_threats_caught": 0,
        "agent_self_eval_passed": 0,
    }

    start_total_time = time.time()

    async with AsyncPostgresSaver.from_conn_string(POSTGRES_URI) as checkpointer:
        await checkpointer.setup()

        app = workflow.compile(checkpointer=checkpointer)

        for idx, task_data in enumerate(tasks, 1):
            task_id = task_data["task_id"]
            clean_task_name = task_id.replace("/", "_")

            unique_thread_id = f"bench_{session_id}_{clean_task_name}"

            print(f"[{idx}/{len(tasks)}] Task: {task_id} ...", end=" ", flush=True)

            initial_state: AgentState = {
                "task": task_data["prompt"],
                "test_code": "",
                "entry_point": task_data["entry_point"],
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

            config = {
                "configurable": {"thread_id": unique_thread_id},
            }

            try:
                final_state = await app.ainvoke(initial_state, config=config)
            except Exception as exc:
                print(f"AGENT CRASH: {exc}")
                results.append({"task_id": task_id, "status": f"AGENT_CRASH: {exc}"})
                continue

            attempts = final_state.get("attempts", 1)
            final_code = final_state.get("code") or ""
            agent_passed = final_state.get("tests_passed", False)
            findings = final_state.get("findings") or []

            if findings:
                stats["security_threats_caught"] += len(findings)

            if agent_passed:
                stats["agent_self_eval_passed"] += 1


            passed_ground_truth, logs = await run_in_docker_sandbox(
                code=final_code,
                test_code=task_data["test"],
                entry_point=task_data["entry_point"],
                timeout_seconds=5,
            )

            if passed_ground_truth:
                stats["pass_final"] += 1
                if attempts == 1:
                    stats["pass_at_1"] += 1
                print(f"SUCCESS (Attempt {attempts})")
            else:
                print(f"FAILURE (Reference tests did not pass)")

            results.append({
                "task_id": task_id,
                "attempts": attempts,
                "agent_self_eval": agent_passed,
                "ground_truth_pass": passed_ground_truth,
                "findings_count": len(findings),
            })

            await asyncio.sleep(1.5)

    total_time = round(time.time() - start_total_time, 2)

    print("\n" + "=" * 70)
    print("BENCHMARK SUMMARY (BENCHMARK REPORT)")
    print("=" * 70)
    print(f"Total tasks: {stats['total']}")
    print(f"Total time: {total_time} sec (average {round(total_time / stats['total'], 2)} sec/task)")
    print("-" * 70)
    pass_at_1_pct = round(stats["pass_at_1"] / stats["total"] * 100, 1)
    pass_final_pct = round(stats["pass_final"] / stats["total"] * 100, 1)
    print(f"Pass@1 (Solved on first attempt): {stats['pass_at_1']} / {stats['total']} ({pass_at_1_pct}%)")
    print(f"Pass@Final (Solved including repair): {stats['pass_final']} / {stats['total']} ({pass_final_pct}%)")
    print(f"Self-healing gain: +{stats['pass_final'] - stats['pass_at_1']} solved tasks thanks to repair")
    print(f"Semgrep security threats caught: {stats['security_threats_caught']}")
    print(f"Agent self-evaluation accuracy: {stats['agent_self_eval_passed']} / {stats['total']}")
    print("=" * 70)

    output_file = "benchmark_results.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(
            {"session_id": session_id, "stats": stats, "results": results},
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"Results saved successfully to {output_file}")


if __name__ == "__main__":
    asyncio.run(run_benchmark(num_tasks=30))