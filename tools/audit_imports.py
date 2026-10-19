#!/usr/bin/env python3
"""Import auditor (work order T03a): HYDRA must stay stdlib-only (R2).

Parses every ``*.py`` under a root (default ``hydra/``) with ``ast`` and fails
on any absolute import whose top-level module is neither in
``sys.stdlib_module_names`` nor the ``hydra`` package itself. Relative imports
(``from . import x``) are always fine. Prints one ``path:line: module`` line per
violation — never the matched text beyond the module name. Exit 0 = clean.

Usage:
    python tools/audit_imports.py [ROOT]      # ROOT defaults to hydra/
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = REPO_ROOT / "hydra"
# This repo's own packages: not third-party dependencies. ``tests`` is imported
# lazily by hydra/evals.py for the fake server and is itself stdlib-only.
SELF = {"hydra", "tests"}


def imported_roots(path: Path) -> list[tuple[str, int]]:
    """Top-level names of every absolute import in ``path`` (lineno kept)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(alias.name.split(".", 1)[0], node.lineno)
                    for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: from . import x / from .pkg import x
                continue
            if node.module:
                out.append((node.module.split(".", 1)[0], node.lineno))
    return out


def audit(root: Path) -> list[str]:
    """All third-party import violations found under ``root``."""
    problems: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            imports = imported_roots(path)
        except SyntaxError as exc:
            problems.append(f"{path}:{exc.lineno}: syntax error: {exc.msg}")
            continue
        for name, lineno in imports:
            if name in sys.stdlib_module_names or name in SELF:
                continue
            problems.append(f"{path}:{lineno}: third-party import '{name}'")
    return problems


def main(argv: list[str]) -> int:
    root = Path(argv[1]).resolve() if len(argv) > 1 else DEFAULT_ROOT
    if not root.is_dir():
        print(f"audit_imports: not a directory: {root}", file=sys.stderr)
        return 2
    problems = audit(root)
    for line in problems:
        print(line)
    rel = root.relative_to(REPO_ROOT) if root.is_relative_to(REPO_ROOT) else root
    if problems:
        print(f"audit_imports: FAIL — {len(problems)} third-party import(s) "
              f"under {rel}")
        return 1
    n = sum(1 for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    print(f"audit_imports: OK — {n} files under {rel} use stdlib imports only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
