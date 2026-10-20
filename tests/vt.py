"""Tiny VT emulator for the pty tests (WO2 M2).

Enough of a terminal to verify what the inline renderer actually puts on the
screen: printing, CR/LF/BS, CSI ``A/B/C/D/H/G/K/J`` and ``m``, private modes
(``?…h/l``) and OSC strings. Wide characters take two cells; combining marks
attach to the previous cell.

    vt = VT(54, 31)
    vt.feed(raw_bytes_from_the_pty)
    vt.text()          # visible screen
    vt.scrollback      # lines that scrolled off the top
    vt.modes           # enabled private modes, e.g. {2004, 1007}
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hydra import ui_format as U  # noqa: E402


class VT:
    def __init__(self, cols: int = 80, rows: int = 24):
        self.cols, self.rows = int(cols), int(rows)
        self.reset()

    # ------------------------------------------------------------- lifecycle
    def reset(self):
        self.grid = [[" "] * self.cols for _ in range(self.rows)]
        self.row = self.col = 0
        self.modes: set = set()
        self.sgr = ""
        self.scrollback: list = []
        self._buf = ""
        self._osc = False

    def resize(self, cols: int, rows: int):
        self.cols, self.rows = int(cols), int(rows)
        self.reset()

    # ------------------------------------------------------------------ feed
    def feed(self, data):
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8", "replace")
        text = self._buf + data
        self._buf = ""
        i = 0
        while i < len(text):
            ch = text[i]
            if self._osc:
                if ch == "\x07":
                    self._osc = False
                elif ch == "\x1b" and text[i + 1:i + 2] == "\\":
                    self._osc = False
                    i += 1
                i += 1
                continue
            if ch == "\x1b":
                if i + 1 >= len(text):
                    self._buf = text[i:]
                    break
                nxt = text[i + 1]
                if nxt == "[":
                    j = i + 2
                    while j < len(text) and not ("\x40" <= text[j] <= "\x7e"):
                        j += 1
                    if j >= len(text):
                        self._buf = text[i:]        # incomplete CSI
                        break
                    self._csi(text[i + 2:j], text[j])
                    i = j + 1
                    continue
                if nxt == "]":
                    self._osc = True
                    i += 2
                    continue
                i += 2                              # two-byte escape
                continue
            if ch == "\r":
                self.col = 0
            elif ch == "\n":
                self._down(1, scroll=True)
                self.col = 0
            elif ch == "\b":
                self.col = max(0, self.col - 1)
            elif ch == "\t":
                self.col = min(self.cols - 1, (self.col // 8 + 1) * 8)
            else:
                self._put(ch)
            i += 1

    # ----------------------------------------------------------------- paint
    def _put(self, ch):
        w = U.char_width(ch)
        if w == 0:
            if self.col:
                cell = self.grid[self.row][self.col - 1]
                self.grid[self.row][self.col - 1] = (
                    cell + ch if cell != " " else ch)
            return
        if self.col + w > self.cols:
            self.col = 0
            self._down(1)
        self.grid[self.row][self.col] = ch
        if w == 2 and self.col + 1 < self.cols:
            self.grid[self.row][self.col + 1] = ""
        self.col = min(self.cols - 1, self.col + w)

    def _down(self, n: int = 1, scroll: bool = False):
        for _ in range(n):
            if self.row + 1 >= self.rows:
                if not scroll:
                    break                      # CSI B clamps at the bottom
                self.scrollback.append(self._line(self.grid.pop(0)))
                self.grid.append([" "] * self.cols)
                continue
            self.row += 1

    def _up(self, n: int = 1):
        self.row = max(0, self.row - n)

    def _csi(self, params: str, final: str):
        if params.startswith("?"):
            nums = [int(p) for p in params[1:].split(";") if p.isdigit()]
            if final == "h":
                self.modes.update(nums)
            elif final == "l":
                self.modes.difference_update(nums)
            return
        nums = []
        for p in params.split(";"):
            try:
                nums.append(int(p) if p else 0)
            except ValueError:
                nums.append(0)

        def num(idx=0, default=0):
            v = nums[idx] if idx < len(nums) else 0
            return v or default

        if final == "A":
            self._up(num(0, 1))
        elif final == "B":
            self._down(num(0, 1))
        elif final == "C":
            self.col = min(self.cols - 1, self.col + num(0, 1))
        elif final == "D":
            self.col = max(0, self.col - num(0, 1))
        elif final in ("H", "f"):
            self.row = min(self.rows - 1, max(0, num(0, 1) - 1))
            self.col = min(self.cols - 1, max(0, num(1, 1) - 1))
        elif final == "G":
            self.col = min(self.cols - 1, max(0, num(0, 1) - 1))
        elif final == "K":
            m = num(0, 0)
            if m == 0:
                for c in range(self.col, self.cols):
                    self.grid[self.row][c] = " "
            elif m == 1:
                for c in range(0, self.col + 1):
                    self.grid[self.row][c] = " "
            else:
                self.grid[self.row] = [" "] * self.cols
        elif final == "J":
            m = num(0, 0)
            if m == 0:
                for c in range(self.col, self.cols):
                    self.grid[self.row][c] = " "
                for r in range(self.row + 1, self.rows):
                    self.grid[r] = [" "] * self.cols
            elif m == 1:
                for c in range(0, self.col + 1):
                    self.grid[self.row][c] = " "
                for r in range(0, self.row):
                    self.grid[r] = [" "] * self.cols
            else:
                self.grid = [[" "] * self.cols for _ in range(self.rows)]
        elif final == "m":
            self.sgr = "" if (not nums or nums == [0]) else params
        # anything else: ignored on purpose

    # ---------------------------------------------------------------- output
    @staticmethod
    def _line(cells) -> str:
        return "".join(cells).rstrip()

    def lines(self) -> list:
        return [self._line(row) for row in self.grid]

    def text(self) -> str:
        return "\n".join(self.lines())

    def scrollback_text(self) -> str:
        return "\n".join(self.scrollback)

    def all_text(self) -> str:
        return "\n".join(self.scrollback + self.lines())

    def count_screen(self, needle: str) -> int:
        return sum(line.count(needle) for line in self.lines())
