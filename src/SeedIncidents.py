#!/usr/bin/env python3
"""
seed_incidents.py

Populates a Chroma collection with synthetic incident records built from
general, textbook performance-regression patterns (lock contention, GC
pressure, unbuffered I/O, algorithmic complexity blowups, etc.) - NOT
real incidents from this project. Every record is tagged "synthetic" so
it can be told apart from (and later deleted in favor of) genuine
incidents once you've accumulated real ones.

Purpose: exercise the retrieval pipeline end-to-end (vector generation
-> storage -> cosine similarity -> top-k) with a plausible-looking
incident population, before real incident data exists.

Run:
    python seed_incidents.py            # appends 60 synthetic incidents
    python seed_incidents.py --wipe     # clears the store first
"""

import argparse
import random
from pathlib import Path

from RunRegressionCheck import metrics_to_vector, VECTOR_SCHEMA_VERSION
from ChromaStore import add_incident, wipe

# A representative "healthy baseline" run, close to this project's real
# main-branch numbers, used as the reference point for every synthetic
# incident's baseline_metrics.
BASE_METRICS = {
    "elapsed_ms": 380,
    "throughput_lps": 2600000,
    "gc_pause_ms": 6,
    "gc_count": 4,
    "heap_usage_pct": 4.3,
    "blocked_threads": 0,
    "waiting_threads": 2,
    "cpu_pct": 10.0,
}

# Each pattern: relative % change per unit severity for continuous
# metrics (rel), and absolute count added per unit severity for
# count-based metrics (abs). Severity multiplies both.
PATTERNS = [
    {
        "name": "gc_pressure_allocation",
        "root_cause": "New version allocates significantly more short-lived objects per record (boxing, extra intermediate collections), increasing GC frequency and pause time.",
        "fix": "Removed intermediate object allocations in the hot loop; reused buffers instead of allocating per iteration.",
        "rel": {"elapsed_ms": 0.12, "throughput_lps": -0.10, "gc_pause_ms": 0.9, "heap_usage_pct": 0.4, "cpu_pct": 0.08},
        "abs": {"gc_count": 3},
    },
    {
        "name": "memory_leak_cache_growth",
        "root_cause": "A cache or listener added in the new version is never evicted, causing heap usage to grow and GC pressure to increase over the run.",
        "fix": "Added an eviction policy and cleared cache references after use.",
        "rel": {"heap_usage_pct": 0.8, "gc_pause_ms": 0.5, "elapsed_ms": 0.08},
        "abs": {"gc_count": 2},
    },
    {
        "name": "thread_pool_undersized",
        "root_cause": "Thread pool size was reduced or capped too low for the workload, causing tasks to queue and wait for a free worker.",
        "fix": "Restored/increased thread pool size to match available cores and workload.",
        "rel": {"elapsed_ms": 0.35, "throughput_lps": -0.25, "cpu_pct": -0.15},
        "abs": {"waiting_threads": 4},
    },
    {
        "name": "lock_contention_shared_resource",
        "root_cause": "A newly added synchronized block around a shared counter serializes what used to be parallel work, causing threads to block on each other.",
        "fix": "Replaced the synchronized block with a lock-free atomic counter and reduced lock scope.",
        "rel": {"elapsed_ms": 0.4, "throughput_lps": -0.3, "cpu_pct": -0.2},
        "abs": {"blocked_threads": 5},
    },
    {
        "name": "deadlock_adjacent_contention",
        "root_cause": "Inconsistent lock acquisition order between two code paths causes threads to block on each other for extended periods, nearly stalling the pipeline.",
        "fix": "Standardized lock acquisition order across all code paths to eliminate circular wait.",
        "rel": {"elapsed_ms": 1.2, "throughput_lps": -0.6, "cpu_pct": -0.4},
        "abs": {"blocked_threads": 10},
    },
    {
        "name": "unbuffered_blocking_io",
        "root_cause": "Switched to unbuffered/raw file reads (e.g. byte-at-a-time RandomAccessFile.readLine) instead of a buffered reader, causing excessive syscalls per line.",
        "fix": "Wrapped file access in a BufferedReader, or loaded the file into memory before parallel processing.",
        "rel": {"elapsed_ms": 0.6, "throughput_lps": -0.45, "cpu_pct": -0.3},
        "abs": {"waiting_threads": 2},
    },
    {
        "name": "disk_io_contention",
        "root_cause": "All worker threads read from disk directly and concurrently, causing seek contention on a single disk device.",
        "fix": "Loaded the file into memory once before spawning worker threads, eliminating concurrent disk reads.",
        "rel": {"elapsed_ms": 0.5, "throughput_lps": -0.35},
        "abs": {"waiting_threads": 3},
    },
    {
        "name": "cpu_busy_wait_spin_lock",
        "root_cause": "A polling loop was introduced that spins checking a flag instead of blocking, consuming CPU without making progress.",
        "fix": "Replaced busy-wait polling with a proper blocking wait/notify or condition variable.",
        "rel": {"elapsed_ms": 0.25, "throughput_lps": -0.15, "cpu_pct": 0.6},
        "abs": {},
    },
    {
        "name": "excessive_logging_serialization",
        "root_cause": "Verbose per-record logging (e.g. JSON serialization of each record for debug output) was left enabled, adding CPU and I/O overhead per record.",
        "fix": "Reduced log level and removed per-record serialization from the hot path.",
        "rel": {"elapsed_ms": 0.18, "cpu_pct": 0.2, "throughput_lps": -0.12},
        "abs": {},
    },
    {
        "name": "connection_pool_starvation",
        "root_cause": "Downstream connection pool size wasn't scaled with increased concurrency, causing threads to wait for an available connection.",
        "fix": "Increased connection pool size and added backpressure handling.",
        "rel": {"elapsed_ms": 0.3, "throughput_lps": -0.2},
        "abs": {"waiting_threads": 5},
    },
    {
        "name": "cache_thrashing",
        "root_cause": "A change in access pattern (e.g. processing order) reduced cache locality, increasing cache misses and CPU cycles per operation.",
        "fix": "Restored sequential access pattern and adjusted chunk size to fit cache lines better.",
        "rel": {"elapsed_ms": 0.15, "cpu_pct": 0.25},
        "abs": {},
    },
    {
        "name": "n_plus_one_pattern",
        "root_cause": "A batched operation was replaced with per-record calls (e.g. per-line lookups instead of a single batched pass), multiplying overhead per record.",
        "fix": "Batched the repeated operation back into a single pass.",
        "rel": {"elapsed_ms": 0.45, "throughput_lps": -0.3, "cpu_pct": 0.15},
        "abs": {},
    },
    {
        "name": "algorithmic_complexity_regression",
        "root_cause": "A linear scan replaced an indexed/hashed lookup, degrading complexity from roughly O(n) to O(n^2) as data volume grew.",
        "fix": "Reintroduced a HashMap-based lookup instead of nested linear scans.",
        "rel": {"elapsed_ms": 0.9, "throughput_lps": -0.5, "cpu_pct": 0.35},
        "abs": {},
    },
    {
        "name": "thread_creation_overhead",
        "root_cause": "New threads are created per chunk instead of reusing a thread pool, adding thread startup overhead and increasing GC churn from short-lived Thread objects.",
        "fix": "Switched to a reused ExecutorService thread pool instead of spawning new threads per task.",
        "rel": {"elapsed_ms": 0.2, "cpu_pct": 0.15},
        "abs": {"gc_count": 2},
    },
    {
        "name": "reduced_parallelism",
        "root_cause": "The thread-count argument/config was accidentally lowered or hardcoded down, reducing effective parallelism.",
        "fix": "Restored the configured thread count to match available cores.",
        "rel": {"elapsed_ms": 0.5, "throughput_lps": -0.35, "cpu_pct": -0.4},
        "abs": {},
    },
]

