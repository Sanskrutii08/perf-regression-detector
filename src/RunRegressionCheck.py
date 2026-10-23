#!/usr/bin/env python3
"""
RunRegressionCheck.py

Checks out the main (baseline) branch first, builds and runs the Java
aggregator to capture its JVM metrics, then checks out the feature branch
and does the same. Outputs both metric sets, a raw code diff, a
regression signature vector, and the top-k most similar past incidents
retrieved from the Chroma incident store as JSON, ready to hand to the
LLM.

Usage:
    python RunRegressionCheck.py --main main --feature feature-branch \
        --main-class MultiThreadLogAggregator --log sample.log --threads 8

Output:
    Prints a JSON object to stdout:
    {
      "main":    { "ref": "main", "metrics": {...} },
      "feature": { "ref": "feature-branch", "metrics": {...} },
      "diff": "<raw unified diff text>",
      "vector": [0.99, -0.85, 0.0, ...],
      "vector_schema_version": 1,
      "retrieved_incidents": [
        { "similarity": 0.81, "root_cause": "...", "fix": "...", "tags": [...] },
        ...
      ]
    }

Assumptions (tell me if wrong):
  - Both branches run the SAME entry point class (same command, different
    checked-out code).
  - ReportPrinter's "=== METRICS_JSON ===" marker is followed by ONLY the
    JSON block, nothing else.
  - Repo root is this script's directory; .java sources live under "src".
    Adjust JAVA_SRC_DIR below if not.
  - ChromaStore.py's chroma_data/ directory (built by SeedIncidents.py)
    is present in the same directory as this script.
"""

import argparse
import json
import math
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

from ChromaStore import query_similar

# ---------------------------------------------------------------------------
# Config - adjust to match your repo layout
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parent
JAVA_SRC_DIR = REPO_DIR
BUILD_DIR = REPO_DIR / "out"
METRICS_MARKER = "=== METRICS_JSON ==="

# ---------------------------------------------------------------------------
# Regression signature vector schema (versioned - bump if this changes)
# ---------------------------------------------------------------------------
# Order matters - this IS the vector layout. Do not reorder without
# bumping VECTOR_SCHEMA_VERSION, or old stored incident vectors become
# silently incomparable to new ones.
VECTOR_SCHEMA_VERSION = 1

DIMENSIONS = [
    "elapsed_ms",
    "throughput_lps",
    "gc_pause_ms",
    "gc_count",
    "heap_usage_pct",
    "blocked_threads",
    "waiting_threads",
    "cpu_pct",
]

# "What counts as a big relative change" per dimension - a relative_delta
# equal to SCALE_CONSTANTS[dim] maps to tanh(1) ~= 0.76. Placeholders -
# tune against your real historical incidents once you have a few.
SCALE_CONSTANTS = {
    "elapsed_ms": 0.5,
    "throughput_lps": 0.5,
    "gc_pause_ms": 1.0,
    "gc_count": 1.0,
    "heap_usage_pct": 0.3,
    "blocked_threads": 1.0,
    "waiting_threads": 1.0,
    "cpu_pct": 0.5,
}


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
    # untracked files (build output, local data) are ignored - they don't affect checkout
    return run_git("status", "--porcelain", "--untracked-files=no").stdout.strip() == ""


def current_branch() -> str:
    return run_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def compute_diff(baseline: str, feature: str, scope_dir: Optional[str] = None,
                  context_lines: int = 3) -> str:
    """
    Returns the raw unified diff between baseline and feature.

    If scope_dir is given, the diff is limited to files under that
    directory (relative to the repo root) - useful for excluding test
    files, generated code, or unrelated modules from what gets passed
    to the LLM. If scope_dir is None, returns the full diff across
    every changed file between the two refs.

    context_lines controls how many unchanged lines git includes around
    each change (git's own default is 3).
    """
    args = ["diff", f"-U{context_lines}", f"{baseline}..{feature}"]
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


def _relative_delta(baseline_val: float, feature_val: float) -> float:
    """
    (feature - baseline) / baseline, with a zero-baseline guard.

    If baseline is 0 (e.g. blocked_threads was 0 and is now 3), a plain
    relative delta is undefined. We fall back to treating any nonzero
    feature value as a full-scale change (1.0), since there's no
    meaningful percentage to compute from zero.
    """
    if baseline_val == 0:
        return 1.0 if feature_val != 0 else 0.0
    return (feature_val - baseline_val) / baseline_val


