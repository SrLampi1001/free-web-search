# Coding Assistance — `free-web-search-mcp`

> Session transcript and prompt corpus for the development of this project. Mirrors the `simple-cli-coder-with-rag/coding-assistance/` pattern: a `docs/prompts/` directory of session JSON files plus a `prompts.jsonl` consolidated from the user prompts in those sessions.

## Interface — Harness

[OpenCode](https://opencode.ai) is the CLI and harness used for development of this project. No extra plugins, no custom configurations, vanilla OpenCode v2.0.21 ([changelog](https://opencode.ai/changelog) says v1.18.34 is latest, but `opencode --version` reports v2.0.21).

## Models

- **OpenCode — Fledge Alpha Free, Nemotron 3.5 Lighting Free.** Coding and bug-solving tasks. Documentation refinement.
- **Minimax — MiniMax M3, MiniMax M2.7.** Coding, planning, and refinement tasks. Documentation refinement.
- **Claude (web interface).** Planning and research tasks, refinement.

## Layout

```
docs/prompts/session-*.json     # raw session captures (gitignored)
coding-assistance/
├── README.md                   # this file
└── prompts.jsonl               # consolidated user prompts, one per line
```

The `session-*.json` files are dropped into `docs/prompts/` by the operator after each session (or by an export pipeline). The script at `simple-cli-coder-with-rag/scripts/prompts_jsonl.py` reads them and emits one user-prompt record per line into `prompts.jsonl`.

## How to regenerate `prompts.jsonl`

From the project root:

```bash
uv run python ../simple-cli-coder-with-rag/scripts/prompts_jsonl.py \
    --source-dir docs/prompts \
    --output    coding-assistance/prompts.jsonl
```

Each output line is shaped:

```json
{
  "session_id":    "ses_…",
  "session_title": "…",
  "agent":         "build",
  "model":         {"id": "…", "providerID": "…", "variant": "…"},
  "source_file":   "session-<uuid>.json",
  "message": {
    "id":   "msg_…",
    "type": "user",
    "time": {"created": <epoch_ms>},
    "text": "<prompt body>",
    "files": [...]
  }
}
```

`assistant` and `idle` messages are skipped. See the script's docstring for the full schema and edge-case handling.

## Notes

- **`docs/prompts/session-*.json` is gitignored.** The raw captures contain full model output, reasoning blocks, and tokens-per-call metadata. Commit only the consolidated `prompts.jsonl`.
- **Empty input exits 0 with a warning.** The script creates an empty `prompts.jsonl` if no user prompts are found, so this directory can be staged safely even when no sessions have been exported yet.
- **Custom paths are supported.** Override `--source-dir` and `--output` to point at any session directory.
