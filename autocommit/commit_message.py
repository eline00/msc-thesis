"""Groq API integration for commit message generation."""

import os
import sys

from dotenv import load_dotenv
load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")

_MODEL = "llama-3.3-70b-versatile"

_SYSTEM_PROMPT = """\
You are an expert software engineer writing git commit messages.
Given a unified diff, write a single-line conventional commit message that summarises the change.
Format: <type>: <description>
Types: feat, fix, refactor, test, docs, style, chore
Keep it under 72 characters. Return ONLY the commit message, nothing else.\
"""


def generate(patch_content: str, group_num: int, hunk_count: int, hunk_names: str) -> str:
    """
    Generate a commit message for the given patch using Groq.
    Falls back to a template message if the API key is missing or the call fails.
    """
    fallback = f"etc[{group_num}]: {hunk_count} hunk(s) — {hunk_names}"

    if not GROQ_API_KEY:
        _log("GROQ_API_KEY not found — using template commit message")
        return fallback

    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        response = client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": patch_content},
            ],
            max_tokens=100,
        )
        message = (response.choices[0].message.content or "").strip()
        if not message:
            return fallback
        _log(f"Generated commit message: {message}")
        return message
    except Exception as exc:
        _log(f"WARNING: Groq call failed ({exc}) — using template commit message")
        return fallback


def _log(msg: str) -> None:
    print(f"  [commit_message] {msg}", file=sys.stderr, flush=True)
