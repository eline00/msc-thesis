"""Google Gemini integration for commit message generation."""

import os
import sys

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

_MODEL = "gemini-2.0-flash"

_SYSTEM_PROMPT = """\
You are an expert software engineer writing git commit messages.
Given a unified diff, write a single-line conventional commit message that summarises the change.
Format: <type>(<scope>): <description>
Types: feat, fix, refactor, test, docs, style, chore
Keep it under 72 characters. Return ONLY the commit message, nothing else.\
"""


def generate(patch_content: str, group_num: int, hunk_count: int, hunk_names: str) -> str:
    """
    Generate a commit message for the given patch using Google Gemini.
    Falls back to a template message if the API key is missing or the call fails.
    """
    fallback = f"etc[{group_num}]: {hunk_count} hunk(s) — {hunk_names}"

    if not GOOGLE_API_KEY:
        _log("GOOGLE_API_KEY not found — using template commit message")
        return fallback

    try:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=GOOGLE_API_KEY)
        response = client.models.generate_content(
            model=_MODEL,
            contents=patch_content,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
            ),
        )
        message = (response.text or "").strip()
        if not message:
            return fallback
        _log(f"Generated commit message: {message}")
        return message
    except Exception as exc:
        _log(f"WARNING: Gemini call failed ({exc}) — using template commit message")
        return fallback


def _log(msg: str) -> None:
    print(f"  [commit_message] {msg}", file=sys.stderr, flush=True)
