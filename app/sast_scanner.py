import asyncio
import json
from pathlib import Path
import subprocess
from typing import List
from typing_extensions import TypedDict
import uuid

PROJECT_ROOT = Path(__file__).resolve().parent


class CleanFinding(TypedDict):
    rule_id: str
    line: int
    category: str
    impact: str
    severity: str
    message: str


def _run_semgrep_sync(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


async def run_semgrep_docker(code_string: str) -> List[CleanFinding]:
    file_name = f"_scan_target_{uuid.uuid4().hex}.py"
    temp_target_path = PROJECT_ROOT / file_name

    cmd = [
        "semgrep",
        "--config",
        "p/security-audit",
        "--config",
        "p/secrets",
        str(temp_target_path),
        "--json",
        "--quiet",
    ]

    try:
        temp_target_path.write_text(code_string, encoding="utf-8")
        proc = await asyncio.to_thread(_run_semgrep_sync, cmd)

        if proc.returncode not in (0, 1):
            error_details = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(
                f"Semgrep error (code {proc.returncode}): {error_details}"
            )

        raw_output_text = proc.stdout.strip()
        if not raw_output_text:
            return []

        raw_json = json.loads(raw_output_text)

        clean_findings: List[CleanFinding] = []
        for result in raw_json.get("results", []):
            extra = result.get("extra", {})
            metadata = extra.get("metadata", {})

            clean_findings.append({
                "rule_id": result.get("check_id", "unknown"),
                "line": result.get("start", {}).get("line", 0),
                "category": metadata.get("category", "unknown"),
                "impact": metadata.get("impact", "UNKNOWN"),
                "severity": extra.get("severity", "WARNING"),
                "message": extra.get("message", ""),
            })

        return clean_findings

    finally:
        temp_target_path.unlink(missing_ok=True)