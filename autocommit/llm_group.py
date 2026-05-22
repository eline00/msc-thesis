"""LLM-based hunk grouping via Azure OpenAI."""

import json
import os
import sys
from pathlib import Path
from openai import AzureOpenAI

# ── Azure OpenAI configuration ────────────────────────────────────────────────
AZURE_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT")
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are an expert software engineer helping to untangle a tangled git commit.

You will be given a numbered list of diff hunks from a single commit that mixes \
multiple unrelated logical concerns (features, bugfixes, refactors, etc.).

Your task is to partition these hunks into groups so that each group contains \
exactly the hunks that belong to the same logical concern.

Rules:
- Every hunk index (0-based) must appear in exactly one group. Do not omit any.
- Prefer fewer, larger groups over many singletons — hunks for the same feature \
  belong together even when they touch different files.
- Order the groups so that dependencies come first (a group that defines something \
  should appear before a group that uses it).
- Return ONLY a JSON object with a single key "groups" whose value is an array of \
  arrays of integer hunk indices.

Example for 5 hunks split into two groups:
{"groups": [[0, 2, 4], [1, 3]]}
"""


def propose_groups(hunk_paths: list[str]) -> list[list[str]]:
    """
    Ask the LLM to partition the given hunks by logical concern.

    Returns a list of groups, each group being a list of hunk *filenames*.

    Falls back to one group per hunk if the API response is invalid.
    """
    client = _build_client()

    sections: list[str] = []
    for i, path in enumerate(hunk_paths):
        content = Path(path).read_text()
        sections.append(f"=== Hunk {i} ({Path(path).name}) ===\n{content}")
    user_message = "\n\n".join(sections)

    _log(f"Calling Azure OpenAI to group {len(hunk_paths)} hunk(s) "
         f"(deployment: {AZURE_DEPLOYMENT}) ...")

    response = client.chat.completions.create(
        model=AZURE_DEPLOYMENT,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        response_format={"type": "json_object"},
        temperature=0, # ranges from 0 (deterministic) to 1 (creative)
    )

    raw = response.choices[0].message.content or ""
    _log(f"LLM raw response: {raw[:300]}{'...' if len(raw) > 300 else ''}")

    try:
        data = json.loads(raw)
        index_groups: list[list[int]] = data["groups"]
    except (json.JSONDecodeError, KeyError) as exc:
        _log(f"WARNING: could not parse LLM response ({exc}). Falling back to singletons.")
        return [[Path(p).name] for p in hunk_paths]

    # Validate that every index appears exactly once
    all_indices = [idx for g in index_groups for idx in g]
    expected = set(range(len(hunk_paths)))
    received = set(all_indices)
    if received != expected or len(all_indices) != len(hunk_paths):
        missing = expected - received
        extra   = received - expected
        _log(f"WARNING: index validation failed — missing={missing}, extra={extra}. "
             f"Falling back to singletons.")
        return [[Path(p).name] for p in hunk_paths]

    names = [Path(p).name for p in hunk_paths]
    result = [[names[i] for i in group] for group in index_groups]
    _log(f"Proposed {len(result)} group(s): sizes={[len(g) for g in result]}")
    return result


def _build_client():
    return AzureOpenAI(
        azure_endpoint=AZURE_ENDPOINT,
        api_key=AZURE_API_KEY,
        api_version=AZURE_API_VERSION,
    )

def _log(msg: str) -> None:
    print(f"  [llm_group] {msg}", file=sys.stderr, flush=True)
