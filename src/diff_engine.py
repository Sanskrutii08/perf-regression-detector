#!/usr/bin/env python3
"""
Metrics diff engine for the performance regression detector project.

Runs a baseline command and a candidate command (typically two versions of
the Java aggregator, or the same version with different args), parses their
printed reports, computes percentage deltas, and emits a structured verdict.
This is the deterministic core the AI/RAG layer will sit on top of later -
no LLM involved here, just measurement and thresholds.

Usage:
    python3 diff_engine.py \
        --baseline "java -cp out MultiThreadLogAggregator sample.log 1" \
        --candidate "java MultiThreadLogAggregator sample.log 8" \
        --runs 3 \
        --throughput-threshold 10 \
        --latency-threshold 10 \
        --out result.json

Exits with status 1 if a regression is detected (so CI can fail the build
on it), status 0 otherwise.
"""
import argparse
import json
import re
import shlex
import statistics
import subprocess
import sys
from dataclasses import asdict, dataclass

ELAPSED_RE = re.compile(r"Elapsed time:\s*(\d+)\s*ms")
THROUGHPUT_RE = re.compile(r"Throughput:\s*(\d+)\s*lines/sec")
TOTAL_LINES_RE = re.compile(r'"total_lines":\s*(\d+)')  # from ReportPrinter's METRICS_JSON block


@dataclass
class RunMetrics:
    elapsed_ms: int
    throughput_lps: int
    total_lines: int


def run_once(command: str) -> str:
    """Runs a shell command and returns its captured stdout."""
    result = subprocess.run(
        shlex.split(command),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed (exit {result.returncode}): {command}\n{result.stderr}"
        )
    return result.stdout


def parse_report(output: str) -> RunMetrics:
    """Extracts the metrics the aggregator programs print into their report."""
    elapsed_match = ELAPSED_RE.search(output)
    throughput_match = THROUGHPUT_RE.search(output)
    total_lines_match = TOTAL_LINES_RE.search(output)

    if not (elapsed_match and throughput_match and total_lines_match):
        raise ValueError(
            "Could not find expected metrics in output. "
            "Was the command's report format changed?\n---\n" + output
        )

    return RunMetrics(
        elapsed_ms=int(elapsed_match.group(1)),
        throughput_lps=int(throughput_match.group(1)),
        total_lines=int(total_lines_match.group(1)),
    )


def run_and_measure(command: str, runs: int, warmup: int) -> RunMetrics:
    """
    Runs a command multiple times to smooth out JVM cold-start noise, discards
    the first `warmup` runs, and returns the median of the remaining runs.
    """
    all_metrics = []
    total_iterations = runs + warmup
    for i in range(total_iterations):
        output = run_once(command)
        metrics = parse_report(output)
        label = "warmup" if i < warmup else "measured"
        print(
            f"  [{label}] run {i + 1}/{total_iterations}: "
            f"elapsed={metrics.elapsed_ms}ms throughput={metrics.throughput_lps} lines/sec",
            file=sys.stderr,
        )
        all_metrics.append(metrics)

    measured = all_metrics[warmup:]
    return RunMetrics(
        elapsed_ms=int(statistics.median(m.elapsed_ms for m in measured)),
        throughput_lps=int(statistics.median(m.throughput_lps for m in measured)),
        total_lines=measured[-1].total_lines,
    )


def pct_change(baseline: float, candidate: float) -> float:
    """Positive = candidate is higher than baseline."""
    if baseline == 0:
        return 0.0
    return round(100.0 * (candidate - baseline) / baseline, 2)


def compute_verdict(
    throughput_pct: float,
    elapsed_pct: float,
    throughput_threshold: float,
    latency_threshold: float,
) -> str:
    """
    throughput_pct: negative = candidate has lower throughput -> bad
    elapsed_pct: positive = candidate took longer -> bad
    """
    is_throughput_regression = throughput_pct <= -throughput_threshold
    is_latency_regression = elapsed_pct >= latency_threshold

    if is_throughput_regression or is_latency_regression:
        return "REGRESSION"

    is_throughput_improvement = throughput_pct >= throughput_threshold
    is_latency_improvement = elapsed_pct <= -latency_threshold

    if is_throughput_improvement or is_latency_improvement:
        return "IMPROVEMENT"

    return "PASS"


def main():
    parser = argparse.ArgumentParser(
        description="Compare a baseline and candidate run, and flag performance regressions."
    )
    parser.add_argument(
        "--baseline", required=True,
        help="Shell command for the baseline run, e.g. 'java -cp out MultiThreadLogAggregator sample.log 1'",
    )
    parser.add_argument(
        "--candidate", required=True,
        help="Shell command for the candidate run, e.g. 'java LogAggregatorV2 sample.log 8'",
    )
    parser.add_argument(
        "--runs", type=int, default=3,
        help="Number of measured runs to take the median of (default: 3)",
    )
    parser.add_argument(
        "--warmup", type=int, default=1,
        help="Number of warmup runs to discard before measuring, to absorb JVM cold-start noise (default: 1)",
    )
    parser.add_argument(
        "--throughput-threshold", type=float, default=10.0,
        help="Percent drop in throughput that counts as a regression (default: 10)",
    )
    parser.add_argument(
        "--latency-threshold", type=float, default=10.0,
        help="Percent increase in elapsed time that counts as a regression (default: 10)",
    )
    parser.add_argument(
        "--out", default=None,
        help="Optional path to also write the result JSON to a file",
    )
    args = parser.parse_args()

    print(f"Running baseline: {args.baseline}", file=sys.stderr)
    baseline = run_and_measure(args.baseline, args.runs, args.warmup)

    print(f"Running candidate: {args.candidate}", file=sys.stderr)
    candidate = run_and_measure(args.candidate, args.runs, args.warmup)

    throughput_pct = pct_change(baseline.throughput_lps, candidate.throughput_lps)
    elapsed_pct = pct_change(baseline.elapsed_ms, candidate.elapsed_ms)

    verdict = compute_verdict(
        throughput_pct, elapsed_pct,
        args.throughput_threshold, args.latency_threshold,
    )

    result = {
        "baseline_command": args.baseline,
        "candidate_command": args.candidate,
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

    print(json.dumps(result, indent=2))

    if args.out:
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nWrote result to {args.out}", file=sys.stderr)

    if verdict == "REGRESSION":
        sys.exit(1)  # non-zero exit lets CI fail the build on a real regression


if __name__ == "__main__":
    main()