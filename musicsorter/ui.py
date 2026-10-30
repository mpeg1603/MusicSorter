"""Terminal helpers: progress lines, sizes and durations, confirmation prompts."""

from __future__ import annotations

import sys
import time


def say(message: str = "") -> None:
    print(message, file=sys.stderr, flush=True)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    if s >= 3600:
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        say("Not running in a terminal; pass --yes to confirm.")
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


class Progress:
    """One self-updating status line on a terminal; occasional plain lines when output is redirected."""

    def __init__(self, label: str, total: int):
        self.label, self.total, self.done = label, total, 0
        self.started = self.shown = time.monotonic()
        self.tty = sys.stderr.isatty()
        self.next_percent = 10

    def step(self, n: int = 1, note: str = "") -> None:
        self.done += n
        if self.tty:
            now = time.monotonic()
            if now - self.shown >= 0.25 or self.done >= self.total:
                self.shown = now
                sys.stderr.write("\r\033[K" + self._line(note))
                sys.stderr.flush()
        elif self.total and self.done * 100 >= self.next_percent * self.total:
            self.next_percent += 10
            say(self._line(note))

    def finish(self, note: str = "") -> None:
        if self.tty:
            sys.stderr.write("\r\033[K")
        took = fmt_duration(time.monotonic() - self.started)
        say(f"{self.label}: {self.done:,} done in {took}" + (f"  ({note})" if note else ""))

    def _line(self, note: str) -> str:
        percent = 100 * self.done / self.total if self.total else 100
        elapsed = time.monotonic() - self.started
        eta = ""
        if 0 < self.done < self.total and elapsed > 5:
            eta = f", ~{fmt_duration(elapsed * (self.total - self.done) / self.done)} left"
        return f"{self.label}: {self.done:,}/{self.total:,} ({percent:.0f}%{eta})" + (f"  {note}" if note else "")
