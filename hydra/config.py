"""HYDRA configuration: defaults, layered TOML loading, paths, API key resolution.

Layers (later wins):
    built-in defaults  ->  ~/.hydra/roles.auto.toml  ->  ~/.hydra/config.toml
    ->  <project>/.hydra/config.toml  ->  --set flags

Only ``config.py`` (defaults) and ``models.json`` ever mention model names;
the rest of the code treats them as configuration data.
"""
from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    "api": {
        "host": "https://ollama.com",
        "key_env": "OLLAMA_API_KEY",
        "key_file": "~/.hydra/key",
        "plan": "free",               # free | pro | max
        "max_inflight": 0,            # 0 = derive from plan
        "connect_timeout": 15,
        "idle_timeout": 90,           # seconds without a streamed byte = stalled
        "retries": 5,
        "debug_http": False,          # --debug-http / HYDRA_DEBUG_HTTP=1
    },
    "loop": {
        "max_turns": 0,               # 0 = unlimited
        "working_ctx": 48000,
        "compact_at": 0.75,
        "compact_to": 0.35,
        "compact_keep": 6,
        "compact_min_gap": 3,
        "tool_cap": 6000,
        "tool_timeout": 120,
        "parallel_tools": 4,
        "stall_repeat": 3,
        "verify": "auto",             # auto | always | never
        "verify_rounds": 3,
    },
    "agents": {
        "max_depth": 4,               # 0 = unlimited
        "max_agents": 48,             # 0 = unlimited
        "report_cap": 1600,
    },
    "routing": {
        "default": "fallback",
        "json_policy": "cheapest_conformant",  # or "role_chain" (F1)
        "breaker_fails": 3,
        "breaker_cooldown": 300,
    },
    "roles": {
        "lead":       {"chain": ["gpt-oss:120b", "nemotron-3-ultra", "gemma4:31b"], "think": "medium", "temperature": 0.3},
        "coder":      {"chain": ["gpt-oss:120b", "nemotron-3-ultra", "gemma4:31b"], "think": "medium"},
        "worker":     {"chain": ["gpt-oss:20b", "gemma4:31b", "nemotron-3-nano"], "think": "low"},
        "researcher": {"chain": ["nemotron-3-super", "gemma4:31b", "gpt-oss:20b"], "think": "low"},
        "critic":     {"chain": ["nemotron-3-ultra", "gemma4:31b", "gpt-oss:120b"], "think": "medium"},
        "reviewer":   {"chain": ["nemotron-3-ultra", "gemma4:31b", "gpt-oss:120b"], "think": "medium"},
        "summarizer": {"chain": ["nemotron-3-super", "gemma4:31b", "gpt-oss:20b"], "think": "off"},
        "vision":     {"chain": ["gemma4:31b"], "think": "low"},
        "planner":    {"chain": ["gpt-oss:120b", "nemotron-3-ultra", "gemma4:31b"], "think": "medium"},
        "debugger":   {"chain": ["gpt-oss:120b", "nemotron-3-ultra", "gemma4:31b"], "think": "medium"},
        "tester":     {"chain": ["gpt-oss:20b", "gemma4:31b", "nemotron-3-nano"], "think": "low"},
        "scribe":     {"chain": ["nemotron-3-super", "gemma4:31b", "gpt-oss:20b"], "think": "low"},
    },
    "journal": {"fsync": "turn", "git_snapshots": False},
    "models": {
        "probe_candidates": [],   # empty -> CANDIDATE_MODELS (the pricing table)
        "reprobe_days": 7,
    },
    "termux": {"wake_lock": True, "notify_on_finish": True, "notify_on_park": True},
    "daemon": {"poll_secs": 5},
    "park": {"wait_reset": False, "poll_interval": 1800},
    "project": {
        "instruction_files": ["HYDRA.md", "AGENTS.md"],
        "max_chars": 8000,
        "subagent_chars": 2000,
    },
    "autoverify": {
        "enabled": True,
        "timeout": 60,
        "globs": {
            "*.py": "python -m py_compile {file}",
            "*.js": "node --check {file}",
            "*.sh": "bash -n {file}",
        },
    },
    "hooks": {
        "pre_tool": "", "post_tool": "", "post_edit": "",
        "agent_done": "", "finish": "", "park": "", "error": "",
    },
    "mcp": {"servers": {}},
    "hygiene": {
        "untrusted_tagging": True,
        "redact": True,
        "untrusted_paths": ["~/storage/downloads/**"],
        "redact_env": ["OLLAMA_API_KEY"],
    },
}

