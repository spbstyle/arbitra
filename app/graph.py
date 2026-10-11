import ast
import re
import os
from typing import Literal, Optional, TypedDict

from app.database import DATABASE_URL
from app.sast_scanner import run_semgrep_docker
from dotenv import load_dotenv
from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph
from app.sandbox import run_in_docker_sandbox
from app.interface_extractor import extract_interface

load_dotenv()

TEST_GENERATOR_PROMPT = """You are a Senior QA Automation Engineer specializing in Python testing.
Your task is to write a comprehensive, strict set of autonomous checks (`assert`) to validate the implementation against the task requirements.

--- TECHNICAL REQUIREMENTS ---
{task}

--- FIXED INTERFACE (CONTRACT) ---
{interface_stub}

--- TARGET ENTRY POINT ---
Function: `{entry_point}`

--- CRITICAL TEST REQUIREMENTS ---
1. STRICT CONTRACT COMPLIANCE:
   - Call ONLY functions from the “FIXED INTERFACE” block.
   - Follow the exact parameter names and types. Do not invent arguments that are not in the signature.
   - DO NOT try to declare or implement the function `{entry_point}` again! It is already imported and implemented.

2. COVERAGE (5–8 INDEPENDENT CHECKS):
   - Basic scenarios (Happy Path): validate the standard correct behavior according to the task specification.
   - Edge cases: empty structures (if allowed), zeros, negative values, single items, large values.
   - Operation order and precedence (if applicable to the logic of the task).

3. DETERMINISM:
   - Tests must be fast and reproducible.
   - Do not use time.sleep(), random, network sockets, or heavy third-party libraries.
   - Use standard `assert` statements with clear error messages:
     `assert {entry_point}(...) == expected_result, "Error description"`

4. OUTPUT RULES:
   - Return ONLY executable Python test code.
   - NO markdown formatting (no ```python ... ```).
   - NO introductory text, comments, or explanations.
   
- Do not write `from solution import ...` or `import solution`! The function `{entry_point}` is ALREADY imported and available directly.
"""

DEFAULT_POSTGRES_URI = os.getenv(
    DATABASE_URL,
    "postgresql+psycopg://postgres:postgres@localhost:5432/secure_code_agent"
)

class AgentState(TypedDict):
    task: str
    test_code: str
    entry_point: Optional[str]
    raw_response: Optional[str]
    code: Optional[str]
    is_valid: Optional[bool]
    error_message: Optional[str]
    tests_passed: Optional[bool]
    test_logs: Optional[str]
    attempts: int
    max_attempts: int
    findings: Optional[list[dict]]


llm = ChatGroq(model="openai/gpt-oss-120b", temperature=0.0)


async def generate_code_node(state: AgentState) -> dict:
    if state.get("raw_response") or state.get("code"):
        return {"attempts": state.get("attempts", 1)}

    prompt = (
        "You are a Senior Python Software Engineer. Solve the following task:\n\n"
        "--- TASK SPECIFICATION ---\n"
        f"{state['task']}\n\n"
        "--- CRITICAL IMPLEMENTATION RULES ---\n"
        "1. KISS PRINCIPLE (KEEP IT SIMPLE, NO OVER-ENGINEERING):\n"
        "   - Write minimal, elegant, and idiomatic Python code.\n"
        "   - Do NOT build complex custom lexers, tokenizers, or AST interpreters from scratch.\n"
        "   - NEVER use 'eval()' or 'exec()'. If evaluating math/logical expressions is needed, "
        "rely on standard library tools (e.g., 'ast.parse(expr, mode=\"eval\")' or 'ast.literal_eval()').\n\n"
        "2. EDGE CASE RESILIENCE:\n"
        "   - Handle boundary conditions: negative numbers, unary signs (e.g., '-5 + 10'), zeros, empty inputs, and irregular whitespace/newlines.\n"
        "   - Ensure stack or list operations never cause IndexError or KeyError on edge cases.\n\n"
        "3. SIGNATURE & IMPORTS:\n"
        "   - Strictly preserve the requested function name, parameter types, and return type.\n"
        "   - Include all necessary imports at the very beginning (e.g., from typing import ..., import ast, import re).\n\n"
        "4. OUTPUT FORMAT:\n"
        "   - Return ONLY raw, valid, executable Python code.\n"
        "   - Do NOT include markdown code blocks (no ```python). No conversational text or explanations."
    )

    try:
        print("\n[GENERATION] ──► Attempt #1: Initial request to the model...")
        res = await llm.ainvoke(prompt)
        print("\n--- CODE GENERATED ON ATTEMPT #1 ---")
        print(res.content)
        print("-----------------------------------\n")
        return {"raw_response": str(res.content), "attempts": 1}
    except Exception as exc:
        return {
            "raw_response": None,
            "error_message": f"LLM Generation Error: {exc}",
            "attempts": 1,
        }


