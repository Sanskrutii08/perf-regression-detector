#!/usr/bin/env python3
"""
RunRegressionCheck.py

Checks out the main (baseline) branch first, builds and runs the Java
aggregator to capture its JVM metrics, then checks out the feature branch
and does the same. Outputs both metric sets as JSON so they can be passed
to the LLM alongside a code diff and retrieved incidents (handled by
other scripts in the pipeline).

Usage:
    python RunRegressionCheck.py --main main --feature feature-branch \
        --main-class LogAggregatorV2InMemory --log sample.log --threads 8

Output:
    Prints a JSON object to stdout:
    {
      "main":    { "ref": "main", "metrics": {...} },
      "feature": { "ref": "feature-branch", "metrics": {...} }
    }

Assumptions (tell me if wrong):
  - Both branches run the SAME entry point class (same command, different
    checked-out code).
  - ReportPrinter's "=== METRICS_JSON ===" marker is followed by ONLY the
    JSON block, nothing else.
  - Repo root is this script's directory; .java sources live under "src".
    Adjust JAVA_SRC_DIR below if not.
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Config - adjust to match your repo layout
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parent
JAVA_SRC_DIR = REPO_DIR
BUILD_DIR = REPO_DIR / "out"
METRICS_MARKER = "=== METRICS_JSON ==="


@dataclass
class RunResult:
    ref: str
    metrics: dict


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
def run_git(*args, check=True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=REPO_DIR, capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr}")
    return result


def checkout(ref: str) -> None:
    print(f"[git] checking out {ref}", file=sys.stderr)
    run_git("checkout", ref)


def current_ref_is_clean() -> bool:
    return run_git("status", "--porcelain").stdout.strip() == ""


def current_branch() -> str:
    return run_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# Build + run
# ---------------------------------------------------------------------------
def compile_java() -> None:
    print("[build] compiling Java sources...", file=sys.stderr)
    java_files = [str(p) for p in JAVA_SRC_DIR.rglob("*.java")]
    if not java_files:
        raise RuntimeError(f"No .java files found under {JAVA_SRC_DIR}")

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["javac", "-d", str(BUILD_DIR), *java_files],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"javac failed:\n{result.stderr}")


def run_aggregator(main_class: str, log_path: Path, num_threads: Optional[int]) -> str:
    cmd = f'java -cp "{args.class_path}" {args.main_class} {args.log} {args.threads}'
    if num_threads is not None:
        cmd.append(str(num_threads))

    print(f"[run] {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Java run failed (exit {result.returncode}):\n{result.stderr}")
    return result.stdout


def parse_metrics(stdout: str) -> dict:
    if METRICS_MARKER not in stdout:
        raise RuntimeError(
            "METRICS_JSON marker not found in program output - "
            "did the run actually complete?"
        )
    json_part = stdout.split(METRICS_MARKER, 1)[1].strip()
    try:
        return json.loads(json_part)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Could not parse METRICS_JSON block: {e}\nRaw:\n{json_part}")


def run_for_ref(ref: str, main_class: str, log_path: Path, num_threads: Optional[int]) -> RunResult:
    checkout(ref)
    compile_java()
    stdout = run_aggregator(main_class, log_path, num_threads)
    metrics = parse_metrics(stdout)
    return RunResult(ref=ref, metrics=metrics)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--main", default="main", help="Baseline branch/tag to run first (default: main)")
    parser.add_argument("--feature", required=True, help="Feature branch/tag to run second")
    parser.add_argument("--main-class", required=True, help="Entry point class, e.g. LogAggregatorV2InMemory")
    parser.add_argument("--class-path", required=True, help="Class path")
    parser.add_argument("--log", default="sample.log", help="Path to the log file to process")
    parser.add_argument("--threads", type=int, default=None, help="Thread count arg to pass through")
    parser.add_argument("--out", default=None, help="Optional path to also write JSON output to a file")
    args = parser.parse_args()

    if not current_ref_is_clean():
        print("[error] working tree has uncommitted changes - commit or stash before running", file=sys.stderr)
        sys.exit(1)

    original_ref = current_branch()
    log_path = (REPO_DIR / args.log).resolve()

    try:
        main_result = run_for_ref(args.main, args.main_class, log_path, args.threads)
        feature_result = run_for_ref(args.feature, args.main_class, log_path, args.threads)
    finally:
        # always return to whatever branch you started on, even on failure
        checkout(original_ref)

    output = {
        "main": asdict(main_result),
        "feature": asdict(feature_result),
    }
    output_json = json.dumps(output, indent=2)

    print(output_json)  # stdout - this is what a downstream script/LLM pipe consumes

    if args.out:
        Path(args.out).write_text(output_json)
        print(f"[info] also wrote output to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()