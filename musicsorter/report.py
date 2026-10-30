"""Human-readable output: the plan summary, the full move list and the duplicates report."""

from __future__ import annotations

import json
import os
from collections import Counter

from .model import Item
from .planner import SINGLES, UNKNOWN_ARTIST
from .ui import fmt_duration, human_size

WEB_SOURCES = ("musicbrainz", "deezer", "acoustid")
NAME_SOURCES = ("filename", "folder")


def write_plan(plan_dir: str, plan: dict, items: list[Item], targets: dict[Item, str], notes: list[str]) -> str:
    """Write plan.json, moves.tsv, plan.txt and a preview of the duplicates report. Returns the summary."""
    os.makedirs(plan_dir, exist_ok=True)
    with open(os.path.join(plan_dir, "plan.json"), "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(plan_dir, "moves.tsv"), "w", encoding="utf-8") as fh:
        fh.write("kind\tfrom\tto\n")
        for move in plan["moves"]:
            fh.write(f"{move['kind']}\t{move['src']}\t{move['dst']}\n")
    if plan["duplicates"]:
        with open(os.path.join(plan_dir, "duplicates_report.preview.txt"), "w", encoding="utf-8") as fh:
            fh.write(render_duplicates_report(plan))
    summary = render_summary(plan, items, targets, notes)
    with open(os.path.join(plan_dir, "plan.txt"), "w", encoding="utf-8") as fh:
        fh.write(summary)
        fh.write(_details(items, targets))
    return summary


def render_summary(plan: dict, items: list[Item], targets: dict[Item, str], notes: list[str]) -> str:
    settings = plan["settings"]
    moves = plan["moves"]
    kinds = Counter(m["kind"] for m in moves)
    kept = [it for it in items if not it.duplicate_of]
    dupes = [it for it in items if it.duplicate_of]

    origin = Counter()
    for it in items:
        used = set(it.meta.sources.values())
        if not it.meta.artist:
            origin["artist still unknown"] += 1
        elif used & set(WEB_SOURCES):
            origin["completed from the web"] += 1
        elif used & set(NAME_SOURCES):
            origin["completed from file/folder names"] += 1
        else:
            origin["from tags alone"] += 1

    library_root = settings["dest"]
    artists, albums, singles = set(), set(), 0
    for it in kept:
        rel = os.path.relpath(targets[it], library_root).split(os.sep)
        if len(rel) >= 3:
            artists.add(rel[0])
            if rel[1] == SINGLES:
                singles += 1
            else:
                albums.add((rel[0], rel[1]))

    verb = "copied" if settings["mode"] == "copy" else "moved"
    lines = [
        f"MusicSorter plan, created {plan['created']}",
        "",
        f"Sources:              {', '.join(settings['sources'])}",
        f"Sorted library:       {settings['dest']}",
        f"Duplicates folder:    {settings['duplicates_dir']}",
        f"Mode:                 {settings['mode']} (files are {verb}; nothing is deleted or re-encoded)",
        "",
        f"Audio files found:    {len(items):,} ({human_size(sum(it.audio.size for it in items))})",
        f"  unreadable:         {sum(1 for it in items if it.audio.error):,}",
        "Where the artist/album/title came from:",
        *(f"  {label + ':':<34}{origin[label]:,}" for label in (
            "from tags alone", "completed from file/folder names", "completed from the web", "artist still unknown")),
        "",
        f"Library after sorting: {len(artists):,} artists, {len(albums):,} albums, {singles:,} tracks in Singles",
        f"Duplicates:           songs with extra copies: {len(plan['duplicates']):,}; {len(dupes):,} files "
        f"({human_size(sum(it.audio.size for it in dupes))}) go to the duplicates folder",
        "",
        f"Files to be {verb}:",
        f"  tracks:             {kinds['track']:,}",
        f"  duplicates:         {kinds['duplicate']:,}",
        f"  cover art etc.:     {kinds['sidecar']:,}",
        f"  already in place:   {sum(1 for it in items if targets[it] == it.audio.path):,}",
    ]
    if notes:
        lines += ["", *notes]
    return "\n".join(lines) + "\n"


def _details(items: list[Item], targets: dict[Item, str]) -> str:
    """Lists worth reviewing before applying: web-sourced metadata, unknown artists, unreadable files."""
    out = []
    web = [it for it in items if set(it.meta.sources.values()) & set(WEB_SOURCES)]
    if web:
        out += ["", f"Metadata found online ({len(web):,} files) - worth a spot check:"]
        for it in web:
            used = sorted({s for s in it.meta.sources.values() if s in WEB_SOURCES})
            out.append(f"  {it.audio.path}\n      -> {targets[it]}   [{', '.join(used)}]")
    unknown = [it for it in items if not it.meta.artist]
    if unknown:
        out += ["", f"Artist could not be determined ({len(unknown):,} files, filed under '{UNKNOWN_ARTIST}'):"]
        out += [f"  {it.audio.path}" for it in unknown]
    broken = [it for it in items if it.audio.error]
    if broken:
        out += ["", f"Unreadable or damaged files ({len(broken):,}; sorted by file name only):"]
        out += [f"  {it.audio.path}: {it.audio.error}" for it in broken]
    out += ["", "Every planned move is listed in moves.tsv; duplicates in duplicates_report.preview.txt."]
    return "\n".join(out) + "\n"


def render_duplicates_report(plan: dict, problems: dict[str, str] | None = None) -> str:
    """The text file left in the duplicates folder: which file superseded which, and why."""
    problems = problems or {}
    settings = plan["settings"]
    total = sum(len(group["superseded"]) for group in plan["duplicates"])
    lines = [
        "MusicSorter duplicates report",
        f"Planned {plan['created']}",
        "",
        f"The sorted library is in {settings['dest']}.",
        f"This folder holds {total:,} superseded copies of {len(plan['duplicates']):,} songs. For each song the",
        "best-quality copy (lossless > higher bitrate > lower bitrate) was kept in the library.",
        "Copies only count as the same recording when artist, title AND length (within a few seconds) match,",
        "so different cuts such as extended or radio edits are never listed here.",
        "",
    ]
    for number, group in enumerate(plan["duplicates"], 1):
        keeper = group["keeper"]
        lines.append(f"[{number}] {group['artist']} - {group['title']}")
        lines += _file_lines("KEPT", keeper, problems)
        for dup in group["superseded"]:
            lines += _file_lines("SUPERSEDED", dup, problems)
            lines.append(f"{'':16}reason: {dup['reason']}")
        lines.append("")
    return "\n".join(lines)


def _file_lines(label: str, entry: dict, problems: dict[str, str]) -> list[str]:
    head = f"  {label:<12}  {entry['quality']}, {fmt_duration(entry['duration'])}"
    lines = [head, f"{'':16}{entry['dst']}"]
    if entry["dst"] != entry["src"]:
        lines.append(f"{'':16}(was {entry['src']})")
    if entry["src"] in problems:
        lines.append(f"{'':16}NOT MOVED: {problems[entry['src']]}")
    return lines