def extract_and_validate(raw_text: str) -> tuple[str, bool, Optional[str]]:
    pattern = r"```(?:python)?\s*\n(.*?)```"
    matches = re.findall(pattern, raw_text, re.DOTALL)
    code = max(matches, key=len).strip() if matches else raw_text.strip()

    try:
        ast.parse(code)
        return code, True, None
    except SyntaxError as exc:
        return code, False, f"SyntaxError (line {exc.lineno}): {exc.msg}"


def validate_ast_node(state: AgentState) -> dict:
    raw = state.get("raw_response")
    if not raw:
        return {
            "code": None,
            "is_valid": False,
            "error_message": state.get("error_message")
            or "Model response is empty.",
        }

    clean_code, is_valid, err = extract_and_validate(raw)
    return {"code": clean_code, "is_valid": is_valid, "error_message": err}

def extract_and_validate_tests(
    raw_text: str,
) -> tuple[str, bool, Optional[str]]:
  raw_text = (
      raw_text.replace("\u2011", "-")  # Non-breaking hyphen -> regular hyphen
      .replace("\u2013", "-")  # Short dash -> regular hyphen
      .replace("\u2014", "-")  # Long dash -> regular hyphen
      .replace("\u00a0", " ")  # Non-breaking space -> regular space
      .replace("“", '"')  # Curly quotes -> regular quotes
      .replace("”", '"')
  )
  pattern = r"```(?:python)?\s*\n(.*?)```"
  matches = re.findall(pattern, raw_text, re.DOTALL)
  test_code = max(matches, key=len).strip() if matches else raw_text.strip()
  cleaned_lines = [
      line for line in test_code.splitlines()
      if not re.match(r'^\s*(?:from\s+solution\s+import|import\s+solution\b)', line)
  ]
  test_code = "\n".join(cleaned_lines).strip()

  if not test_code:
    return "", False, "Tests are empty."

  try:
    tree = ast.parse(test_code)
  except SyntaxError as exc:
    return (
        test_code,
        False,
        f"SyntaxError in generated tests (line {exc.lineno}):"
        f" {exc.msg}",
    )

  has_assert = any(isinstance(node, ast.Assert) for node in ast.walk(tree))
  if not has_assert:
    return (
        test_code,
        False,
        "Generated tests do not contain 'assert' checks.",
    )

  has_func_def = any(
      isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
      for node in tree.body
  )
  if has_func_def:
    return (
        test_code,
        False,
        "The tester attempted to declare a function instead of writing assert checks.",
    )

  return test_code, True, None


async def generate_tests_node(state: AgentState) -> dict:
  code = state.get("code")
  task = state.get("task")
  if state.get("test_code"):
      return {}
  if not code or not state.get("is_valid"):
    return {
        "test_code": "",
        "error_message": "Unable to generate tests: code is missing or invalid.",
    }

  try:
    interface_stub, entry_point = extract_interface(code)
    target_function = entry_point or state.get("entry_point", "solve")

    prompt = (
        TEST_GENERATOR_PROMPT.replace("{task}", task)
        .replace("{interface_stub}", interface_stub)
        .replace("{entry_point}", target_function)
    )

    print("\n[TEST DESIGN] ──► Generating independent assert tests...")
    res = await llm.ainvoke(prompt)
    clean_tests, is_valid, err = extract_and_validate_tests(str(res.content))

    if not is_valid:
        print(f"[TEST DESIGN] ──► TEST FAILURE: {err}")
        return {
            "test_code": "",
            "error_message": f"Invalid Tests: {err}",
        }

    print("--- GENERATED VALID TESTS ---")
    print(clean_tests)
    print("--------------------------------------\n")

    return {
        "test_code": clean_tests,
        "entry_point": target_function,
    }

  except Exception as exc:
    print(f"[TEST DESIGN] ──► Error generating tests: {exc}")
    return {
        "test_code": "",
        "error_message": f"Test Generation Error: {exc}",
    }

async def execute_sandbox_node(state: AgentState) -> dict:
    attempt = state.get("attempts", 1)
    if not state.get("is_valid") or not state.get("code"):
        current_logs = (
                state.get("test_logs")
                or f"Syntax error before execution: {state.get('error_message')}"
        )
        return {
            "tests_passed": False,
            "test_logs": current_logs,
        }

    passed, logs = await run_in_docker_sandbox(
        code=state["code"],
        test_code=state["test_code"],
        entry_point=state.get("entry_point"),
        timeout_seconds=10,
    )
    if passed:
        print(f"[SANDBOX] ──► Attempt #{attempt}: TESTS PASSED SUCCESSFULLY")
    else:
        print(f"[SANDBOX] ──► Attempt #{attempt}: TESTS FAILED")
        print(f"--- SANDBOX ERROR LOG ---\n{logs}\n-------------------------------")
    return {"tests_passed": passed, "test_logs": logs}