def metrics_to_vector(baseline_metrics: Dict, feature_metrics: Dict) -> List[float]:
    """
    Converts a (baseline, feature) metrics pair into the fixed-length
    regression signature vector used for incident similarity search.

    Each dimension is tanh(relative_delta / scale_constant):
      - relative_delta captures direction + magnitude of change
      - scale_constant calibrates what counts as "big" per metric
      - tanh bounds the result to [-1, 1] so one extreme metric doesn't
        dominate cosine similarity against the rest

    Direction is intentionally NOT normalized to "bad = positive" - this
    vector describes the shape of what changed, not a good/bad score.

    Raises KeyError if either metrics dict is missing a required
    dimension, rather than silently defaulting to 0.
    """
    vector = []
    for dim in DIMENSIONS:
        if dim not in baseline_metrics:
            raise KeyError(f"baseline_metrics missing required dimension: {dim}")
        if dim not in feature_metrics:
            raise KeyError(f"feature_metrics missing required dimension: {dim}")

        rel_delta = _relative_delta(baseline_metrics[dim], feature_metrics[dim])
        scaled = rel_delta / SCALE_CONSTANTS[dim]
        vector.append(math.tanh(scaled))

    return vector


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    from LLM_Analyzer import analyze as llm_analyze, resolve_client_and_model, PROVIDERS
    from openai import OpenAIError

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--main", default="main", help="Baseline branch/tag to run first (default: main)")
    parser.add_argument("--feature", required=True, help="Feature branch/tag to run second")
    parser.add_argument("--main-class", required=True, help="Entry point class, e.g. MultiThreadLogAggregator")
    parser.add_argument("--log", default="sample.log", help="Path to the log file to process")
    parser.add_argument("--threads", type=int, default=None, help="Thread count arg to pass through")
    parser.add_argument("--classpath", default=None, help="Java classpath to run with (default: the compiled BUILD_DIR)")
    parser.add_argument("--diff-dir", default=None, help="Scope the code diff to this directory (relative to repo root). If omitted, diffs all files.")
    parser.add_argument("--diff-context", type=int, default=3, help="Number of unchanged context lines around each diff hunk (default: 3)")
    parser.add_argument("--top-k", type=int, default=3, help="Max number of similar past incidents to retrieve (default: 3)")
    parser.add_argument("--min-similarity", type=float, default=0.5, help="Minimum cosine similarity for a retrieved incident to be included (default: 0.5)")
    parser.add_argument("--analyze", action="store_true", help="Also run the two-pass LLM analysis on this run's output (requires an API key for --provider)")
    parser.add_argument("--provider", choices=list(PROVIDERS.keys()) + ["custom"], default="gemini", help="LLM provider to use if --analyze is set (default: gemini)")
    parser.add_argument("--model", default=None, help="Override the provider's default model (for --analyze)")
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

    regression_vector = metrics_to_vector(main_result.metrics, feature_result.metrics)

    # top-k similar past incidents, filtered by min_similarity - if
    # nothing clears the threshold this is deliberately an empty list,
    # not a forced weak match (see ChromaStore.query_similar docstring)
    retrieved = query_similar(regression_vector, top_k=args.top_k, min_similarity=args.min_similarity)
    if retrieved:
        print(f"[retrieval] found {len(retrieved)} similar incident(s) above similarity {args.min_similarity}", file=sys.stderr)
    else:
        print(f"[retrieval] no incidents found above similarity {args.min_similarity}", file=sys.stderr)
    retrieved_incidents = [
        {
            "similarity": round(similarity, 4),
            "root_cause": incident["root_cause"],
            "fix": incident["fix"],
            "tags": incident["tags"],
        }
        for similarity, incident in retrieved
    ]

    output = {
        "main": asdict(main_result),
        "feature": asdict(feature_result),
        "diff": compute_diff(args.main, args.feature, scope_dir=args.diff_dir, context_lines=args.diff_context),
        "vector": regression_vector,
        "vector_schema_version": VECTOR_SCHEMA_VERSION,
        "retrieved_incidents": retrieved_incidents,
    }
    output_json = json.dumps(output, indent=2)

    if args.analyze:
        try:
            class _AnalyzeArgs:
                provider = args.provider
                model = args.model
                base_url = None
                api_key_env = None

            client, model = resolve_client_and_model(_AnalyzeArgs())
            llm_result = llm_analyze(output, client, model)
            output["llm_analysis"] = llm_result
            output_json = json.dumps(output, indent=2)

            print(f"\n=== PASS 1: BLIND HYPOTHESIS ({model}) ===", file=sys.stderr)
            print(llm_result["pass1_blind_hypothesis"], file=sys.stderr)
            print(f"\n=== PASS 2: FINAL ANALYSIS ({model}) ===", file=sys.stderr)
            print(llm_result["pass2_final_analysis"], file=sys.stderr)
        except (RuntimeError, ValueError, OpenAIError) as e:
            # Don't let a missing/misconfigured LLM provider (or an API error
            # such as a bad key, unknown model or rate limit) take down a run
            # that otherwise succeeded - metrics/diff/retrieval are still
            # valid and useful even without the LLM step.
            print(f"[warn] skipping LLM analysis: {e}", file=sys.stderr)

    print(output_json)  # stdout - this is what a downstream script/LLM pipe consumes

    if args.out:
        Path(args.out).write_text(output_json)
        print(f"[info] also wrote output to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()