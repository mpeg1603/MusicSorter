"""Command line: `musicsorter plan`, `musicsorter apply`, `musicsorter undo`."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from . import __version__
from .audio import find_audio_files, scan
from .cache import Cache, default_cache_path
from .dedupe import find_duplicates
from .execute import apply_plan, undo_plan
from .lookup import Identifier
from .model import Item
from .planner import Settings, build_plan
from .report import write_plan
from .resolve import resolve
from .ui import Progress, say


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        say("\nInterrupted.")
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="musicsorter",
        description="Sort a music library into Artist/Album folders and move duplicate tracks aside.",
        epilog="Typical use: `plan` (changes nothing), read the plan, then `apply`. `undo` reverts an apply.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="scan the library and write a plan; changes nothing on disk")
    plan.add_argument("sources", nargs="+", help="folder(s) containing the music to sort")
    plan.add_argument("--dest", required=True,
                      help="top folder of the sorted library (may be the same as the source to sort in place)")
    plan.add_argument("--duplicates-dir",
                      help="where superseded copies go (default: a 'Duplicates' folder next to --dest)")
    plan.add_argument("--out", help="folder for the plan files (default: ./musicsorter-plan-<date-time>)")
    plan.add_argument("--copy", action="store_true",
                      help="copy files instead of moving them, leaving the originals untouched")
    plan.add_argument("--offline", action="store_true", help="never look anything up on the internet")
    plan.add_argument("--acoustid-key", default=os.environ.get("ACOUSTID_API_KEY", ""),
                      help="AcoustID API key for audio fingerprinting (or set ACOUSTID_API_KEY)")
    plan.add_argument("--duration-tolerance", type=float, default=3.0, metavar="SECONDS",
                      help="max length difference for two files to count as the same recording (default 3)")
    plan.add_argument("--compilations", choices=("various", "artist"), default="various",
                      help="file various-artists compilations under 'Various Artists' (default) "
                           "or under each track's own artist")
    plan.add_argument("--keep-filenames", action="store_true",
                      help="keep the original file names instead of 'NN - Title.ext'")
    plan.add_argument("--no-sidecars", action="store_true",
                      help="leave cover art, logs and cue sheets where they are")
    plan.add_argument("--no-content-check", action="store_true",
                      help="skip hashing same-size files to find byte-identical copies")
    plan.add_argument("--workers", type=int, default=8, help="parallel tag readers (default 8)")
    plan.add_argument("--no-cache", action="store_true", help=f"do not use the cache at {default_cache_path()}")
    plan.set_defaults(func=cmd_plan)

    apply = commands.add_parser("apply", help="carry out a plan")
    apply.add_argument("plan_dir")
    apply.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    apply.set_defaults(func=lambda a: apply_plan(a.plan_dir, a.yes))

    undo = commands.add_parser("undo", help="revert an applied plan using its journal")
    undo.add_argument("plan_dir")
    undo.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    undo.set_defaults(func=lambda a: undo_plan(a.plan_dir, a.yes))
    return parser


def _device(path: str) -> int:
    while not os.path.exists(path) and os.path.dirname(path) != path:
        path = os.path.dirname(path)
    return os.stat(path).st_dev


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip(os.sep) + os.sep)


def default_duplicates_dir(dest: str) -> str:
    """'Duplicates' next to the library, unless that would land on another disk (dest is a mount point)."""
    parent = os.path.dirname(dest.rstrip(os.sep)) or os.sep
    if os.path.ismount(dest) or _device(parent) != _device(dest):
        return os.path.join(dest, "Duplicates")
    return os.path.join(parent, "Duplicates")


def cmd_plan(args: argparse.Namespace) -> int:
    sources = [os.path.abspath(os.path.expanduser(s)) for s in args.sources]
    for source in sources:
        if not os.path.isdir(source):
            say(f"Not a folder: {source}")
            return 1
    dest = os.path.abspath(os.path.expanduser(args.dest))
    duplicates_dir = (os.path.abspath(os.path.expanduser(args.duplicates_dir)) if args.duplicates_dir
                      else default_duplicates_dir(dest))
    if _inside(dest, duplicates_dir):
        say("The library folder cannot be inside the duplicates folder.")
        return 1
    plan_dir = os.path.abspath(args.out or f"musicsorter-plan-{datetime.now():%Y%m%d-%H%M%S}")
    if os.path.exists(os.path.join(plan_dir, "plan.json")):
        say(f"{plan_dir} already contains a plan; choose another --out.")
        return 1

    settings = Settings(
        sources=sources, dest=dest, duplicates_dir=duplicates_dir, mode="copy" if args.copy else "move",
        keep_filenames=args.keep_filenames, compilations=args.compilations, sidecars=not args.no_sidecars,
    )
    notes = []
    cache = Cache(None if args.no_cache else default_cache_path())
    try:
        say(f"Looking for audio files in {', '.join(sources)} ...")
        files = find_audio_files(sources, exclude=[duplicates_dir])
        say(f"Found {len(files):,} audio files.")
        audio = scan(files, cache, args.workers)

        identifier = None
        if args.offline:
            notes.append("Ran with --offline: files with incomplete tags were not looked up online.")
        else:
            identifier = Identifier(cache, args.acoustid_key, say)
            if not identifier.acoustid:
                why = "fpcalc (chromaprint) is not installed" if args.acoustid_key else "no --acoustid-key given"
                notes.append(f"Audio fingerprinting was off ({why}); untagged files were matched by name only.")

        progress = Progress("Working out artist/album/title", len(audio))
        items = []
        for entry in audio:
            items.append(Item(entry, resolve(entry, identifier)))
            progress.step(note=f"{identifier.http.requests:,} web requests" if identifier else "")
        progress.finish(f"{identifier.http.requests:,} web requests" if identifier else "")
        if identifier and identifier.disabled:
            notes.append(f"These services failed and were skipped part-way: {', '.join(sorted(identifier.disabled))}. "
                         "Planning again later will retry only what is missing.")

        say("Looking for duplicates ...")
        duplicates = find_duplicates(items, args.duration_tolerance, compare_content=not args.no_content_check)
        plan, targets = build_plan(items, duplicates, settings)
    finally:
        cache.close()

    if any(_device(m["src"]) != _device(os.path.dirname(m["dst"])) for m in plan["moves"][:200]):
        notes.append("Some files go to a different disk; they will be copied and verified before the original "
                     "is removed, which is much slower than a move on the same disk.")
    summary = write_plan(plan_dir, plan, items, targets, notes)
    print()
    print(summary)
    print(f"Plan saved in {plan_dir}")
    print("  plan.txt                        summary, plus files worth checking")
    print("  moves.tsv                       every move: kind, from, to")
    if plan["duplicates"]:
        print("  duplicates_report.preview.txt   which file supersedes which")
    print()
    print(f"Nothing has been changed yet. To carry out the plan:  musicsorter apply {plan_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
