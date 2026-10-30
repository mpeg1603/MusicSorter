"""Carrying out a plan, and undoing it.

Safety rules:
  * never overwrite: a move whose target already exists is skipped and reported;
  * a file that changed after planning (size or modification time) is skipped;
  * every completed move is appended to journal.jsonl straight away, so `undo` works even after
    a crash or Ctrl+C, and running `apply` again simply continues where it stopped;
  * nothing is ever deleted. Moves across filesystems copy, check the size, then remove the original.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
from collections import Counter, defaultdict
from datetime import datetime

from .report import render_duplicates_report
from .ui import Progress, confirm, human_size, say

JOURNAL = "journal.jsonl"
REPORT_NAME = "duplicates_report.txt"
_OK = ("done", "already done")


def load_plan(plan_dir: str) -> dict:
    with open(os.path.join(plan_dir, "plan.json"), encoding="utf-8") as fh:
        return json.load(fh)


class Journal:
    def __init__(self, path: str):
        self.fh = open(path, "a", encoding="utf-8")
        self.unsynced = 0

    def log(self, **entry) -> None:
        self.fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.fh.flush()
        self.unsynced += 1
        if self.unsynced >= 100:
            os.fsync(self.fh.fileno())
            self.unsynced = 0

    def close(self) -> None:
        self.fh.flush()
        os.fsync(self.fh.fileno())
        self.fh.close()


def apply_plan(plan_dir: str, assume_yes: bool = False) -> int:
    plan = load_plan(plan_dir)
    settings, moves = plan["settings"], plan["moves"]
    mode = settings["mode"]
    if not moves:
        say("Nothing to do: every file is already in place.")
        return 0

    shortages = free_space_problems(moves, mode)
    if shortages:
        say("Not enough free space:")
        for line in shortages:
            say(f"  {line}")
        return 1

    verb = "Copy" if mode == "copy" else "Move"
    say(f"{verb} {len(moves):,} files ({human_size(sum(m['size'] for m in moves))})")
    say(f"  into   {settings['dest']}")
    say(f"  and    {settings['duplicates_dir']} (duplicates)")
    if not assume_yes and not confirm("Go ahead?"):
        say("Cancelled; nothing was changed.")
        return 1

    journal = Journal(os.path.join(plan_dir, JOURNAL))
    statuses: Counter = Counter()
    problems: dict[str, str] = {}
    emptied: set[str] = set()
    progress = Progress(f"{verb.rstrip('e')}ing files", len(moves))
    try:
        for move in moves:
            status = _apply_one(move, mode, journal)
            statuses[status] += 1
            if status not in _OK:
                problems[move["src"]] = status
            elif mode == "move":
                emptied.add(os.path.dirname(move["src"]))
            progress.step()
    except KeyboardInterrupt:
        journal.close()
        say("\nStopped. Run `apply` again to continue, or `undo` to put everything back.")
        return 130
    progress.finish()

    if plan["duplicates"]:
        _write_report(plan, problems, journal)
    if mode == "move":
        _remove_empty_dirs(emptied, settings["sources"], journal)
    journal.close()

    say(f"Done: {statuses['done']:,} files {'copied' if mode == 'copy' else 'moved'}"
        + (f", {statuses['already done']:,} were already done" if statuses["already done"] else ""))
    if problems:
        path = os.path.join(plan_dir, "apply_problems.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(f"{reason}\t{src}\n" for src, reason in problems.items())
        say(f"{len(problems):,} files were skipped; see {path}")
        for reason, count in Counter(problems.values()).most_common():
            say(f"  {count:>6,}  {reason}")
    if plan["duplicates"]:
        say(f"Duplicates report: {os.path.join(settings['duplicates_dir'], REPORT_NAME)}")
    say(f"To revert: musicsorter undo {plan_dir}")
    return 0 if not problems else 2


def _apply_one(move: dict, mode: str, journal: Journal) -> str:
    src, dst, size = move["src"], move["dst"], move["size"]
    src_exists = os.path.lexists(src)
    if os.path.lexists(dst):
        if os.path.getsize(dst) == size and (mode == "copy" or not src_exists):
            return "already done"
        if not (src_exists and os.path.samefile(src, dst)):  # a case-only rename is fine
            return "target already exists"
    if not src_exists:
        return "file no longer exists"
    st = os.stat(src)
    if st.st_size != size or st.st_mtime_ns != move["mtime_ns"]:
        return "file changed since planning"
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if mode == "copy":
            _copy(src, dst)
        else:
            _move(src, dst)
    except OSError as exc:
        return f"error: {exc.strerror or exc}"
    journal.log(op=mode, src=src, dst=dst, size=size)
    return "done"


def _move(src: str, dst: str) -> None:
    if os.path.lexists(dst):  # same file under a different case on a case-insensitive disk
        temporary = dst + ".musicsorter-tmp"
        os.rename(src, temporary)
        os.rename(temporary, dst)
        return
    try:
        os.rename(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        _copy(src, dst)  # different filesystem: copy, verify, then remove the original
        os.unlink(src)


def _copy(src: str, dst: str) -> None:
    partial = dst + ".part"
    try:
        shutil.copyfile(src, partial)
        with contextlib.suppress(OSError):  # FAT/exFAT/NTFS mounts may refuse permission bits
            shutil.copystat(src, partial)
        if os.path.getsize(partial) != os.path.getsize(src):
            raise OSError(errno.EIO, "size differs after copying")
        os.rename(partial, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(partial)
        raise


def free_space_problems(moves: list[dict], mode: str) -> list[str]:
    """Bytes that will really be written (copies, or moves to another disk) vs. free space there."""
    needed: dict[int, int] = defaultdict(int)
    where: dict[int, str] = {}
    for move in moves:
        if not os.path.lexists(move["src"]) or os.path.lexists(move["dst"]):
            continue
        target = _existing_ancestor(os.path.dirname(move["dst"]))
        device = os.stat(target).st_dev
        if mode == "copy" or os.stat(move["src"]).st_dev != device:
            needed[device] += move["size"]
            where.setdefault(device, target)
    problems = []
    for device, size in needed.items():
        free = shutil.disk_usage(where[device]).free
        if size + 256 * 2**20 > free:
            problems.append(f"{where[device]}: needs {human_size(size)}, has {human_size(free)} free")
    return problems


def _existing_ancestor(path: str) -> str:
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return path


def _write_report(plan: dict, problems: dict[str, str], journal: Journal) -> None:
    folder = plan["settings"]["duplicates_dir"]
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, REPORT_NAME)
    previous = os.path.getsize(path) if os.path.exists(path) else None
    with open(path, "a", encoding="utf-8") as fh:
        if previous:
            fh.write("\n\n" + "=" * 100 + "\n\n")
        fh.write(render_duplicates_report(plan, problems))
    journal.log(op="report", path=path, previous_size=previous)


def _remove_empty_dirs(dirs: set[str], roots: list[str], journal: Journal) -> None:
    """Remove folders the moves left empty, walking upwards but never removing a source folder itself."""
    roots = [os.path.abspath(r) for r in roots]
    for directory in sorted(dirs, key=len, reverse=True):
        while directory not in roots and any(directory.startswith(root + os.sep) for root in roots):
            try:
                os.rmdir(directory)
            except OSError:
                break
            journal.log(op="rmdir", path=directory)
            directory = os.path.dirname(directory)


def undo_plan(plan_dir: str, assume_yes: bool = False) -> int:
    path = os.path.join(plan_dir, JOURNAL)
    entries = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                with contextlib.suppress(ValueError):
                    entries.append(json.loads(line))
    files = [e for e in entries if e.get("op") in ("move", "copy")]
    if not files:
        say("Nothing to undo.")
        return 0
    plan = load_plan(plan_dir)
    action = "put back" if files[0]["op"] == "move" else "delete the copies of"
    say(f"This will {action} {len(files):,} files.")
    if not assume_yes and not confirm("Go ahead?"):
        say("Cancelled; nothing was changed.")
        return 1

    problems = []
    touched: set[str] = set()
    progress = Progress("Undoing", len(entries))
    for entry in reversed(entries):
        op = entry.get("op")
        try:
            if op == "rmdir":
                os.makedirs(entry["path"], exist_ok=True)
            elif op == "report":
                if entry["previous_size"] is None:
                    os.unlink(entry["path"])
                    touched.add(os.path.dirname(entry["path"]))
                else:
                    os.truncate(entry["path"], entry["previous_size"])
            elif op == "move":
                src, dst = entry["src"], entry["dst"]
                if not os.path.lexists(dst):
                    problems.append(f"missing, cannot put back: {dst}")
                elif os.path.lexists(src) and not os.path.samefile(src, dst):
                    problems.append(f"original location is occupied: {src}")
                else:
                    os.makedirs(os.path.dirname(src), exist_ok=True)
                    _move(dst, src)
                    touched.add(os.path.dirname(dst))
            elif op == "copy":
                dst = entry["dst"]
                if os.path.lexists(dst) and os.path.getsize(dst) == entry["size"]:
                    os.unlink(dst)
                    touched.add(os.path.dirname(dst))
        except OSError as exc:
            problems.append(f"{exc.strerror or exc}: {entry}")
        progress.step()
    progress.finish()

    settings = plan["settings"]
    _remove_empty_dirs(touched, [settings["dest"], settings["duplicates_dir"]], _NullJournal())
    for root in (settings["duplicates_dir"], settings["dest"]):
        if root not in settings["sources"]:
            with contextlib.suppress(OSError):
                os.rmdir(root)  # only succeeds if it is empty
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    os.rename(path, os.path.join(plan_dir, f"journal.undone-{stamp}.jsonl"))
    if problems:
        say(f"{len(problems):,} entries could not be undone:")
        for line in problems[:50]:
            say(f"  {line}")
        return 2
    say("Undo complete.")
    return 0


class _NullJournal:
    def log(self, **entry) -> None:
        pass
