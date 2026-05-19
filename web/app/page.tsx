"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface Step {
  kind: "thought" | "action" | "observation" | "answer" | "error";
  text?: string;
  tool?: string;
  args?: Record<string, unknown>;
}

interface RunState {
  run_id: string;
  status: "running" | "done" | "error";
  task: string;
  steps: Step[];
  answer: string;
}

// ── Step rendering ────────────────────────────────────────────────────────────

function StepView({ step }: { step: Step }) {
  if (step.kind === "answer") {
    return (
      <div className="trace-line">
        <span className="trace-dot dot-answer" />
        <div className="answer-card">
          <div className="trace-kind">Final Answer</div>
          <div className="answer-text">{step.text}</div>
        </div>
      </div>
    );
  }
  if (step.kind === "error") {
    return (
      <div className="trace-line">
        <span className="trace-dot dot-error" />
        <div className="error-card">Agent error — {step.text}</div>
      </div>
    );
  }
  if (step.kind === "thought") {
    return (
      <div className="trace-line">
        <span className="trace-dot dot-thought" />
        <div className="trace-kind">Reasoning</div>
        <div className="trace-text">{step.text}</div>
      </div>
    );
  }
  if (step.kind === "action") {
    const code =
      (step.args?.code as string) ?? (step.args?.expression as string) ?? "";
    return (
      <div className="trace-line">
        <span className="trace-dot dot-action" />
        <div className="trace-kind">
          Tool call → <span className="tool-name">{step.tool}</span>
        </div>
        <div className="code-block">{code}</div>
      </div>
    );
  }
  // observation
  return (
    <div className="trace-line">
      <span className="trace-dot dot-observation" />
      <div className="trace-kind">
        Observation ← <span className="tool-name">{step.tool}</span>
      </div>
      <div className="obs-block">{step.text}</div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

const FALLBACK_EXAMPLES = [
  "Find every prime number below 10,000 and report the count and sum.",
];

export default function HomePage() {
  const [task, setTask] = useState("");
  const [examples, setExamples] = useState<string[]>(FALLBACK_EXAMPLES);
  const [run, setRun] = useState<RunState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const poll = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    fetch("/api/examples")
      .then((r) => r.json())
      .then((d: string[]) => d.length && setExamples(d))
      .catch(() => {});
    return () => {
      if (poll.current) clearInterval(poll.current);
    };
  }, []);

  const launch = useCallback(async (t: string) => {
    const text = t.trim();
    if (!text) return;
    setBusy(true);
    setError(null);
    setRun(null);
    try {
      const res = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: text }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const { run_id } = await res.json();

      poll.current = setInterval(async () => {
        try {
          const r = await fetch(`/api/run/${run_id}`);
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const state: RunState = await r.json();
          setRun(state);
          if (state.status !== "running") {
            if (poll.current) clearInterval(poll.current);
            setBusy(false);
          }
        } catch (e) {
          if (poll.current) clearInterval(poll.current);
          setError(e instanceof Error ? e.message : "Polling failed");
          setBusy(false);
        }
      }, 650);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start run");
      setBusy(false);
    }
  }, []);

  return (
    <>
      <div className="page-header">
        <span className="eyebrow">Agentic AI · Tool Use · Reasoning</span>
        <h1 className="page-title">Autonomous AI Agent</h1>
        <p className="page-sub">
          Give the agent a task. It plans, calls tools, reads the results, and
          self-corrects until it solves the problem — every step shown live.
        </p>
      </div>

      <div className="task-panel">
        <textarea
          value={task}
          onChange={(e) => setTask(e.target.value)}
          placeholder="Describe a task for the agent to solve…"
        />
        <div className="task-actions">
          <div className="chips">
            <span className="chips-label">Try:</span>
            {examples.map((ex) => (
              <button
                key={ex}
                className="chip"
                onClick={() => {
                  setTask(ex);
                  launch(ex);
                }}
              >
                {ex.length > 52 ? ex.slice(0, 52) + "…" : ex}
              </button>
            ))}
          </div>
          <button
            className="run-btn"
            onClick={() => launch(task)}
            disabled={busy || !task.trim()}
          >
            {busy ? "Running…" : "Run Agent"}
          </button>
        </div>
        <div className="tools-note">
          Tools available to the agent: <strong>calculator</strong> (safe
          arithmetic) and <strong>run_python</strong> (a restricted Python
          sandbox — AST-validated, no file or network access).
        </div>
      </div>

      {error && <div className="error-card">{error}</div>}

      {run && (
        <>
          {run.status === "running" && (
            <div className="run-status">
              <span className="spinner" /> Agent working — step{" "}
              {run.steps.length + 1}…
            </div>
          )}
          <div className="trace">
            {run.steps.map((s, i) => (
              <StepView key={i} step={s} />
            ))}
          </div>
        </>
      )}

      {!run && !error && (
        <div className="empty-hint">
          Pick an example or write your own task. The agent runs a plan → act →
          observe loop and shows its full reasoning trace.
        </div>
      )}

      <div className="footer">
        <span>Autonomous AI Agent · plan · act · observe</span>
        <span>FastAPI · OpenAI · Next.js · djkimlab.com</span>
      </div>
    </>
  );
}
