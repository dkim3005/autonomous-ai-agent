"""
Autonomous AI Agent — a tool-using reasoning loop.
FastAPI on port 19001.

Given a task, the agent runs a plan -> act -> observe loop: the LLM reasons,
selects a tool, the tool runs in a sandbox, the observation is fed back, and
the loop repeats until the agent produces a final answer. Every step is
recorded so the frontend can replay the agent's reasoning live.

Tools:
  calculator  — safe arithmetic expression evaluator
  run_python  — restricted Python sandbox (see sandbox.py)
"""

from __future__ import annotations

import ast
import json
import math
import operator
import os
import subprocess
import sys
import threading
import uuid

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BASE = os.path.dirname(__file__)
SANDBOX_PATH = os.path.join(BASE, "sandbox.py")
MAX_STEPS = 8


def _load_env():
    path = os.path.join(BASE, ".env")
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


_load_env()
MODEL = os.environ.get("AGENT_MODEL", "gpt-4o-mini")

app = FastAPI(title="Autonomous AI Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Tool: calculator ──────────────────────────────────────────────────────────

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}
_FUNCS = {n: getattr(math, n) for n in (
    "sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "log",
    "log2", "log10", "exp", "factorial", "floor", "ceil", "gcd", "hypot",
    "degrees", "radians", "fabs", "comb", "perm")}
_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max, "pow": pow})


def _calc(node):
    if isinstance(node, ast.Expression):
        return _calc(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("only numeric literals allowed")
    if isinstance(node, ast.BinOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise ValueError("operator not allowed")
        if isinstance(node.op, ast.Pow):
            right = _calc(node.right)
            if abs(right) > 10000:
                raise ValueError("exponent too large")
            return op(_calc(node.left), right)
        return op(_calc(node.left), _calc(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise ValueError("operator not allowed")
        return op(_calc(node.operand))
    if isinstance(node, ast.Name):
        if node.id in _CONSTS:
            return _CONSTS[node.id]
        raise ValueError(f"unknown name '{node.id}'")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ValueError("function not allowed")
        return _FUNCS[node.func.id](*[_calc(a) for a in node.args])
    raise ValueError("expression form not allowed")


def tool_calculator(expression: str) -> str:
    expr = (expression or "").strip()
    if not expr:
        return "error: empty expression"
    try:
        return f"{expr} = {_calc(ast.parse(expr, mode='eval'))}"
    except Exception as e:                       # noqa: BLE001
        return f"error: {e}"


# ── Tool: run_python ──────────────────────────────────────────────────────────

def tool_python(code: str) -> str:
    if not (code or "").strip():
        return "error: no code provided"
    try:
        proc = subprocess.run(
            [sys.executable, SANDBOX_PATH],
            input=code, capture_output=True, text=True, timeout=14,
        )
    except subprocess.TimeoutExpired:
        return "error: execution timed out (14s wall-clock limit)"
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return f"error: sandbox failure\n{(proc.stderr or '')[:300]}"
    if not result["ok"]:
        body = result["stdout"]
        return (f"[partial stdout]\n{body}\n[error] {result['error']}"
                if body else f"[error] {result['error']}")
    return result["stdout"] or "(ran successfully but printed nothing — use print())"


TOOLS = [
    {"type": "function", "function": {
        "name": "calculator",
        "description": "Evaluate one arithmetic expression. Supports + - * / // "
                       "% **, parentheses, math functions (sqrt, log, sin, "
                       "factorial, comb, …) and constants pi, e, tau.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string"}}, "required": ["expression"]},
    }},
    {"type": "function", "function": {
        "name": "run_python",
        "description": "Run Python in a restricted sandbox and return stdout. "
                       "Use for any multi-step computation, simulation, data "
                       "work, or string processing. Modules available: math, "
                       "random, statistics, datetime, itertools, json, re, "
                       "collections, fractions, decimal. No file or network "
                       "access. Always print() the results you need.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string"}}, "required": ["code"]},
    }},
]

TOOL_FNS = {"calculator": tool_calculator, "run_python": tool_python}

SYSTEM_PROMPT = (
    "You are an autonomous problem-solving agent. Work toward the user's task "
    "step by step.\n"
    "- Before each tool call, state your reasoning in one or two short sentences.\n"
    "- Use run_python for any non-trivial computation, simulation, data "
    "processing, or logic — never guess a result you could compute.\n"
    "- Use calculator for quick standalone arithmetic.\n"
    "- If a tool returns an error, read it carefully and correct your approach.\n"
    "- When the task is fully solved, reply directly with a clear, well-"
    "organised final answer and do NOT call a tool.\n"
    "Be concise and rigorous."
)

# ── Agent loop ────────────────────────────────────────────────────────────────

RUNS: dict[str, dict] = {}


def _call_llm(messages: list[dict]) -> dict:
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": MODEL, "messages": messages, "tools": TOOLS,
              "temperature": 0.2},
        timeout=90,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"LLM API error {resp.status_code}: {resp.text[:200]}")
    return resp.json()["choices"][0]["message"]


def _agent_loop(run_id: str, task: str):
    run = RUNS[run_id]
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    try:
        for _ in range(MAX_STEPS):
            msg = _call_llm(messages)
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")

            if not tool_calls:
                run["steps"].append({"kind": "answer",
                                     "text": content or "(no answer produced)"})
                run["answer"] = content or ""
                run["status"] = "done"
                return

            if content:
                run["steps"].append({"kind": "thought", "text": content})
            messages.append(msg)

            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                run["steps"].append({"kind": "action", "tool": name, "args": args})
                fn = TOOL_FNS.get(name)
                observation = fn(**args) if fn else f"unknown tool: {name}"
                run["steps"].append({"kind": "observation", "tool": name,
                                     "text": observation})
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                  "content": observation})

        run["steps"].append({"kind": "answer",
                              "text": "Reached the step limit before completing."})
        run["answer"] = "Reached the step limit."
        run["status"] = "done"
    except Exception as e:                       # noqa: BLE001
        run["steps"].append({"kind": "error", "text": str(e)})
        run["status"] = "error"


# ── Request models ────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    task: str


EXAMPLES = [
    "Find every prime number below 10,000 and report how many there are and their sum.",
    "Simulate rolling two six-sided dice 200,000 times. Report the probability of each total from 2 to 12.",
    "If I invest $10,000 at 6.5% annual interest compounded monthly, what is it worth after 25 years?",
    "Sort these by length, longest first, and give the total letter count: autonomous, agent, orchestrates, tools.",
]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "service": "autonomous-ai-agent",
        "model": MODEL,
        "key_configured": bool(os.environ.get("OPENAI_API_KEY")),
    }


@app.get("/api/examples")
def examples():
    return EXAMPLES


@app.post("/api/run")
def start_run(req: RunRequest):
    if not req.task.strip():
        raise HTTPException(status_code=400, detail="Task must not be empty")
    run_id = uuid.uuid4().hex[:12]
    RUNS[run_id] = {"status": "running", "task": req.task,
                    "steps": [], "answer": ""}
    threading.Thread(target=_agent_loop, args=(run_id, req.task),
                     daemon=True).start()
    return {"run_id": run_id}


@app.get("/api/run/{run_id}")
def get_run(run_id: str):
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"run_id": run_id, **run}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=19001, reload=False)
