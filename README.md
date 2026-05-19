# Autonomous AI Agent

A tool-using reasoning agent. Give it a task and it runs a **plan → act →
observe** loop — reasoning about the problem, calling a tool, reading the
result, and self-correcting — until it produces a final answer. Every step is
streamed to the UI so the agent's full reasoning trace is visible.

## How it works

1. The LLM receives the task and the available tool schemas.
2. It emits a short reasoning step and a tool call.
3. The tool runs; its output is fed back as an observation.
4. The loop repeats (up to a step budget). When the LLM responds without a
   tool call, that response is the final answer.

If a tool returns an error, the agent reads it and corrects its approach — for
example, rewriting code that printed nothing or threw an exception.

## Tools

- **calculator** — a safe arithmetic evaluator (AST-parsed: operators, math
  functions, constants — no names or calls outside a whitelist).
- **run_python** — a restricted Python sandbox (`sandbox.py`). Defence in depth:
  AST validation rejects non-whitelisted imports, dunder access, and
  exec/eval/open before any code runs; execution uses restricted builtins; the
  caller runs it as a subprocess with a wall-clock timeout and CPU/memory caps.
  No file or network access.

## Stack

- Backend: FastAPI (port 19001)
- LLM: OpenAI chat completions with tool calling
- Frontend: Next.js 15 (port 19000)

## Setup

Create `.env` with an OpenAI key:

```
OPENAI_API_KEY=sk-...
AGENT_MODEL=gpt-4o-mini
```

## Run

```bash
python3 api.py
cd web && npm install && npm run build && npm start
```

Open http://localhost:19000

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Service health, model, key status |
| GET | `/api/examples` | Example tasks |
| POST | `/api/run` | Start an agent run `{task}` → `{run_id}` |
| GET | `/api/run/{id}` | Poll run status, step trace, and answer |
