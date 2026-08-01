#!/usr/bin/env python3
"""
branch_metrics_runner.py

Checks out the main (baseline) branch first, builds and runs the Java
aggregator to capture its JVM metrics, then checks out the feature branch
and does the same. Outputs both metric sets as JSON so they can be passed
to the LLM alongside a code diff and retrieved incidents (handled by
other scripts in the pipeline).

Usage:
    python branch_metrics_runner.py --main main --feature feature-branch \
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
JAVA_SRC_DIR = REPO_DIR / "src"
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


def compute_diff(baseline: str, feature: str, scope_dir: Optional[str] = None) -> str:
    """
    Returns the code diff between baseline and feature.

    If scope_dir is given, the diff is limited to files under that
    directory (relative to the repo root) - useful for excluding test
    files, generated code, or unrelated modules from what gets passed
    to the LLM. If scope_dir is None, returns the full diff across
    every changed file between the two refs.
    """
    args = ["diff", f"{baseline}..{feature}"]
    if scope_dir:
        args += ["--", scope_dir]
    result = run_git(*args)
    return result.stdout


# ---------------------------------------------------------------------------
# Build + run
# ---------------------------------------------------------------------------
def compile_java(classpath: Optional[str] = None) -> None:
    out_dir = Path(classpath) if classpath else BUILD_DIR
    print(f"[build] compiling Java sources into {out_dir} ...", file=sys.stderr)
    java_files = [str(p) for p in JAVA_SRC_DIR.rglob("*.java")]
    if not java_files:
        raise RuntimeError(f"No .java files found under {JAVA_SRC_DIR}")

    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["javac", "-d", str(out_dir), *java_files],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"javac failed:\n{result.stderr}")


def run_aggregator(main_class: str, log_path: Path, num_threads: Optional[int],
                    classpath: Optional[str] = None) -> str:
    cp = classpath if classpath else str(BUILD_DIR)
    cmd = ["java", "-cp", cp, main_class, str(log_path)]
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


def run_for_ref(ref: str, main_class: str, log_path: Path, num_threads: Optional[int],
                 classpath: Optional[str] = None) -> RunResult:
    checkout(ref)
    compile_java(classpath=classpath)
    stdout = run_aggregator(main_class, log_path, num_threads, classpath=classpath)
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
    parser.add_argument("--log", default="sample.log", help="Path to the log file to process")
    parser.add_argument("--threads", type=int, default=None, help="Thread count arg to pass through")
    parser.add_argument("--classpath", default=None, help="Java classpath to run with (default: the compiled BUILD_DIR)")
    parser.add_argument("--diff-dir", default=None, help="Scope the code diff to this directory (relative to repo root). If omitted, diffs all files.")
    parser.add_argument("--out", default=None, help="Optional path to also write JSON output to a file")
    args = parser.parse_args()

    if not current_ref_is_clean():
        print("[error] working tree has uncommitted changes - commit or stash before running", file=sys.stderr)
        sys.exit(1)

    original_ref = current_branch()
    log_path = (REPO_DIR / args.log).resolve()

    try:
        main_result = run_for_ref(args.main, args.main_class, log_path, args.threads, classpath=args.classpath)
        feature_result = run_for_ref(args.feature, args.main_class, log_path, args.threads, classpath=args.classpath)
    finally:
        # always return to whatever branch you started on, even on failure
        checkout(original_ref)

    output = {
        "main": asdict(main_result),
        "feature": asdict(feature_result),
        "diff": compute_diff(args.main, args.feature, scope_dir=args.diff_dir),
    }
    output_json = json.dumps(output, indent=2)

    print(output_json)  # stdout - this is what a downstream script/LLM pipe consumes

    if args.out:
        Path(args.out).write_text(output_json)
        print(f"[info] also wrote output to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()