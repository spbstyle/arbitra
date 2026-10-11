import asyncio
import re
import shutil
import subprocess
import traceback


def enrich_traceback_with_code(full_script: str, traceback_str: str) -> str:
    matches = re.findall(r'File "<stdin>", line (\d+)', traceback_str)
    if not matches:
        return traceback_str

    script_lines = full_script.splitlines()
    failing_line_num = int(matches[-1])

    if 1 <= failing_line_num <= len(script_lines):
        failing_code = script_lines[failing_line_num - 1].strip()
        enriched_info = (
            f"{traceback_str.strip()}\n\n"
            "[ERROR PARSER ERROR DETAILS]:\n"
            f"The test failed on line {failing_line_num}:\n"
            f"--> {failing_code}"
        )
        return enriched_info

    return traceback_str


def _sync_run_docker(full_script: str, timeout_seconds: int) -> tuple[bool, str]:
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        "none",
        "--memory",
        "256m",
        "--cpus",
        "1.0",
        "--pids-limit",
        "64",
        "--tmpfs",
        "/tmp",
        "python:3.11-slim",
        "python",
        "-",
    ]

    try:
        proc = subprocess.run(
            docker_cmd,
            input=full_script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )

        if proc.returncode == 0:
            return True, proc.stdout or "Execution successful"
        else:
            raw_error = (proc.stderr or proc.stdout or "").strip()
            if not raw_error:
                raw_error = f"The container exited with code {proc.returncode} (possibly due to OOM kill)."

            enriched_error = enrich_traceback_with_code(full_script, raw_error)
            return False, enriched_error

    except subprocess.TimeoutExpired:
        return (
            False,
            f"TimeoutError: Execution exceeded the limit of {timeout_seconds} seconds.",
        )
    except Exception as exc:
        traceback.print_exc()
        return False, f"SandboxError [{type(exc).__name__}]: {exc or repr(exc)}"


async def run_in_docker_sandbox(
        code: str,
        test_code: str,
        entry_point: str | None = None,
        timeout_seconds: int = 5,
) -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "SandboxError: 'docker' was not found in the system PATH."
    solution_mock = (
        "import sys\n"
        "sys.modules['solution'] = sys.modules['__main__']\n\n"
    )
    full_script = f"{solution_mock}{code}\n\n# --- TESTS ---\n{test_code}\n"

    return await asyncio.to_thread(_sync_run_docker, full_script, timeout_seconds)