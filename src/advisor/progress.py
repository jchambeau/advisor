"""Progress display on stderr, so stdout keeps only the results.

On a terminal the line is rewritten in place (cut to the terminal width so it never wraps);
otherwise (systemd journal, pipe) a line is printed every `every` steps so the log stays short.
"""

from __future__ import annotations

import shutil
import sys
import time
from typing import TextIO

BAR_WIDTH = 20


class Progress:
    def __init__(self, label: str, total: int | None = None, every: int = 50, stream: TextIO | None = None):
        self.label = label
        self.total = total
        self.every = every
        self.stream = stream or sys.stderr
        self.interactive = self.stream.isatty()
        self.start = time.monotonic()
        self._first: tuple[float, int] | None = None  # time and count of the first update, for the ETA
        self._shown = False

    def _eta(self, done: int, total: int) -> str:
        now = time.monotonic()
        if self._first is None:
            self._first = (now, done)
            return ""
        t0, d0 = self._first
        if done <= d0 or done >= total:
            return ""
        # Measured from the first update, so one-off startup costs (imports, first request) are left out.
        remaining = (now - t0) / (done - d0) * (total - done)
        return f" ~{remaining:.0f}s left"

    def update(self, done: int, detail: str = "") -> None:
        total = self.total if self.total and done <= self.total else None  # an estimate that was exceeded
        if total:
            filled = BAR_WIDTH * done // total
            text = f"{self.label} [{'#' * filled}{'.' * (BAR_WIDTH - filled)}] {done}/{total}"
            text += self._eta(done, total)
        else:
            text = f"{self.label} {done}"
        if detail:
            text += f" - {detail}"
        if self.interactive:
            width = shutil.get_terminal_size().columns - 1
            # \x1b[K clears the rest of the line, including text another library printed there.
            self.stream.write(f"\r{text[:width]}\x1b[K")
            self.stream.flush()
            self._shown = True
        elif done == 1 or done % self.every == 0 or done == total:
            self.stream.write(text + "\n")
            self.stream.flush()

    def done(self, summary: str = "") -> None:
        """End the progress line; call it in a `finally` so an error never lands on the bar."""
        if self.interactive and self._shown:
            self.stream.write("\n")
        if summary:
            self.stream.write(f"{self.label}: {summary} in {time.monotonic() - self.start:.0f}s\n")
        self.stream.flush()
