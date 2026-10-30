"""Deciding where every file goes.

    <library>/<Artist>/<Album>/<NN - Title>.ext
    <library>/<Artist>/Singles/<Title>.ext          (singles, or album unknown)
    <library>/Various Artists/<Album>/<NN - Artist - Title>.ext
    <duplicates>/<Artist>/<Album>/...               (superseded copies, same layout)
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime

from .model import Item
from .names import (
    album_key, artist_key, fold, is_single_title, is_various, lead_artist, primary_artist, safe_component, title_key,
)

SINGLES = "Singles"
VARIOUS_ARTISTS = "Various Artists"
UNKNOWN_ARTIST = "Unknown Artist"
# Non-audio files that travel with an album folder (cover art, rip logs, cue sheets...).
SIDECAR_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".cue", ".log", ".nfo", ".txt", ".pdf", ".m3u", ".m3u8",
    ".accurip", ".md5", ".sfv", ".ffp",
}


@dataclass
class Settings:
    sources: list[str]
    dest: str
    duplicates_dir: str
    mode: str = "move"  # or "copy"
    keep_filenames: bool = False
    compilations: str = "various"  # or "artist"
    sidecars: bool = True


@dataclass
class Move:
    src: str
    dst: str
    kind: str  # "track", "duplicate" or "sidecar"
    size: int
    mtime_ns: int


def build_plan(items: list[Item], duplicates: dict[Item, list[Item]], settings: Settings) -> tuple[dict, dict[Item, str]]:
    """Return the plan (as saved to plan.json) and the target path of every audio file."""
    infer_album_artists(items)
    for item in items:
        item.keep_name = os.path.exists(os.path.splitext(item.audio.path)[0] + ".cue")
    targets, claimed = assign_targets(items, settings)

    moves = [
        Move(it.audio.path, targets[it], "duplicate" if it.duplicate_of else "track", it.audio.size, it.audio.mtime_ns)
        for it in items
        if targets[it] != it.audio.path
    ]
    if settings.sidecars:
        moves += sidecar_moves(items, targets, claimed)

    plan = {
        "format": 1,
        "created": datetime.now().isoformat(timespec="seconds"),
        "settings": asdict(settings),
        "moves": [asdict(m) for m in moves],
        "duplicates": [
            {
                "artist": keeper.meta.artist or UNKNOWN_ARTIST,
                "title": keeper.meta.title or os.path.basename(keeper.audio.path),
                "keeper": _entry(keeper, targets),
                "superseded": [dict(_entry(d, targets), reason=_reason(d, keeper)) for d in losers],
            }
            for keeper, losers in sorted(
                duplicates.items(),
                key=lambda kv: (artist_key(kv[0].meta.artist), title_key(kv[0].meta.title), kv[0].audio.path),
            )
        ],
    }
    return plan, targets


def infer_album_artists(items: list[Item]) -> None:
    """Fill in a missing album artist for albums whose tracks sit together in one folder.

    Tracks credited to "Artist A" and "Artist A & Artist B" on the same album all belong in Artist A's
    folder; an album where no artist has a majority is treated as a various-artists compilation.
    Only applies when the album makes up (nearly) the whole folder: a Downloads folder holding several
    different artists' "Greatest Hits" tracks is not one album."""
    per_dir = Counter(os.path.dirname(it.audio.path) for it in items)
    groups: dict[tuple[str, str], list[Item]] = defaultdict(list)
    for item in items:
        meta = item.meta
        if meta.album and meta.artist and not meta.album_artist and not meta.compilation and not meta.single:
            groups[(os.path.dirname(item.audio.path), album_key(meta.album))].append(item)
    for (directory, _), members in groups.items():
        if len(members) < 2 or len({artist_key(it.meta.artist) for it in members}) == 1:
            continue
        if len(members) < 0.8 * per_dir[directory]:
            continue
        lead = {it: fold(lead_artist(it.meta.artist), drop_article=True) for it in members}
        top, count = Counter(lead.values()).most_common(1)[0]
        if count * 2 >= len(members):
            name = Counter(primary_artist(it.meta.artist) for it in members if lead[it] == top).most_common(1)[0][0]
            for it in members:
                if lead[it] == top:
                    it.meta.album_artist = name
                    it.meta.sources["album_artist"] = "inferred"
        elif len(set(lead.values())) >= 3:
            numbers = [(it.meta.disc, it.meta.track) for it in members if it.meta.track]
            if len(numbers) == len(set(numbers)):  # one release has no repeated track numbers
                for it in members:
                    it.meta.compilation = True


def folder_artist(item: Item, settings: Settings) -> str:
    meta = item.meta
    if meta.compilation and meta.album and settings.compilations == "various":
        return VARIOUS_ARTISTS
    if meta.album_artist and not is_various(meta.album_artist):
        return meta.album_artist
    return primary_artist(meta.artist) if meta.artist else UNKNOWN_ARTIST


