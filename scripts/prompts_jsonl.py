"""Consolidate OpenCode session JSON files into a single JSONL of user prompts.

Reads every ``*.json`` file in ``coding-assitance/json-sessions/`` and writes
one JSON line per **user prompt** (any message with ``type: "user"``) into
``coding-assitance/prompts.jsonl``. Assistant replies (``type: "assistant"``)
and idle markers (``type: "idle"``) are skipped.

Each line is the original user message wrapped with session-level context
extracted from the parent file's ``info`` block:

    {
      "session_id":    "ses_…",
      "session_title": "…",
      "agent":         "build",
      "model":         {"id": "…", "providerID": "…", "variant": "…"},
      "source_file":   "session-<uuid>.json",
      "message": {
        "id":    "msg_…",
        "type":  "user",
        "time":  {"created": <epoch_ms>},
        "text":  "<prompt body>",
        "files": [...],
        "agents": [...]
      }
    }

The prompt body lives at ``message.text``; referenced attachments (file
mentions / inline data) live at ``message.files``.

Run from the project root:

    uv run python scripts/prompts_jsonl.py

Both paths default to the locations above and can be overridden:

    uv run python scripts/prompts_jsonl.py \\
        --source-dir coding-assitance/json-sessions \\
        --output    coding-assitance/prompts.jsonl

The script prints how many user prompts were written on success and
exits with status ``0``. It exits ``2`` if the source directory is
missing and ``1`` with a warning if no user prompts are found (the
output is still created, empty, in that case).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_DIR = REPO_ROOT / "coding-assistance" / "json-sessions"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "coding-assistance" / "prompts.jsonl"


def _iter_user_prompts(session: dict[str, Any], source_file: str) -> Iterator[dict[str, Any]]:
    """Yield one record per ``type: "user"`` message in ``session``.

    Each record combines session-level fields from the ``info`` object with
    the original message (kept verbatim under ``message``) and the ``source_file``
    filename for traceability.
    """
    info = session.get("info") or {}
    for message in session.get("messages", []):
        if message.get("type") != "user":
            continue
        yield {
            "session_id": info.get("id"),
            "session_title": info.get("title"),
            "agent": info.get("agent"),
            "model": info.get("model"),
            "source_file": source_file,
            "message": message,
        }


def consolidate(source_dir: Path, output_path: Path) -> int:
    """Write one JSON line per user prompt in ``source_dir`` to ``output_path``."""
    if not source_dir.is_dir():
        print(f"error: source directory not found: {source_dir}", file=sys.stderr)
        return 2

    json_files = sorted(source_dir.glob("*.json"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with output_path.open("w", encoding="utf-8") as out:
        for path in json_files:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    session = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"warning: skipping {path.name}: {exc}", file=sys.stderr)
                continue
            for record in _iter_user_prompts(session, path.name):
                out.write(json.dumps(record, ensure_ascii=False))
                out.write("\n")
                written += 1

    if written == 0:
        print(f"warning: no user prompts found in {source_dir}", file=sys.stderr)
    else:
        print(f"wrote {written} user prompt(s) to {output_path}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Consolidate session JSON files into a single .jsonl of user prompts.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"Directory of input session JSON files (default: {DEFAULT_SOURCE_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Output .jsonl path (default: {DEFAULT_OUTPUT_PATH})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return consolidate(args.source_dir.resolve(), args.output.resolve())


if __name__ == "__main__":
    sys.exit(main())
