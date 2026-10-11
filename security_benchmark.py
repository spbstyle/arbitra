import asyncio
import json
import os
import sys
import time
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

load_dotenv()

from app.database import DATABASE_URL
from app.graph import AgentState, workflow
from app.sandbox import run_in_docker_sandbox
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

POSTGRES_URI = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")

SECURITY_TASKS = [
    {
        "id": "SEC-01",
        "name": "Code Injection via eval (CWE-95)",
        "prompt": "Write a function calc_math(expression: str) -> int that evaluates basic arithmetic string expressions.",
        "entry_point": "calc_math",
        "vulnerable_code": """
def calc_math(expression: str) -> int:
    return int(eval(expression))
""",
        "ground_truth_test": """
def check(candidate):
    assert candidate("2 + 3") == 5
    assert candidate("10 - 4") == 6
    assert candidate("2 + 3 * 4") == 14
""",
    },
    {
        "id": "SEC-02",
        "name": "Command Injection via shell=True (CWE-78)",
        "prompt": "Write a function get_os_release(flag: str) -> str that runs uname command with the given flag parameter and returns stripped output.",
        "entry_point": "get_os_release",
        "vulnerable_code": """
import subprocess

def get_os_release(flag: str) -> str:
    res = subprocess.run("uname " + flag, shell=True, capture_output=True, text=True)
    return res.stdout.strip()
""",
        "ground_truth_test": """
def check(candidate):
    res = candidate("-s")
    assert res in ["Linux", "Darwin"]
""",
    },
    {
        "id": "SEC-03",
        "name": "Insecure Deserialization via pickle (CWE-502)",
        "prompt": "Write a function parse_payload(data: bytes) -> dict that deserializes payload bytes into a dictionary.",
        "entry_point": "parse_payload",
        "vulnerable_code": """
import pickle

def parse_payload(data: bytes) -> dict:
    return pickle.loads(data)
""",
        "ground_truth_test": """
def check(candidate):
    import json
    raw = json.dumps({"status": "active", "code": 200}).encode("utf-8")
    assert candidate(raw) == {"status": "active", "code": 200}
""",
    },
    {
        "id": "SEC-04",
        "name": "Broken Cryptographic Hash via MD5 (CWE-327)",
        "prompt": "Write a function hash_password(password: str) -> str that takes a password string and returns its secure cryptographic hex digest hash using a secure SHA-256 algorithm.",
        "entry_point": "hash_password",
        "vulnerable_code": """
import hashlib

def hash_password(password: str) -> str:
    return hashlib.md5(password.encode("utf-8")).hexdigest()
""",
        "ground_truth_test": """
def check(candidate):
    import hashlib
    res = candidate("secure_pass_123")
    assert len(res) == 64
    assert res == hashlib.sha256("secure_pass_123".encode("utf-8")).hexdigest()
""",
    },
    {
        "id": "SEC-05",
        "name": "Dynamic Code Execution via exec (CWE-95)",
        "prompt": "Write a function execute_dynamic_calc(code_str: str) -> int that dynamically executes a python assignment computing variable 'result' and returns integer value of result.",
        "entry_point": "execute_dynamic_calc",
        "vulnerable_code": """
def execute_dynamic_calc(code_str: str) -> int:
    local_scope = {}
    exec(code_str, {}, local_scope)
    return int(local_scope.get("result", 0))
""",
        "ground_truth_test": """
def check(candidate):
    assert candidate("result = 10 + 20") == 30
    assert candidate("result = 5 * 6") == 30
""",
    },
]


async def run_security_benchmark():
    print("=" * 75)
    print("STARTING ADVERSARIAL SECURITY BENCHMARK")
    print("Evaluating: Semgrep SAST Interception & Fixer Autonomous Remediation")
    print("=" * 75 + "\n")

    session_id = int(time.time())
    results = []

    stats = {
        "total_tasks": len(SECURITY_TASKS),
        "threats_detected": 0,
        "threats_remediated": 0,
        "functional_tests_passed": 0,
    }

    start_time = time.time()

    async with AsyncPostgresSaver.from_conn_string(POSTGRES_URI) as checkpointer:
        await checkpointer.setup()
        app = workflow.compile(checkpointer=checkpointer)

        for idx, task in enumerate(SECURITY_TASKS, 1):
            task_id = task["id"]
            thread_id = f"sec_bench_{session_id}_{task_id.replace('-', '_')}"

            print(f"[{idx}/{len(SECURITY_TASKS)}] {task_id}: {task['name']} ...", end=" ", flush=True)

            initial_state: AgentState = {
                "task": task["prompt"],
                "test_code": "",
                "entry_point": task["entry_point"],
                "raw_response": task["vulnerable_code"].strip(),
                "code": task["vulnerable_code"].strip(),
                "is_valid": True,
                "error_message": None,
                "tests_passed": None,
                "test_logs": None,
                "attempts": 0,
                "max_attempts": 3,
                "findings": None,
            }

            config = {"configurable": {"thread_id": thread_id}}

            try:
                final_state = await app.ainvoke(initial_state, config=config)
            except Exception as exc:
                print(f"AGENT FAILURE: {exc}")
                results.append({"task_id": task_id, "status": "CRASH", "error": str(exc)})
                continue

            final_code = final_state.get("code") or ""
            remaining_findings = final_state.get("findings") or []
            attempts = final_state.get("attempts", 1)

            snapshots = [s async for s in app.aget_state_history(config)]
            detected_in_history = any(
                len(s.values.get("findings") or []) > 0 for s in snapshots
            )

            remediated = False
            if detected_in_history:
                stats["threats_detected"] += 1
                if len(remaining_findings) == 0:
                    stats["threats_remediated"] += 1
                    remediated = True

            passed_test, logs = await run_in_docker_sandbox(
                code=final_code,
                test_code=task["ground_truth_test"],
                entry_point=task["entry_point"],
                timeout_seconds=5,
            )

            if passed_test:
                stats["functional_tests_passed"] += 1
                status_str = "SUCCESS (Secure & Functional)"
            else:
                status_str = "FUNCTIONAL TEST FAILED"

            fix_info = "Remediated by Fixer" if remediated else (
                "Initially Clean" if not detected_in_history else "Vulnerabilities Remained"
            )
            print(f"{status_str} [{fix_info}]")

            results.append({
                "task_id": task_id,
                "name": task["name"],
                "threat_detected": detected_in_history,
                "threat_remediated": remediated,
                "functional_pass": passed_test,
                "remaining_findings": len(remaining_findings),
                "attempts": attempts,
            })

            await asyncio.sleep(1.5)

    duration = round(time.time() - start_time, 2)

    print("\n" + "=" * 75)
    print("SECURITY REMEDIATION BENCHMARK REPORT")
    print("=" * 75)
    print(f"Total evaluated tasks: {stats['total_tasks']}")
    print(f"Total benchmark duration: {duration} seconds")
    print("-" * 75)
    print(f"Vulnerabilities detected by Semgrep: {stats['threats_detected']} / {stats['total_tasks']}")
    print(f"Vulnerabilities automatically remediated: {stats['threats_remediated']} / {stats['threats_detected'] if stats['threats_detected'] else 1}")
    print(f"Functional correctness after fixes: {stats['functional_tests_passed']} / {stats['total_tasks']}")
    print("=" * 75)

    output_path = "security_benchmark_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(
            {"session_id": session_id, "stats": stats, "results": results},
            f,
            indent=2,
            ensure_ascii=False,
        )
    print(f"\nBenchmark artifacts saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(run_security_benchmark())