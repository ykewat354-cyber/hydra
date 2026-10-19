# Changelog

All notable changes to HYDRA. Dates are local (Termux, Android).

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
