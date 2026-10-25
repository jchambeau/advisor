"""Progress display on stderr, so stdout keeps only the results.

On a terminal the line is rewritten in place; otherwise (systemd journal, pipe) a line is
printed every `every` steps so the log stays short.
"""

from __future__ import annotations

import sys
import time
from typing import TextIO


class Progress:
    def __init__(self, label: str, total: int | None = None, every: int = 50, stream: TextIO | None = None):
        self.label = label
        self.total = total
        self.every = every
        self.stream = stream or sys.stderr
        self.interactive = self.stream.isatty()
        self.start = time.monotonic()
        self._last = ""

    def update(self, done: int, detail: str = "") -> None:
        elapsed = time.monotonic() - self.start
        if self.total:
            bar_width = 20
            filled = int(bar_width * done / self.total)
            text = f"{self.label} [{'#' * filled}{'.' * (bar_width - filled)}] {done}/{self.total}"
            if 0 < done < self.total:
                remaining = elapsed / done * (self.total - done)
                text += f" ~{remaining:.0f}s left"
        else:
            text = f"{self.label} {done}"
        if detail:
            text += f" - {detail}"
        if self.interactive:
            padding = " " * max(0, len(self._last) - len(text))
            self.stream.write(f"\r{text}{padding}")
            self.stream.flush()
            self._last = text
        elif done == 1 or done % self.every == 0 or done == self.total:
            self.stream.write(text + "\n")
            self.stream.flush()

    def done(self, summary: str = "") -> None:
        elapsed = time.monotonic() - self.start
        if self.interactive and self._last:
            self.stream.write("\n")
        if summary:
            self.stream.write(f"{self.label} {summary} ({elapsed:.0f}s)\n")
        self.stream.flush()
