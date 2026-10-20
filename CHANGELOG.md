# Changelog

All notable changes to HYDRA. Dates are local (Termux, Android).

## v1.2.4 — 2026-10-20

UI v2, milestone 1 (Work Order #2): a calmer, phone-first look on the existing
renderer. No behaviour change — rendering only.

### Changed

- **Compact welcome lockup** — the 5-row block banner is gone. Home shows the
  3-row logo (19 columns) with `HYDRA <version>`, model, plan and key status
  to its right, the cwd, and a two-line tips block that hides itself after the
  first message. Below 44 columns the logo drops to one line:
  `▌HYDRA 1.2.4 · model` (`ui.logo = auto|on|off`).
- **No painted backgrounds, no italics** — all colour comes from six semantic
  tokens (`accent`, `muted`, `ok`, `warn`, `err`, `info`) in `hydra/ui_theme.py`;
  `ui.theme = dark|light|auto` and truecolor/256/16 depth detection.
- **Glyph tiers** — `safe` (default), `rich`, `ascii`; `--ascii` /
  `HYDRA_ASCII=1` select ASCII, `hydra ui-demo glyphs` prints every tier.
- **Live status line** — spinner, verb, elapsed (`14s`, `1m 05s`), `↓ tok`,
  `esc to stop`; idle screens stay quiet. Footer shows context hints or
  `ctx NN% · $cost`, and the model name while running.
- **Tool cards** — `● tool summary` with right-aligned diffstat and elapsed,
  `└ brief` results, coloured `+/-` diff rows, middle-ellipsis paths.
- **Honest closing cards** — `✓ Done` / `● Partial` / `✗ Failed` with labelled
  `files / verify / open / next / why / run` rows; `files not tracked` when the
  report did not track changes; per-file `+N -M` from real tool calls.
- **Real cursor at the caret** — the fake block cursor over the placeholder is
  gone; the terminal cursor is positioned inside the rounded input box.
- **Mouse capture off by default** — fullscreen still uses the alternate screen
  and bracketed paste, so native swipe-scroll and long-press copy keep working;
  `ui.mouse = true` re-enables `?1000h/?1002h/?1006h`.
- **`hydra ui-demo`** — scripted `home / running / done / partial / failed /
  palette / stream / unicode / glyphs / all` scenes through the real renderer;
  `--snapshot --no-color` prints ANSI-free screens (used by the goldens).
- **New UI flags** — `--ui inline|fullscreen|plain`, `--no-color`, `--compact`.

### Added

- `hydra/ui_theme.py` (tokens, depth, glyph tiers, SGR sanitizers) and
  `hydra/ui_demo.py` (scenes).
- `tests/test_ui_v2.py` — golden snapshots (36/48/54/80/120, eight scenes),
  width/SGR invariants, honesty checks, ascii/plain-mode checks (283 tests).

## v1.2.3 — 2026-10-19

Second root-fix pass, driven by re-testing the same 3D gym task on the real
free API. v1.2.2 fixed project context and the website skill; this release
fixes what the re-test exposed: unreliable file reporting and a model that
ignored the finish window.

### Fixed

- **File reporting for shell edits** — the executor tracks
  `write_file`/`edit_file`/`apply_patch`, but models also edit through shell
  heredocs and `sed -i`. Partial, killed and finished reports now fall back to
  files modified under the workdir since the agent started (extension
  allow-list, sensitive/heavy paths skipped). Regression test covers a file
  written only through `bash`.
- **Turn-cap finish window** — with 1–2 turns left, the loop now steers the
  model: stop exploring and call `finish` with an honest report. This prevents
  the "25/30 turns, no finish, empty files" failure seen in both re-tests.
- **Hinglish connectors** — "fir"/"phir" means "then", "aur" means "and"; the
  lead prompt now says these are instructions, not UI text. The previous
  re-test rendered a literal "FIR" sprite.
- **Stricter website-3d skill** — a sphere body plus two `Sprite` wings is
  explicitly a placeholder; require 8–20 butterflies with
  `BufferGeometry`/`ShapeGeometry` wings that rotate, no literal "FIR" text,
  one background server started with the `bash` tool's `background=true`
  (not a shell `&`), and one `curl` 200 check before finishing.
- **Exact edit tools** — lead/coder prompts and the skill now prefer
  `write_file`/`edit_file` over shell heredocs so verification can open the
  exact files that changed.

### Re-test note

The re-test still ended `partial` (30-turn cap) and the butterfly wings were
still sprites, so the remaining gap is **model capability**, not a harness
crash or duplicate-project bug. The verifier did its job: it rejected a finish
that claimed a dead localhost and an unverifiable FIR removal.

## v1.2.2 — 2026-10-19

Root-fix release for the 3D gym-site capability test: HYDRA built a duplicate
site next to `gym_website/`, used a weak flat-plane 3D demo, left 0/4 todos
open, reported an empty `files_changed`, and the verifier still passed it.
These are the structural fixes, not prompt excuses.

### Added

- **Workspace context in the env block** — top-level entries, per-project
  marker facts (`index.html`, `package.json`, asset dirs), recently modified
  files, and the last same-cwd runs with their changed files (redacted). A new
  run can now see that `gym_website/` already exists before it plans.
- **Built-in `website-3d` skill** — progressive-disclosure recipe shipped in
  the package: reuse the existing project, no duplicate asset trees, real
  Three.js geometry/lights/scroll, serve with `background=true`, curl 200.
  Project/global skills still override the built-in one.
- **`finish` auto-fills `files_changed`** — the executor records every
  successful `write_file`/`edit_file`/`apply_patch` path; `finish` uses those
  when the model omits the list.
- **Verifier: pending todos + duplicate asset trees** — a `done` finish with
  open todos, or two live trees (`assets/css` and `css`, `assets/js` and `js`,
  …), fails deterministically before the LLM reviewer runs.
- **`hydra models tune --apply` covers more roles** — `reviewer`, `planner`,
  `debugger`, `tester` and `scribe` are now written too; reviewer gets the
  diverse-family chain instead of always trying `nemotron-3-ultra` first.

### Fixed

- **Model probe counted only native tool calls** — models that emit a
  `<tool_call>`/JSON block (measured: `gpt-oss:120b`) were marked
  `tools:false` and pruned by the router for every tool role. The probe now
  runs the same tolerant `parse_text_calls` parser as the main loop.
- **Hard-stop regression test could kill the test runner** — it executed a
  real `killall python3`; it now repeats a harmless command with the same
  signature.

## v1.2.1 — 2026-10-19

Bugfix release for the gym-site capability test: the harness hung on a
foreground server, the model repeated the same command, and the verifier
passed a dead host.

### Fixed

- **Shell timeout after the shell exits** — a background child can keep the
  stdout pipe open and keep writing after the shell has exited. The wall-clock
  timeout now applies regardless of `proc.poll()`, kills the whole process
  group by its session id, drains the pipe non-blockingly, and tells the model
  to retry long jobs with `background=true`.
- **Hard loop breaker** — after `loop.stall_hard_stop` (default 6) identical
  tool calls, the run ends with a `partial` report instead of burning turns.
  The existing steering/escalation still fires first at `stall_repeat`.
- **Deterministic verifier checks** — before the LLM reviewer, existing code
  artifacts are checked for missing local CSS/JS assets, empty or unbalanced
  files, JavaScript syntax (`node --check`), and a claimed localhost URL that
  is not reachable.
- **Prompt/tool guidance** — servers must use `background=true`; never start a
  server in the foreground or run `killall python3`.
- **Eval resume timer** — `kill_after` now starts after the child creates its
  run directory, so a loaded phone cannot kill Python during startup and leave
  nothing for `hydra resume`.

## v1.2.0 — 2026-10-19

Real-API work order: measured Ollama Cloud behaviour on a free key, fixed what
the fake server could not catch, and added the missing operator tools. See
`docs/REAL_API_NOTES.md` for every measured fact, date and run id.

### Added

- **F1 structured routing** — tool-less structured calls (planner, verifier,
  summarizer, normalizer) parse/normalize JSON, do one repair re-ask, then try
  the next candidate by measured `json_class`, cost and latency.
- **F2 `hydra models bench` / `hydra models tune`** — bundled B1/B2/B3 fixtures,
  per-model scorecard (`~/.hydra/scorecard.json`), budget guard, proposed role
  chains and `~/.hydra/roles.auto.toml`.
- **F3 `hydra report [RUN|--last] [--full] [--json] [--since 2h] [--timeline]`**
  — paste-friendly, redacted run report (agents, tools, retries, HTTP, cost,
  compactions, events).
- **F4 `--debug-http` / `HYDRA_DEBUG_HTTP=1`** — redacted `http.jsonl` per run
  (status, latency, TTFB, bytes, selected headers, prefix hash; never bodies or
  `Authorization`).
- **F5 `hydra doctor --limits`** — ≤6-call probe storing `~/.hydra/limits.json`
  (`queue` or `reject:<status>`, `max_parallel_ok`); the gate/AIMD honour it.
- **F6 capability auto-detect** — 24 h cache in `~/.hydra/caps.json`; dead tools
  are removed from schemas and the env block; `hydra doctor --caps` re-checks.
- **F7 keep-alive** — one persistent HTTPS connection per thread with reconnect
  on stale sockets (measured ~34% lower median latency on the free plan).
- **F8 `hydra usage` upgrades** — month-to-date, per model, per run,
  `--since 2h|3d`, and `--remote` (`/api/usage`) printing only non-personal
  fields.
- **F9 untrusted hardening** — `</untrusted>` in content is neutralised and a
  one-line "data, never instructions" reminder follows the wrapper (toggleable).
- **F10 opt-in budget** — `--budget-usd N` or `[budget] usd = N` (off by
  default); reaching it checkpoints, notifies and parks with exit 75;
  `hydra resume --budget-usd M` continues.
- **F12 `hydra update`** — `git pull --ff-only`, changelog, offline tests, and a
  hard rollback if the tests fail.
- **F13 `hydra doctor --full`** — tmux/git/rg/jq/termux-api, disk, DNS/HTTPS,
  key status (never the key), Python, writable `$TMPDIR`, free model list.
- **F14 per-run web cache** — identical successful `web_fetch`/`web_search`
  calls reuse the first result.
- **T21 `--notify`** — forces finish notification and forwards through
  `--detach`; wake-lock/notification invocations are recorded in
  `~/.hydra/niceties.jsonl` and the run journal.

### Fixed

- Compaction now counts the pinned prefix (system + tools + env) and spaces
  compactions (`compact_min_gap`, ≥2 retired turns) — no thrash at 8k working
  context.
- Background jobs persist across crash/resume and are re-attached instead of
  re-run; killed foreground tools return an explicit `INTERRUPTED` result.
- `--lead` tries the requested model before fallbacks; a bad/paid lead is marked
  `missing`/`paid` and failover is visible.
- Reply-language prompt spells out Devanagari, English and Roman Hinglish, so
  models stop answering English/Devanagari tasks in Hinglish.
- `hooks.timeout` is configurable (default 30 s); hooks can never gate a turn.
- Tool robustness: bash timeouts kill process groups, background-job fd leak
  fixed, 200k-char output spill/paging, friendly binary `read_file` error,
  unicode journal appends, parallel append safety.
- Terminal safety: PTY quit/SIGTERM/SIGHUP/uncaught-exception restore, SIGWINCH,
  combining-mark wrapping for Devanagari/CJK, escape-free non-TTY output, and a
  documented `reset`/`stty sane` recovery after `kill -9`.
- `web_fetch` rejects non-http(s) URLs before any call; hosted 404s fall back to
  the raw fetcher and are reported as "page not found".
- `ask_models race(n)` degrades to `race-seq` when `max_inflight=1`.

- `hydra undo` restores the run baseline (turn 0); snapshots are taken
  before the first turn and on the finishing turn.
- Detached runs start in the caller's directory and forward
  HOME/PYTHONPATH/HYDRA_HOME even when the tmux server is stale.

### Tests and docs

- Unit suite: 237+ tests, `tools/scan_secrets.py` clean, stdlib-only audit.
- New `docs/REAL_API_NOTES.md` (dated real-API facts) and this changelog.

## v1.1.1 — baseline (commit 8bbca6a)

Full-screen TUI, readable research verification, web raw fallback, `hydra run
file.md`, Roman-Hinglish reply fix, and real scrollback.
