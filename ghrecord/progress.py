"""Lightweight progress reporting to stderr.

By default (interactive terminal, not quiet, not verbose) the pipeline shows
phase lines plus an in-place "scanning N/M" counter so the user can see it is
working. Verbose mode uses detailed per-repo logging instead, and quiet or
non-TTY runs stay silent (so piped stderr stays clean).
"""

import sys
from typing import TextIO


class Progress:
    def __init__(self, enabled: bool, stream: TextIO | None = None) -> None:
        self.enabled = enabled
        self.stream = stream if stream is not None else sys.stderr
        self._pending = 0  # length of the current in-place line, if any

    def phase(self, msg: str) -> None:
        """A milestone line that stays on screen."""
        if not self.enabled:
            return
        self._clear()
        self.stream.write(f"[ghrecord] {msg}\n")
        self.stream.flush()

    def status(self, msg: str) -> None:
        """A transient line, overwritten in place by the next status/phase."""
        if not self.enabled:
            return
        line = f"[ghrecord] {msg}"
        pad = max(0, self._pending - len(line))
        self.stream.write("\r" + line + " " * pad)
        self.stream.flush()
        self._pending = len(line)

    def done(self) -> None:
        """Finish any in-place line so later output starts on a fresh row."""
        if self.enabled and self._pending:
            self.stream.write("\n")
            self.stream.flush()
            self._pending = 0

    def _clear(self) -> None:
        if self._pending:
            self.stream.write("\r" + " " * self._pending + "\r")
            self.stream.flush()
            self._pending = 0
