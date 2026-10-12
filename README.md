# Arbitra

[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?style=flat-square)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?style=flat-square)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/orchestration-LangGraph-gray?style=flat-square)](https://github.com/langchain-ai/langgraph)
[![Langfuse](https://img.shields.io/badge/observability-Langfuse-orange?style=flat-square)](https://langfuse.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?style=flat-square)](https://postgresql.org)
[![Redis](https://img.shields.io/badge/queue-Taskiq%20%2F%20Redis-DC382D?style=flat-square)](https://redis.io)
[![Docker](https://img.shields.io/badge/sandbox-Docker-2496ED?style=flat-square)](https://docker.com)
[![Semgrep](https://img.shields.io/badge/security-Semgrep-2B2D42?style=flat-square)](https://semgrep.dev)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](https://opensource.org/licenses/MIT)

Arbitra is an asynchronous distributed execution engine for secure, automated Python code synthesis. The system integrates pre-execution static application security testing (SAST), contract interface extraction, container-isolated sandboxing, traceback-driven self-healing loops, stateful human-in-the-loop (HITL) review, and comprehensive LLM telemetry with cost attribution.

---

## System Architecture

Arbitra separates ingestion, state machine orchestration, telemetry, and untrusted execution into decoupled services:

```mermaid
flowchart TD
    Client[Telegram Bot / REST API Client] -->|HTTP 202| Gateway[FastAPI Gateway]
    Gateway -->|Enqueue Task| Redis[(Redis Broker)]
    Gateway -->|Persist Task & Checkpoints| PG[(PostgreSQL)]

    Worker[Taskiq Background Worker] -->|Fetch Job| Redis
    Worker -->|Execute Pipeline| LG[LangGraph Engine]

    subgraph Agent Loop
        Gen[1. Code Generation] --> AST[2. AST Syntax Check]
        AST --> Audit[3. Semgrep SAST Audit]
        Audit -->|Vulnerabilities Detected| Fixer[Security Code Fixer Node]
        Fixer --> AST
        Audit -->|Clean AST| Contract[4. Blackbox Interface Extractor & QA Testgen]
        Contract -->|Pause before Sandbox| HITL{Human Approval}
        HITL -->|Approved| Box[5. Air-Gapped Docker Sandbox]
        Box -->|Assertion Error & Attempts < 3| Repair[6. Self-Healing Repair Loop]
        Repair --> AST
        Box -->|Tests Passed| EndNode[Target Completed]
    end

    LG --> Worker
    Worker -->|Sync State & Runs| PG
    LG -.->|Traces, Tokens & Cost Attribution| LF[(Langfuse Telemetry)]
