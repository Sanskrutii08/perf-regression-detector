#!/usr/bin/env python3
"""
Checks out two git refs (tags/commits/branches) of the WHOLE project, one at
a time, builds each, and measures each with diff_engine's run/parse logic -
producing the same kind of metrics comparison diff_engine.py does, but
without needing two separately pre-built versions sitting on disk at once.

This script does NOT capture the code diff (that's code_diff.py, kept
separate) - it only handles the git checkout + build + measure loop.

Usage:
    python run_regression_check.py \
        --good-ref v2-good \
        --regression-ref v2-regression \
        --build-cmd "javac -d out src/*.java" \
        --run-cmd "java -cp out MultiThreadLogAggregator big.log" \
        --runs 3 --warmup 1 \
        --out result.json

Notes:
- Each ref is checked out in FULL (like `git checkout <ref>`, no path
  scoping) - the whole working tree is swapped to that ref's state before
  building and measuring.
- Requires a clean working tree (no uncommitted changes) before running,
  since `git checkout` across the whole project will refuse to overwrite
  uncommitted local changes.
- --run-cmd is the SAME command for both versions - what changes between
  runs is the project state on disk, swapped in by git checkout before
  each build.
- The original branch/ref you were on is restored automatically when the
  script finishes (or if it fails partway through).
"""
import argparse
import json
import shlex
import subprocess
import sys
from dataclasses import asdict

import diff_engine


def get_current_ref() -> str:
    """Returns the current branch name, or the commit hash if in detached HEAD state."""
    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    if branch.returncode == 0:
        return branch.stdout.strip()
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return commit.stdout.strip()


def ensure_clean_working_tree() -> None:
    result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True, check=True)
    if result.stdout.strip():
        raise RuntimeError(
            "Working tree has uncommitted changes. Commit, stash, or discard them before running "
            "this script - a full `git checkout` between refs needs a clean tree.\n\n"
            + result.stdout
        )


def git_checkout(ref: str) -> None:
    result = subprocess.run(["git", "checkout", ref], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git checkout {ref} failed: {result.stderr}")


def run_build(build_cmd: str) -> None:
    result = subprocess.run(shlex.split(build_cmd), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"Build command failed: {build_cmd}\n{result.stderr}")


def restore_original_ref(original_ref: str) -> None:
    """Best-effort restore of the branch/commit you started on, even if something above failed."""
    try:
        git_checkout(original_ref)
        print(f"Restored to {original_ref}", file=sys.stderr)
    except RuntimeError as e:
        print(f"WARNING: failed to restore original ref {original_ref}: {e}", file=sys.stderr)


def measure_ref(ref: str, build_cmd: str, run_cmd: str, runs: int, warmup: int) -> diff_engine.RunMetrics:
    print(f"\n=== Checking out {ref} (whole project) ===", file=sys.stderr)
    git_checkout(ref)
    run_build(build_cmd)
    print(f"Measuring {ref}...", file=sys.stderr)
    return diff_engine.run_and_measure(run_cmd, runs, warmup)


def main():
    parser = argparse.ArgumentParser(description="Checkout, build, and measure two git refs of the whole project, one by one.")
    parser.add_argument("--good-ref", required=True, help="Git ref for the baseline/good version")
    parser.add_argument("--regression-ref", required=True, help="Git ref for the candidate/regression version")
    parser.add_argument("--build-cmd", required=True, help="Shell command to build after each checkout, e.g. 'javac -d out src/*.java'")
    parser.add_argument("--run-cmd", required=True, help="Shell command to run and measure, same for both versions")
    parser.add_argument("--runs", type=int, default=3, help="Measured runs per version (default: 3)")
    parser.add_argument("--warmup", type=int, default=1, help="Warmup runs to discard per version (default: 1)")
    parser.add_argument("--throughput-threshold", type=float, default=10.0)
    parser.add_argument("--latency-threshold", type=float, default=10.0)
    parser.add_argument("--out", default=None, help="Optional path to write the result JSON to")
    args = parser.parse_args()

    ensure_clean_working_tree()
    original_ref = get_current_ref()
    print(f"Current ref before starting: {original_ref}", file=sys.stderr)

    try:
        baseline = measure_ref(args.good_ref, args.build_cmd, args.run_cmd, args.runs, args.warmup)
        candidate = measure_ref(args.regression_ref, args.build_cmd, args.run_cmd, args.runs, args.warmup)
    finally:
        # Always restore, even if a checkout/build/measure step above raised
        restore_original_ref(original_ref)

    throughput_pct = diff_engine.pct_change(baseline.throughput_lps, candidate.throughput_lps)
    elapsed_pct = diff_engine.pct_change(baseline.elapsed_ms, candidate.elapsed_ms)
    verdict = diff_engine.compute_verdict(
        throughput_pct, elapsed_pct,
        args.throughput_threshold, args.latency_threshold,
    )

    result = {
        "good_ref": args.good_ref,
        "regression_ref": args.regression_ref,
        "run_cmd": args.run_cmd,
        "baseline": asdict(baseline),
        "candidate": asdict(candidate),
        "deltas": {
            "throughput_pct": throughput_pct,
            "elapsed_ms_pct": elapsed_pct,
        },
        "thresholds": {
            "throughput_pct": args.throughput_threshold,
            "latency_pct": args.latency_threshold,
        },
        "verdict": verdict,
    }

    print("\n" + json.dumps(result, indent=2))

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nWrote result to {args.out}", file=sys.stderr)

    if verdict == "REGRESSION":
        sys.exit(1)


if __name__ == "__main__":
    main()