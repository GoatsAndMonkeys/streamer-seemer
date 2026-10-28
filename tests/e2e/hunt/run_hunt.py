"""Run hunt scenarios against MA dev, N times each, a few at a time.

    MA_REPO=/path/to/music-assistant-server .venv/bin/python tests/e2e/hunt/run_hunt.py \\
        --repeat 3 -j 4 --out /tmp/hunt [scenario ...]

Writes ``<out>/<scenario>.<n>.log`` and ``<out>/results.json`` and prints one line per
scenario: how many runs showed the bug, out of how many valid runs.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
SCRIPT = HERE / "scenarios.py"


def scenario_names() -> list[str]:
    """Names registered with @scenario, read without importing (MA is not importable here)."""
    names = []
    for node in ast.walk(ast.parse(SCRIPT.read_text())):
        if isinstance(node, ast.AsyncFunctionDef):
            for deco in node.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and getattr(deco.func, "id", None) == "scenario"
                    and deco.args
                    and isinstance(deco.args[0], ast.Constant)
                ):
                    names.append(str(deco.args[0].value))
    return names


def run_once(name: str, n: int, out: Path, timeout: float) -> dict[str, Any]:
    ma = os.environ["MA_REPO"]
    python = os.environ.get("MA_PYTHON") or str(Path(ma) / ".venv" / "bin" / "python")
    env = dict(os.environ, PYTHONPATH=f"{ma}{os.pathsep}{REPO}")
    log = out / f"{name}.{n}.log"
    try:
        proc = subprocess.run(
            [python, str(SCRIPT), name],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        stdout = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        stderr = "TIMEOUT"
    log.write_text(stdout + "\n--- stderr ---\n" + stderr[-20000:])
    lines = [ln for ln in stdout.splitlines() if ln.startswith("RESULT ")]
    if not lines:
        return {"scenario": name, "run": n, "error": "no RESULT", "log": str(log)}
    res = json.loads(lines[-1].removeprefix("RESULT "))
    res["run"] = n
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("-j", "--jobs", type=int, default=3)
    ap.add_argument("--out", default=str(REPO / ".hunt-out"))
    ap.add_argument("--timeout", type=float, default=600)
    args = ap.parse_args()
    if not os.environ.get("MA_REPO"):
        print("set MA_REPO", file=sys.stderr)
        return 2
    names = args.names or scenario_names()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jobs = [(name, n) for n in range(1, args.repeat + 1) for name in names]
    with ThreadPoolExecutor(args.jobs) as pool:
        results = list(pool.map(lambda j: run_once(j[0], j[1], out, args.timeout), jobs))
    path = out / "results.json"
    old = json.loads(path.read_text()) if path.exists() else []
    keep = [r for r in old if r.get("scenario") not in names]
    path.write_text(json.dumps(keep + results, indent=1, default=str))
    for name in names:
        runs = [r for r in results if r["scenario"] == name]
        valid = [r for r in runs if r.get("valid") and "error" not in r]
        bug = sum(1 for r in valid if r.get("bug_present"))
        errs = [r.get("error") for r in runs if "error" in r]
        print(f"{name:34s} bug {bug}/{len(valid)} valid of {len(runs)} runs  {errs or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
