# HYDRA Work Order #5 — state

Baseline: v1.3.6 (`5f76e61`). Target: v1.4.0.
Branch: `wo5`.

## M0 — baseline
- [x] full suite verified: 448 tests, 0 failures
- [x] `hydra eval seed`: 10/10 passed, fake estimated cost $0.0139

## M1 — W5-1 OmniRoute Lite
- [x] config protocol + OpenAI key fallback
- [x] `OpenAiClient` SSE/tool-call transport, keep-alive, usage, errors
- [x] runtime/model/caps protocol selection + discovery fallback
- [x] `tests/fake_openai.py` + W5-1 unit/end-to-end tests
- [x] commit W5-1

## M2 — W5-2 code precision
- [x] `hydra diff` + TUI `/diff`
- [x] tolerant edit fallback + closest candidates
- [x] W5-2 tests
- [x] commit W5-2

## M3 — W5-3 release
- [x] footprint: 4,735 tokens (< 5,000)
- [ ] full suite re-run
- [ ] eval seed re-run
- [ ] black-box suite re-run
- [ ] CHANGELOG/README + version 1.4.0
- [ ] tag v1.4.0, merge main
- [ ] report + secret scan