SEVERITIES = [0.5, 1.0, 1.5, 2.0]


def apply_pattern(base: dict, pattern: dict, severity: float, rng: random.Random) -> dict:
    """
    Produces a synthetic feature_metrics dict by perturbing base according
    to the pattern's relative/absolute deltas, scaled by severity, with a
    small amount of jitter so the 4 severity variants aren't perfectly
    smooth multiples of each other.
    """
    feature = dict(base)
    for dim, pct in pattern["rel"].items():
        jitter = rng.uniform(-0.05, 0.05)
        feature[dim] = base[dim] * (1 + pct * severity + jitter)
    for dim, amount in pattern["abs"].items():
        feature[dim] = base[dim] + amount * severity

    # round sensibly per metric type
    feature["elapsed_ms"] = round(feature["elapsed_ms"])
    feature["throughput_lps"] = round(feature["throughput_lps"])
    feature["gc_pause_ms"] = max(0, round(feature["gc_pause_ms"]))
    feature["gc_count"] = max(0, round(feature["gc_count"]))
    feature["heap_usage_pct"] = round(feature["heap_usage_pct"], 2)
    feature["blocked_threads"] = max(0, round(feature["blocked_threads"]))
    feature["waiting_threads"] = max(0, round(feature["waiting_threads"]))
    feature["cpu_pct"] = max(0.1, round(feature["cpu_pct"], 2))
    return feature


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wipe", action="store_true", help="Clear incidents.jsonl before seeding")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for jitter (default: 42)")
    args = parser.parse_args()

    if args.wipe:
        wipe()
        print("[info] wiped chroma_data collection")

    rng = random.Random(args.seed)
    count = 0
    for pattern in PATTERNS:
        for severity in SEVERITIES:
            feature_metrics = apply_pattern(BASE_METRICS, pattern, severity, rng)
            vector = metrics_to_vector(BASE_METRICS, feature_metrics)
            add_incident(
                vector=vector,
                vector_schema_version=VECTOR_SCHEMA_VERSION,
                root_cause=pattern["root_cause"],
                fix=pattern["fix"],
                baseline_metrics=BASE_METRICS,
                feature_metrics=feature_metrics,
                tags=["synthetic", pattern["name"], f"severity_{severity}"],
            )
            count += 1

    print(f"[info] seeded {count} synthetic incidents into Chroma collection 'regression_incidents'")


if __name__ == "__main__":
    main()