async def repair_code_node(state: AgentState) -> dict:
    current_attempt = state.get("attempts", 1) + 1

    prompt = (
        f"Your previous code for the task failed with an error.\n\n"
        f"--- ORIGINAL TASK ---\n{state['task']}\n\n"
        f"--- YOUR NON-WORKING CODE ---\n{state['code']}\n\n"
        f"--- ERROR TRACEBACK FROM SANDBOX ---\n{state['test_logs']}\n\n"
        "Fixing instructions:\n"
        "1. Carefully inspect the traceback error (NameError, AssertionError, SyntaxError, or Timeout).\n"
        "2. Fix the root cause (for example, add missing imports or correct the algorithm logic).\n"
        "3. Return ONLY the corrected working solution code. No markdown and no explanations."
    )

    try:
        res = await llm.ainvoke(prompt)
        return {
            "raw_response": str(res.content),
            "attempts": current_attempt,
        }
    except Exception as exc:
        return {
            "raw_response": None,
            "error_message": f"LLM Repair Failed: {exc}",
            "attempts": current_attempt,
        }


def should_repair(state: AgentState) -> Literal["repair", "__end__"]:
    if state.get("tests_passed"):
        return "__end__"

    current_attempts = state.get("attempts", 1)
    max_attempts = state.get("max_attempts", 3)
    if current_attempts >= max_attempts:
        return "__end__"

    return "repair"



async def audit(state: AgentState):
    code = state.get("code")
    if not code or not state.get("is_valid"):
        print("[AUDIT] ──► Skipping audit: code is missing or syntactically invalid.")
        return {"findings": []}

    try:
        findings = await run_semgrep_docker(code)

        if findings:
            print(f"\n[AUDIT] ──► VULNERABILITIES FOUND: {len(findings)}")
            for idx, f in enumerate(findings, 1):
                print(
                    f"  {idx}. [Line {f.get('line')}] [{f.get('severity')}] {f.get('rule_id')}"
                )
                print(f"     Description: {f.get('message')}")
        else:
            print("[AUDIT] ──► Semgrep check passed: no vulnerabilities found.")

        return {"findings": findings}

    except Exception as exc:
        print(f"[AUDIT] ──► SEMGREP TOOL FAILURE: {exc}")
        return {
            "findings": [
                {
                    "rule_id": "audit-error",
                    "line": 0,
                    "severity": "ERROR",
                    "message": str(exc),
                }
            ],
            "error_message": f"Audit Error: {exc}",
        }


def should_fix(state: AgentState) -> Literal["fix", "generate_tests", "__end__"]:
    findings = state.get("findings", [])

    if findings:
        current_attempts = state.get("attempts", 1)
        max_attempts = state.get("max_attempts", 3)
        if current_attempts >= max_attempts:
            return "__end__"
        return "fix"

    return "generate_tests"


async def code_fixer_node(state: AgentState) -> dict:
    prompt: str = f"""You are a Senior Security Engineer and Python developer. 
Your previous code partially solves the task, but it contains CRITICAL SECURITY VULNERABILITIES detected by the Semgrep static analyzer.

--- ORIGINAL TASK ---
{state['task']}

--- VULNERABLE CODE ---
{state['code']}

--- SEMGREP SCAN REPORT ---
{state['findings']}

--- FIX REQUIREMENTS ---
1. Fix ALL reported security vulnerabilities:
   - Replace dangerous calls (eval, exec, os.system, shell=True) with safe alternatives (ast.literal_eval, safe parsers, standard secure libraries).
   - Remove any hardcoded tokens, secrets, or passwords.
   - Avoid direct execution of operating system commands.

2. PRESERVE FUNCTIONALITY AND LOGIC:
   - DO NOT change function names, argument types, or return value formats (the code will be sent directly to unit tests).
   - The solution must still correctly perform the original task.

3. OUTPUT RULES:
   - Return ONLY clean executable Python code.
   - Do not use markdown blocks (no ```python ... ```).
   - Do not include any explanations, fix comments, or greetings.
   - Make sure to keep or add all necessary imports at the very beginning of the file.
"""
    try:
        res = await llm.ainvoke(prompt)
        return {
            "raw_response": str(res.content),
            "attempts": state.get("attempts", 1) + 1,
        }
    except Exception as exc:
        return {
            "raw_response": None,
            "error_message": f"Code Fixer Error: {exc}",
            "attempts": state.get("attempts", 1) + 1,
        }



workflow = StateGraph(AgentState)

workflow.add_node("generate_code", generate_code_node)
workflow.add_node("validate", validate_ast_node)
workflow.add_node("generate_tests", generate_tests_node)
workflow.add_node("audit", audit)
workflow.add_node("sandbox", execute_sandbox_node)
workflow.add_node("repair", repair_code_node)
workflow.add_node("fixer", code_fixer_node)



workflow.add_edge(START, "generate_code")
workflow.add_edge("generate_code", "validate")
workflow.add_edge("validate", "audit")
workflow.add_conditional_edges(
    "audit",
    should_fix,
    {
        "fix": "fixer",
        "generate_tests": "generate_tests",
        "__end__": END,
    }
)
workflow.add_edge("fixer", "validate")
workflow.add_edge("generate_tests", "sandbox")
workflow.add_conditional_edges(
    "sandbox",
    should_repair,
    {
        "repair": "repair",
        "__end__": END,
    },
)





workflow.add_edge("repair", "validate")