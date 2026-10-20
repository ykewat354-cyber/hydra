"""Inline terminal rendering helpers (WO2 M2).

Fullscreen mode owns the alternate screen. Inline mode leaves the transcript in
the terminal's real scrollback — so native swipe-scroll and long-press copy
keep working — and only repaints a *live region* at the bottom: the tail of the
transcript that is still changing, the palette, the status line, the input box
and the footer.

Everything here is pure so the tests can drive it without a terminal.

Row model
---------
``split_blocks`` walks the rendered ``[(block, rows)]`` list and commits rows
as a *prefix*: the first block that is still mutable stops the commit pointer,
so the transcript never prints out of order. A streaming (non-final) assistant
answer commits every complete row except its last, unfinished line.
``paint`` returns the escape bytes that replace the previous live region with
the new rows, plus the bookkeeping needed for the next frame.
"""
from __future__ import annotations


def is_live_block(b) -> bool:
    """True while a block's rendering can still change (must be repainted)."""
    kind = getattr(b, "kind", "") or ""
    meta = getattr(b, "meta", None) or {}
    if kind == "welcome":
        # hidden tips shrink the block after the first user message
        return bool(meta.get("tips", True))
    if kind == "assistant":
        return not bool(getattr(b, "final", True))
    if kind == "tool":
        return meta.get("ok") is None
    if kind == "spawn":
        return str(meta.get("state") or "running") == "running"
    return False


def split_blocks(block_rows: list) -> tuple:
    """Split ``[(block, rows)]`` into ``(committed_rows, live_rows)``."""
    committed: list = []
    live: list = []
    stopped = False
    for b, rows in block_rows:
        if not rows:
            continue
        if stopped:
            live.extend(rows)
            continue
        if is_live_block(b):
            if (getattr(b, "kind", "") == "assistant" and len(rows) > 1):
                committed.extend(rows[:-1])
                live.extend(rows[-1:])
            else:
                live.extend(rows)
            stopped = True
            continue
        committed.extend(rows)
    return committed, live


def paint(rendered: list, prev_h: int, below: int, cursor, width: int) -> tuple:
    """Bytes that repaint the live region in place.

    ``rendered`` is a list of already-styled strings. The previous frame left
    the cursor ``below`` rows above the region bottom, so step down first, then
    up to the region top. Returns ``(chunks, drawn, new_below)`` where ``drawn``
    is the region height after this frame.
    """
    drawn = max(len(rendered), int(prev_h or 0))
    chunks: list = []
    if prev_h:
        if below:
            chunks.append(f"\x1b[{int(below)}B")
        if int(prev_h) > 1:
            # the region has `prev_h` rows: its top is prev_h-1 above the
            # last drawn row where the previous frame left the cursor
            chunks.append(f"\x1b[{int(prev_h) - 1}A")
    for i in range(drawn):
        chunks.append("\r\x1b[2K")
        if i < len(rendered):
            chunks.append(rendered[i])
        if i < drawn - 1:
            chunks.append("\r\n")
    chunks.append("\x1b[?25l")
    new_below = 0
    if cursor is not None:
        row, col = cursor
        new_below = max(0, (drawn - 1) - int(row))
        if new_below:
            chunks.append(f"\x1b[{new_below}A")
        chunks.append("\r")
        if col:
            chunks.append(f"\x1b[{int(col)}C")
        chunks.append("\x1b[?25h")
    return chunks, drawn, new_below


def unprint(prev_h: int, below: int) -> list:
    """Bytes that clear the live region (leave the cursor at its top)."""
    chunks: list = []
    if not prev_h:
        return chunks
    if below:
        chunks.append(f"\x1b[{int(below)}B")
    if int(prev_h) > 1:
        chunks.append(f"\x1b[{int(prev_h) - 1}A")
    chunks.append("\x1b[J")
    return chunks
