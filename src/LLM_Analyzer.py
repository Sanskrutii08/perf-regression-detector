#!/usr/bin/env python3
"""
llm_analyzer.py

Takes the JSON output from branch_metrics_runner.py (metrics, diff,
vector, retrieved_incidents) and runs a TWO-PASS LLM analysis to
hypothesize the root cause of a performance regression.

PROVIDER-AGNOSTIC: uses the OpenAI Python SDK against whichever
provider's OpenAI-compatible endpoint you point it at. Every provider
below exposes one - only the base_url, api key, and model name change:

  gemini      https://generativelanguage.googleapis.com/v1beta/openai/
  groq        https://api.groq.com/openai/v1
  openrouter  https://openrouter.ai/api/v1
  anthropic   https://api.anthropic.com/v1/   (Anthropic's own OpenAI-
                                                compatible beta endpoint)

Swapping providers is a --provider flag, not a code change. This is the
point of the refactor: the prompt-construction and two-pass logic below
never touch the SDK details, so a provider swap can't silently change
how the analysis itself is built.

Why two passes (recap of the design decision made earlier):
  PASS 1 - blind hypothesis: model sees ONLY the metrics deltas (no code
    diff, no past incidents) and guesses what kind of change could cause
    this pattern. This is the model's unbiased first read.
  PASS 2 - context-checked hypothesis: model now sees the code diff and
    retrieved similar incidents, and is asked to either confirm, revise,
    or reject its pass-1 hypothesis using the actual evidence.

Usage:
    python branch_metrics_runner.py --main main --feature dev \
        --main-class LogAggregatorV2InMemory --log sample.log \
        > run_output.json

    python llm_analyzer.py run_output.json --provider gemini
    python llm_analyzer.py run_output.json --provider groq --model llama-3.3-70b-versatile
    python llm_analyzer.py run_output.json --provider custom \
        --base-url https://my-endpoint/v1 --api-key-env MY_KEY --model my-model

Each provider's API key is read from its own environment variable
(see PROVIDERS below) so keys for multiple providers can coexist
without collision.
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI

# ---------------------------------------------------------------------------
# Provider presets - base_url, default model, and which env var holds the
# API key. "default_model" is deliberately None where free-tier model
# availability changes too often to hardcode safely (e.g. OpenRouter's
# free model list) - those require an explicit --model.
# ---------------------------------------------------------------------------
@dataclass
class ProviderConfig:
    base_url: str
    default_model: Optional[str]
    api_key_env: str


PROVIDERS = {
    "gemini": ProviderConfig(
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model="gemini-2.5-flash",
        api_key_env="GEMINI_API_KEY",
    ),
    "groq": ProviderConfig(
        base_url="https://api.groq.com/openai/v1",
        default_model="llama-3.3-70b-versatile",
        api_key_env="GROQ_API_KEY",
    ),
    "openrouter": ProviderConfig(
        base_url="https://openrouter.ai/api/v1",
        default_model=None,  # free model slugs change often - pass --model explicitly
        api_key_env="OPENROUTER_API_KEY",
    ),
    "anthropic": ProviderConfig(
        base_url="https://api.anthropic.com/v1/",
        default_model="claude-haiku-4-5-20251001",
        api_key_env="ANTHROPIC_API_KEY",
    ),
}


def resolve_client_and_model(args) -> tuple:
    """
    Builds the OpenAI client + model string for whichever provider was
    requested. Returns (client, model_name).
    """
    if args.provider == "custom":
        if not args.base_url or not args.api_key_env or not args.model:
            raise ValueError("--provider custom requires --base-url, --api-key-env, and --model")
        base_url, api_key_env, model = args.base_url, args.api_key_env, args.model
    else:
        config = PROVIDERS[args.provider]
        base_url = args.base_url or config.base_url
        api_key_env = args.api_key_env or config.api_key_env
        model = args.model or config.default_model
        if not model:
            raise ValueError(
                f"No default model for provider '{args.provider}' - pass --model explicitly"
            )

    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(
            f"Environment variable {api_key_env} is not set (needed for provider '{args.provider}')"
        )

    client = OpenAI(api_key=api_key, base_url=base_url)
    return client, model


# ---------------------------------------------------------------------------
# Prompt construction (provider-independent - same prompts regardless of
# which model answers them)
# ---------------------------------------------------------------------------
def build_pass1_prompt(main_metrics: dict, feature_metrics: dict) -> str:
    return f"""You are analyzing a performance regression between two versions of a Java service.

