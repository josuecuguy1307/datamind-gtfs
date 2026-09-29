# Local Prompt Runner (Phase 1 + Phase 2)

## What this solves

`local_runner` removes repetitive copy/paste friction when sending long prompts to terminal coding assistants (`codex`, `claude`).

It is local-first and human-in-the-loop:
- dispatch prompt text to CLI assistant
- capture stdout/stderr
- keep prompt lifecycle artifacts
- write run logs for traceability

It does **not** execute returned code, apply patches, or run prompt content as shell commands.

## Folder layout

- `local_runner/config/` configuration
- `local_runner/scripts/` runner scripts
- `local_runner/inbox/` queued prompt files
- `local_runner/processing/` prompt currently running
- `local_runner/done/` completed prompt artifacts
- `local_runner/failed/` failed/timed-out prompt artifacts
- `local_runner/results/` stdout/stderr artifacts
- `local_runner/logs/` JSONL run logs
- `local_runner/templates/` starter prompt templates

## Scripts

- `local_runner/scripts/run_prompt.py`
  - file mode (`--file ...`)
  - clipboard mode (`--clipboard`)
- `local_runner/scripts/run_clipboard_prompt.py`
  - clipboard-only convenience wrapper
- `local_runner/scripts/watch_inbox.py`
  - opt-in inbox watcher (Phase 3)

## Configuration

Edit `local_runner/config/runner_config.yaml`.

### `runner`
- `timeout_seconds`
- `capture_stderr_separately`
- `infer_target_from_filename` (file mode only)
- `default_target` (used by `--target auto`)
- `log_filename`
- `working_directory` (directory used as the subprocess `cwd`; defaults to the project root)

### `clipboard`
- `clipboard_enabled`
- `clipboard_prompt_filename_prefix`
- `clipboard_max_chars`
- `persist_clipboard_prompt`
- `require_target_explicit_for_clipboard`
- `clipboard_persist_destination` (`inbox` or `processing`)

### `watcher` (Phase 3)
- `enabled` (must be true to start watcher)
- `poll_interval_seconds`
- `max_concurrent_per_target`
- `watch_dir` (must stay inside `local_runner/inbox`)
- `auto_infer_target`
- `default_target`
- `ignored_prefixes`

### `targets`
Each target defines:
- `command`: argv list
- `input_mode`: `stdin`, `arg`, `temp_file`, or `interactive_not_supported`

Current default target examples:
- `codex`: `codex exec --skip-git-repo-check -` (`stdin`)
- `claude`: `claude -p` (`stdin`)

If your local CLI version differs, adjust command/input mode accordingly.

`local_runner` is for non-interactive automation only. The console's **AI Assistance** page is the separate, opt-in path for opening an interactive terminal session.

## Usage

### File mode (Phase 1)

```bash
python local_runner/scripts/run_prompt.py \
  --file local_runner/inbox/codex_patch_phase3.txt \
  --target codex
```

```bash
python local_runner/scripts/run_prompt.py \
  --file local_runner/inbox/claude_review_merge.md \
  --target claude
```

### Clipboard mode via unified script (Phase 2)

```bash
python local_runner/scripts/run_prompt.py --clipboard --target codex
```

```bash
python local_runner/scripts/run_prompt.py --clipboard --target claude
```

Use config default target:

```bash
python local_runner/scripts/run_prompt.py --clipboard --target auto
```

One-off clipboard run without prompt artifact persistence:

```bash
python local_runner/scripts/run_prompt.py --clipboard --target codex --no-persist-prompt
```

Print saved output to terminal while still writing artifact files:

```bash
python local_runner/scripts/run_prompt.py --clipboard --target codex --print-output
```

### Clipboard mode via dedicated wrapper

```bash
python local_runner/scripts/run_clipboard_prompt.py --target codex
python local_runner/scripts/run_clipboard_prompt.py --target claude
```

### Inbox watcher (Phase 3)

Start watcher (opt-in):

```bash
python local_runner/scripts/watch_inbox.py --config local_runner/config/runner_config.yaml
```

Stop watcher safely:

- Press `Ctrl+C` or send `SIGTERM`.
- Watcher finishes the current dispatch, then exits.

Lock behavior:

- Watcher creates `local_runner/inbox/.watch_inbox.lock` while running.
- A second watcher instance will refuse to start if lock exists.
- Lock is removed on clean shutdown.

Known limitations:

- Queue execution is serial per target in this MVP.
- Watcher logs additional `prompt_source=watcher` cycle rows to the same JSONL log.
- No daemon/service manager integration in this phase.

## Clipboard backend notes

Clipboard read order:
1. `pyperclip` (if installed)
2. OS fallback commands (`pbpaste`, `powershell Get-Clipboard`, `xclip`, `xsel`, `wl-paste`)

Optional dependency:

```bash
pip install pyperclip
```

If clipboard access fails, runner exits with a clear diagnostic and no hidden traceback spam.

## Prompt lifecycle

### File mode
1. `inbox -> processing`
2. run assistant
3. `processing -> done` on success
4. `processing -> failed` on failure/timeout

### Clipboard mode
- if persistence enabled: a prompt artifact is created and moved through the same lifecycle
- if `--no-persist-prompt`: prompt source remains memory-only (`<clipboard:memory>` in logs)

## Logging and output

### Results
- `local_runner/results/{timestamp}_{target}_{name}.out.txt`
- optional `...err.txt`

### JSONL log
`local_runner/logs/runs.jsonl` includes:
- `timestamp`
- `prompt_source` (`file` / `clipboard`)
- `prompt_file`, `prompt_file_final`
- `prompt_char_count`
- `target`
- `command_used` (sanitized)
- `status` (`success`, `failed`, `timeout`)
- `duration_ms`
- `output_file`, `stderr_file`
- `error_summary`, `return_code`
- `clipboard_char_count` (clipboard mode)

## Troubleshooting

- `Clipboard mode is disabled`: set `clipboard.clipboard_enabled=true`
- `Clipboard is empty`: copy prompt text first
- `Unknown target`: fix `--target` or `targets` config
- `interactive_not_supported`: command is configured as non-automatable; update to non-interactive CLI flags
- `timeout`: increase `runner.timeout_seconds` or fix target command mode
- target prompt hangs: command likely interactive; use a non-interactive form (`codex exec ...`, `claude -p`, etc.)

## Limitations

- No global hotkeys yet
- No n8n/dashboard integration yet
- Non-interactive behavior may vary across CLI versions/auth states