PLAN_CAPACITY = {"free": 1, "pro": 3, "max": 10}

DEFAULT_PRICES: dict = {
    "gpt-oss:120b":       {"in": 0.15,  "cached": 0.014,  "out": 0.60},
    "gpt-oss:20b":        {"in": 0.07,  "cached": 0.007,  "out": 0.30},
    "gemma4:31b":         {"in": 0.14,  "cached": 0.014,  "out": 0.40},
    "nemotron-3-ultra":   {"in": 0.10,  "cached": 0.010,  "out": 3.00},
    "nemotron-3-super":   {"in": 0.015, "cached": 0.0015, "out": 0.60},
    "nemotron-3-nano":    {"in": 0.06,  "cached": 0.006,  "out": 0.24},
    "glm-5.3-flash":      {"in": 0.15,  "cached": 0.015,  "out": 0.50},
    "deepseek-v4.1-flash": {"in": 0.30, "cached": 0.030,  "out": 1.20},
    "minimax-m3":         {"in": 0.60,  "cached": 0.060,  "out": 2.40},
}

CANDIDATE_MODELS: list = list(DEFAULT_PRICES)

CONFIG_TEMPLATE = """# HYDRA config (all keys optional; these are the built-in defaults).
# Layers: defaults -> ~/.hydra/roles.auto.toml -> this file
#         -> <project>/.hydra/config.toml -> --set key=value

[api]
host            = "https://ollama.com"
key_env         = "OLLAMA_API_KEY"
key_file        = "~/.hydra/key"
plan            = "free"          # free | pro | max  -> slots 1 | 3 | 10
max_inflight    = 0               # 0 = derive from plan
connect_timeout = 15
idle_timeout    = 90
retries         = 5
debug_http      = false           # --debug-http / HYDRA_DEBUG_HTTP=1 -> runs/<id>/http.jsonl

[loop]
max_turns       = 0               # 0 = unlimited
working_ctx     = 48000
compact_at      = 0.75
compact_to      = 0.35
compact_keep    = 6               # recent turns kept verbatim after a compaction
compact_min_gap = 3               # minimum turns between two compactions
tool_cap        = 6000
tool_timeout    = 120
parallel_tools  = 4
stall_repeat    = 3
verify          = "auto"          # auto | always | never
verify_rounds   = 3

[agents]
max_depth       = 4               # 0 = unlimited
max_agents      = 48              # 0 = unlimited
report_cap      = 1600

[routing]
default           = "fallback"
json_policy       = "cheapest_conformant"   # or "role_chain" (F1 structured calls)
breaker_fails     = 3
breaker_cooldown  = 300

[journal]
fsync         = "turn"            # event | turn | never
git_snapshots = false

[termux]
wake_lock        = true
notify_on_finish = true
notify_on_park   = true

[park]
wait_reset    = false
poll_interval = 1800

[project]
instruction_files = ["HYDRA.md", "AGENTS.md"]
max_chars         = 8000
subagent_chars    = 2000

[autoverify]
enabled = true
timeout = 60
[autoverify.globs]
"*.py" = "python -m py_compile {file}"
"*.js" = "node --check {file}"
"*.sh" = "bash -n {file}"

[hooks]
pre_tool  = ""
post_tool = ""
post_edit = ""
agent_done = ""
finish    = ""
park      = ""
error     = ""

[hygiene]
untrusted_tagging = true
redact            = true
untrusted_paths   = ["~/storage/downloads/**"]
redact_env        = ["OLLAMA_API_KEY"]

# MCP tools (optional, §19.5): stdio JSON-RPC servers. Their tools appear as
# mcp__<server>__<tool>, visible to roles with mcp = true (default: lead,
# coder, researcher). Schemas are cached in ~/.hydra/mcp_cache/.
# [mcp.servers.example]
# command = ["npx", "-y", "some-mcp-server"]
# env     = {}
# eager   = false

# Role chains/think levels are also configurable here, e.g.:
# [roles.lead]
# chain = ["gpt-oss:120b", "nemotron-3-ultra", "gemma4:31b"]
# think = "medium"
# temperature = 0.3
"""