Below are JVM/runtime metrics from a baseline run and a feature-branch run of the SAME workload. You are NOT shown any code changes yet - reason only from the metric deltas.

BASELINE METRICS:
{json.dumps(main_metrics, indent=2)}

FEATURE BRANCH METRICS:
{json.dumps(feature_metrics, indent=2)}

Based only on which metrics changed and by how much, give your best hypothesis for what KIND of code change could produce this pattern (e.g. lock contention, GC pressure, blocking I/O, reduced parallelism, algorithmic complexity increase, etc.).

Keep it to 3-5 sentences. Be specific about which metric deltas support your hypothesis and why."""


def build_pass2_prompt(pass1_hypothesis: str, diff: str, retrieved_incidents: list) -> str:
    if retrieved_incidents:
        incidents_block = "\n\n".join(
            f"[Similarity: {inc['similarity']}]\n"
            f"Root cause: {inc['root_cause']}\n"
            f"Fix: {inc['fix']}"
            for inc in retrieved_incidents
        )
    else:
        incidents_block = "(No similar past incidents found above the similarity threshold.)"

    return f"""Here is your initial hypothesis from analyzing the metrics alone, without seeing the code:

"{pass1_hypothesis}"

Now here is the actual code diff between the two branches:

{diff if diff.strip() else "(No diff content - files may be identical or diff scope excluded all changes.)"}

And here are similar past incidents retrieved from the incident database, ranked by similarity to this regression's metric signature:

{incidents_block}

Using this evidence, do ONE of the following:
1. CONFIRM your original hypothesis if the diff supports it - point to the specific lines that cause it.
2. REVISE your hypothesis if the diff suggests something more specific or different.
3. REJECT your original hypothesis if the diff contradicts it, and give the correct root cause instead.

Then give a final root cause explanation (3-5 sentences) and a suggested fix, referencing the specific code change. If a retrieved past incident's fix is directly applicable, say so explicitly."""


def call_llm(client: OpenAI, model: str, prompt: str) -> str:
    response = client.chat.completions.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content


def analyze(run_output: dict, client: OpenAI, model: str) -> dict:
    main_metrics = run_output["main"]["metrics"]
    feature_metrics = run_output["feature"]["metrics"]
    diff = run_output.get("diff", "")
    retrieved_incidents = run_output.get("retrieved_incidents", [])

    print(f"[llm] pass 1 - blind hypothesis from metrics only ({model})...", file=sys.stderr)
    pass1_prompt = build_pass1_prompt(main_metrics, feature_metrics)
    pass1_hypothesis = call_llm(client, model, pass1_prompt)

    print(f"[llm] pass 2 - checking hypothesis against diff + retrieved incidents ({model})...", file=sys.stderr)
    pass2_prompt = build_pass2_prompt(pass1_hypothesis, diff, retrieved_incidents)
    pass2_result = call_llm(client, model, pass2_prompt)

    return {
        "provider_model": model,
        "pass1_blind_hypothesis": pass1_hypothesis,
        "pass2_final_analysis": pass2_result,
        "num_retrieved_incidents": len(retrieved_incidents),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input_json", help="Path to the JSON output from branch_metrics_runner.py")
    parser.add_argument("--provider", choices=list(PROVIDERS.keys()) + ["custom"], default="gemini",
                         help="Which provider's OpenAI-compatible endpoint to use (default: gemini)")
    parser.add_argument("--model", default=None, help="Override the provider's default model")
    parser.add_argument("--base-url", default=None, help="Override the provider's base URL (required for --provider custom)")
    parser.add_argument("--api-key-env", default=None, help="Env var holding the API key (required for --provider custom)")
    parser.add_argument("--out", default=None, help="Optional path to write the analysis JSON to")
    args = parser.parse_args()

    client, model = resolve_client_and_model(args)

    with open(args.input_json) as f:
        run_output = json.load(f)

    result = analyze(run_output, client, model)
    result_json = json.dumps(result, indent=2)

    print(f"\n=== PASS 1: BLIND HYPOTHESIS ({model}) ===")
    print(result["pass1_blind_hypothesis"])
    print(f"\n=== PASS 2: FINAL ANALYSIS ({model}) ===")
    print(result["pass2_final_analysis"])

    if args.out:
        with open(args.out, "w") as f:
            f.write(result_json)
        print(f"\n[info] wrote analysis to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()