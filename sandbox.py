"""
Restricted Python sandbox — the agent's code-interpreter tool.

Defence in depth:
  1. AST validation — rejects imports outside a whitelist, dunder access, and
     reflection/exec primitives before any code runs.
  2. Restricted builtins — only a curated safe set is exposed to the code.
  3. Process isolation — the caller runs this as a subprocess with a wall-clock
     timeout; run as __main__ it also caps CPU time and address space.

Run as a module:  echo "<code>" | python3 sandbox.py   ->  JSON on stdout.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys

ALLOWED_MODULES = {
    "math", "random", "statistics", "datetime", "itertools",
    "json", "re", "collections", "fractions", "decimal",
}

SAFE_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "divmod", "enumerate", "filter",
    "float", "format", "frozenset", "int", "len", "list", "map", "max", "min",
    "pow", "print", "range", "reversed", "round", "set", "slice", "sorted",
    "str", "sum", "tuple", "zip", "bin", "hex", "oct", "chr", "ord",
    "isinstance", "issubclass", "repr", "complex", "bytes",
}

BANNED_NAMES = {
    "exec", "eval", "compile", "open", "__import__", "globals", "locals",
    "vars", "input", "breakpoint", "memoryview", "getattr", "setattr",
    "delattr", "help", "exit", "quit", "object", "super",
}


class _Validator(ast.NodeVisitor):
    def __init__(self):
        self.errors: list[str] = []

    def visit_Import(self, node):
        for n in node.names:
            if n.name.split(".")[0] not in ALLOWED_MODULES:
                self.errors.append(f"import of '{n.name}' is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        root = (node.module or "").split(".")[0]
        if root not in ALLOWED_MODULES:
            self.errors.append(f"import from '{node.module}' is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.attr, str) and node.attr.startswith("_"):
            self.errors.append(f"access to attribute '{node.attr}' is not allowed")
        self.generic_visit(node)

    def visit_Name(self, node):
        if node.id in BANNED_NAMES:
            self.errors.append(f"use of '{node.id}' is not allowed")
        self.generic_visit(node)


def validate(code: str) -> list[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error: {e.msg} (line {e.lineno})"]
    v = _Validator()
    v.visit(tree)
    return v.errors


def _safe_import(name, *args, **kwargs):
    if name.split(".")[0] in ALLOWED_MODULES:
        return __import__(name, *args, **kwargs)
    raise ImportError(f"import of '{name}' is blocked by the sandbox")


def run(code: str) -> dict:
    errors = validate(code)
    if errors:
        return {"ok": False, "stdout": "", "error": "; ".join(errors)}

    import builtins as _b
    safe = {k: getattr(_b, k) for k in SAFE_BUILTINS if hasattr(_b, k)}
    safe["__import__"] = _safe_import
    env: dict = {"__builtins__": safe}
    for mod in ALLOWED_MODULES:
        env[mod] = __import__(mod)

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            exec(compile(code, "<agent-sandbox>", "exec"), env)
    except Exception as e:                       # noqa: BLE001
        return {"ok": False, "stdout": buf.getvalue(),
                "error": f"{type(e).__name__}: {e}"}
    out = buf.getvalue()
    if len(out) > 6000:
        out = out[:6000] + "\n… (output truncated)"
    return {"ok": True, "stdout": out, "error": ""}


if __name__ == "__main__":
    # subprocess mode — cap CPU and memory, then run stdin
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
    except Exception:
        pass
    src = sys.stdin.read()
    print(json.dumps(run(src)))