def _prices_template() -> str:
    lines = ["# USD per 1M tokens (editable; informational only — copy from ollama.com/pricing).", ""]
    for name, p in DEFAULT_PRICES.items():
        lines += [f'["{name}"]', f"in = {p['in']}", f"cached = {p['cached']}", f"out = {p['out']}", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------- paths

def home() -> Path:
    return Path(os.environ.get("HYDRA_HOME") or "~/.hydra").expanduser()


def run_dir(run_id: str) -> Path:
    return home() / "runs" / run_id


def expand(value: str) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser()


def shell_path() -> str:
    """Termux rule (§11): never hard-code /bin/sh — use $PREFIX/bin/bash."""
    import shutil
    cand = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr")) / "bin" / "bash"
    if cand.exists():
        return str(cand)
    return shutil.which("bash") or "sh"


# ---------------------------------------------------------------- dict helpers

def deep_merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def set_path(d: dict, dotted: str, value) -> None:
    keys = dotted.split(".")
    cur = d
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[keys[-1]] = value


def get(cfg: dict, dotted: str, default=None):
    cur = cfg
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


# ---------------------------------------------------------------- loading

def _parse_value(raw: str):
    try:
        return tomllib.loads("v = " + raw)["v"]
    except Exception:
        return raw


def parse_overrides(items) -> list:
    out = []
    for it in items or []:
        if isinstance(it, (tuple, list)) and len(it) == 2:
            out.append((str(it[0]), it[1]))
            continue
        k, sep, v = str(it).partition("=")
        if not sep:
            raise ValueError(f"--set needs key=value, got: {it!r}")
        out.append((k.strip(), _parse_value(v.strip())))
    return out


def load_config(overrides=None, project_dir=None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    pauto = home() / "roles.auto.toml"
    if pauto.is_file():
        try:
            deep_merge(cfg, tomllib.loads(pauto.read_text(encoding="utf-8")))
        except Exception:
            pass  # written by `hydra models tune --apply`; never brick startup
    gpath = home() / "config.toml"
    if gpath.is_file():
        try:
            deep_merge(cfg, tomllib.loads(gpath.read_text(encoding="utf-8")))
        except Exception:
            pass  # a broken global config must never brick the harness
    ppin = home() / "pinned.toml"
    if ppin.is_file():
        try:
            deep_merge(cfg, tomllib.loads(ppin.read_text(encoding="utf-8")))
        except Exception:
            pass  # written by `hydra models pin`; must never brick startup
    pdir = Path(project_dir) if project_dir is not None else Path.cwd()
    ppath = pdir / ".hydra" / "config.toml"
    if ppath.is_file():
        try:
            deep_merge(cfg, tomllib.loads(ppath.read_text(encoding="utf-8")))
        except Exception:
            pass
    for k, v in parse_overrides(overrides):
        set_path(cfg, k, v)
    return cfg


def load_prices(cfg=None) -> dict:
    prices = copy.deepcopy(DEFAULT_PRICES)
    pp = home() / "prices.toml"
    if pp.is_file():
        try:
            deep_merge(prices, tomllib.loads(pp.read_text(encoding="utf-8")))
        except Exception:
            pass
    return prices


def est_usd(prices: dict, model: str, tin: int = 0, tout: int = 0) -> float:
    """HYDRA's only price helper, shared by the router ledger and T07/F1."""
    p = (prices or {}).get(model) or {}
    return (int(tin) * float(p.get("in", 0)) +
            int(tout) * float(p.get("out", 0))) / 1e6


def ensure_home(cfg=None) -> None:
    h = home()
    for sub in ("memory", "tools", "roles", "skills", "commands",
                "mcp_cache", "queue", "queue/done", "runs"):
        (h / sub).mkdir(parents=True, exist_ok=True)
    cfgp = h / "config.toml"
    if not cfgp.exists():
        cfgp.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    pr = h / "prices.toml"
    if not pr.exists():
        pr.write_text(_prices_template(), encoding="utf-8")


def max_inflight(cfg: dict) -> int:
    n = int(get(cfg, "api.max_inflight", 0) or 0)
    if n > 0:
        return n
    return PLAN_CAPACITY.get(str(get(cfg, "api.plan", "free")).lower(), 1)


def load_key(cfg: dict):
    env_name = get(cfg, "api.key_env", "OLLAMA_API_KEY")
    if env_name:
        val = os.environ.get(env_name)
        if val:
            return val.strip()
    raw = str(get(cfg, "api.key_file", "~/.hydra/key"))
    if raw.startswith("~/.hydra"):
        # keep the key file inside $HYDRA_HOME when it is overridden
        kp = home() / raw[len("~/.hydra/"):]
    else:
        kp = expand(raw)
    if kp.is_file():
        txt = kp.read_text(encoding="utf-8").strip()
        if txt:
            return txt
    return None