def assign_targets(items: list[Item], settings: Settings) -> tuple[dict[Item, str], set[str]]:
    artist_of = {it: folder_artist(it, settings) for it in items}

    # One folder per artist even if the tags spell it differently ("Red Hot Chili Peppers" /
    # "red hot chili peppers" / "The Red Hot Chili Peppers"): the most common spelling wins.
    spellings: dict[str, Counter] = defaultdict(Counter)
    for name in artist_of.values():
        spellings[artist_key(name)][name] += 1
    artist_dir = {key: safe_component(_pick_name(names), UNKNOWN_ARTIST) for key, names in spellings.items()}

    albums: dict[tuple[str, str], list[Item]] = defaultdict(list)
    for it in items:
        if it.meta.album and not it.meta.single:
            albums[(artist_key(artist_of[it]), album_key(it.meta.album))].append(it)
    album_dir: dict[tuple[str, str], str] = {}
    multi_disc: dict[tuple[str, str], bool] = {}
    for key, members in albums.items():
        if _single_like(key[1], members):
            continue
        name = safe_component(_pick_name(Counter(it.meta.album for it in members)))
        if fold(name) == fold(SINGLES):
            name += " (Album)"
        album_dir[key] = name
        discs = {it.meta.disc for it in members if it.meta.disc}
        multi_disc[key] = len(discs) > 1 or any(it.meta.disc_total > 1 for it in members)

    preferred: dict[Item, str] = {}
    for it in items:
        a_key = artist_key(artist_of[it])
        key = (a_key, album_key(it.meta.album)) if it.meta.album and not it.meta.single else None
        folder = album_dir.get(key, SINGLES)
        root = settings.duplicates_dir if it.duplicate_of else settings.dest
        various = artist_of[it] == VARIOUS_ARTISTS
        name = _file_name(it, folder, multi_disc.get(key, False), various, settings.keep_filenames)
        preferred[it] = os.path.join(root, artist_dir[a_key], folder, name)

    # Files already where they belong claim their paths first, so running the tool again is a no-op.
    claimed: set[str] = set()
    targets: dict[Item, str] = {}
    for it in sorted(items, key=lambda it: (preferred[it] != it.audio.path, it.audio.path)):
        targets[it] = claim(preferred[it], it.audio.path, claimed)
    return targets, claimed


def _single_like(key: str, members: list[Item]) -> bool:
    """An 'album' that is really a single: 'Song - Single', or every track in it is named like the album.
    A release type from the tags or a web service ('album', 'ep') overrides the guess, so a lone
    title track such as 'Californication' stays in its album folder."""
    if any(is_single_title(it.meta.album) for it in members):
        return True
    if any(re.search(r"\b(?:album|ep)\b", it.meta.release_type) for it in members):
        return False
    titled = [it for it in members if it.meta.title]
    return bool(titled) and all(title_key(it.meta.title) == key for it in titled)


def _pick_name(spellings: Counter) -> str:
    """Most common spelling; on a tie prefer mixed case over ALL CAPS / all lower case."""
    return max(spellings.items(), key=lambda kv: (kv[1], not kv[0].islower() and not kv[0].isupper(), kv[0]))[0]


def _file_name(item: Item, folder: str, multi_disc: bool, various: bool, keep_filenames: bool) -> str:
    audio, meta = item.audio, item.meta
    stem, ext = os.path.splitext(os.path.basename(audio.path))
    if keep_filenames or item.keep_name or not meta.title:
        return safe_component(stem, "Untitled") + ext.lower()
    name = meta.title
    if folder != SINGLES:
        if various and meta.artist:
            name = f"{meta.artist} - {name}"
        if meta.track:
            number = f"{meta.track:02d}"
            if multi_disc and meta.disc:
                number = f"{meta.disc}-{number}"
            name = f"{number} - {name}"
    return safe_component(name, "Untitled") + ext.lower()


def claim(path: str, src: str, claimed: set[str]) -> str:
    """Reserve `path`, or 'name (2).ext', 'name (3).ext'... if taken. Never points at an existing file."""
    base, ext = os.path.splitext(path)
    n = 1
    while True:
        candidate = path if n == 1 else f"{base} ({n}){ext}"
        key = candidate.casefold()  # stay safe on case-insensitive disks (NTFS, exFAT)
        if candidate == src or (key not in claimed and not os.path.lexists(candidate)):
            claimed.add(key)
            return candidate
        n += 1


def sidecar_moves(items: list[Item], targets: dict[Item, str], claimed: set[str]) -> list[Move]:
    """Cover art, logs and cue sheets follow the audio when a whole folder goes to one album folder."""
    by_dir: dict[str, list[Item]] = defaultdict(list)
    for it in items:
        by_dir[os.path.dirname(it.audio.path)].append(it)
    moves = []
    for directory, members in sorted(by_dir.items()):
        kept = [it for it in members if not it.duplicate_of] or members
        destinations = {os.path.dirname(targets[it]) for it in kept}
        if len(destinations) != 1:
            continue
        destination = destinations.pop()
        if destination == directory:
            continue
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or os.path.splitext(entry.name)[1].lower() not in SIDECAR_EXTENSIONS:
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            st = entry.stat(follow_symlinks=False)
            dst = claim(os.path.join(destination, entry.name), entry.path, claimed)
            moves.append(Move(entry.path, dst, "sidecar", st.st_size, st.st_mtime_ns))
    return moves


def _entry(item: Item, targets: dict[Item, str]) -> dict:
    return {
        "src": item.audio.path,
        "dst": targets[item],
        "quality": item.audio.quality_label,
        "duration": round(item.audio.duration, 1),
        "size": item.audio.size,
    }


def _reason(item: Item, keeper: Item) -> str:
    if item.identical and item.audio.quality == keeper.audio.quality:
        return "identical copy"
    if item.audio.quality < keeper.audio.quality:
        return "lower quality"
    return "same quality; the kept copy has fuller tags or sits on the original album"
