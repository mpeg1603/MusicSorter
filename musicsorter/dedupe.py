"""Finding duplicate recordings and choosing which copy to keep.

Two files are the same recording when
  * they are byte-for-byte identical, or
  * they have the same artist and title AND their lengths differ by no more than the tolerance.
The length check is what keeps a 7-minute extended cut and the 3-minute original apart.
Of each group the best-quality file is kept: lossless > high-bitrate lossy > low-bitrate lossy.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

from .model import Item
from .names import artist_key, title_key
from .ui import Progress


def keeper_rank(item: Item) -> tuple:
    """Higher is better. Quality decides; ties go to better-tagged album copies, then bigger files."""
    audio, meta = item.audio, item.meta
    tagged = sum(1 for field in ("artist", "title", "album", "track") if audio.tags.get(field))
    on_album = bool(meta.album) and not meta.single and not meta.compilation
    return (audio.quality, tagged, on_album, audio.size, -len(audio.path))


def find_duplicates(items: list[Item], tolerance: float = 3.0, compare_content: bool = True) -> dict[Item, list[Item]]:
    """Return {kept item: [superseded items]} and set `duplicate_of` on every superseded item."""
    loser_to_winner: dict[Item, Item] = {}

    if compare_content:
        for group in _identical_groups(items):
            keeper = max(group, key=keeper_rank)
            for item in group:
                if item is not keeper:
                    loser_to_winner[item] = keeper
                    item.identical = True

    by_song: dict[tuple[str, str], list[Item]] = defaultdict(list)
    for item in items:
        meta = item.meta
        if item not in loser_to_winner and meta.artist and meta.title and item.audio.duration > 0:
            by_song[(artist_key(meta.artist), title_key(meta.title))].append(item)
    for members in by_song.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda it: (it.audio.duration, it.audio.path))
        for cluster in _length_clusters(members, tolerance):
            keeper = max(cluster, key=keeper_rank)
            for item in cluster:
                if item is not keeper:
                    loser_to_winner[item] = keeper

    groups: dict[Item, list[Item]] = defaultdict(list)
    for item, winner in loser_to_winner.items():
        while winner in loser_to_winner:  # an identical copy's keeper may itself have lost on quality
            winner = loser_to_winner[winner]
            item.identical = False
        item.duplicate_of = winner
        groups[winner].append(item)
    for members in groups.values():
        members.sort(key=lambda it: it.audio.path)
    return dict(groups)


def _length_clusters(members: list[Item], tolerance: float) -> list[list[Item]]:
    """Split one artist+title (sorted by length) into groups whose lengths are within the tolerance
    of the shortest member, so lengths that merely creep upward do not chain together."""
    clusters: list[list[Item]] = []
    for item in members:
        if clusters and item.audio.duration - clusters[-1][0].audio.duration <= tolerance:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    return [c for c in clusters if len(c) > 1]


def _identical_groups(items: list[Item]) -> list[list[Item]]:
    by_size: dict[int, list[Item]] = defaultdict(list)
    for item in items:
        if item.audio.size > 0:
            by_size[item.audio.size].append(item)
    candidates = [group for group in by_size.values() if len(group) > 1]
    progress = Progress("Comparing same-size files", sum(len(g) for g in candidates))
    groups = []
    for same_size in candidates:
        by_quick: dict[bytes, list[Item]] = defaultdict(list)
        for item in same_size:
            if (digest := _digest(item.audio.path, quick=True)) is not None:
                by_quick[digest].append(item)
            progress.step()
        for maybe in by_quick.values():
            if len(maybe) < 2:
                continue
            by_full: dict[bytes, list[Item]] = defaultdict(list)
            for item in maybe:
                if (digest := _digest(item.audio.path, quick=False)) is not None:
                    by_full[digest].append(item)
            groups.extend(sorted(g, key=lambda it: it.audio.path) for g in by_full.values() if len(g) > 1)
    progress.finish()
    return groups


def _digest(path: str, quick: bool) -> bytes | None:
    """Hash of the whole file, or (quick) of its first and last 256 KB."""
    h = hashlib.blake2b(digest_size=20)
    chunk = 256 * 1024
    try:
        with open(path, "rb") as fh:
            if quick:
                h.update(fh.read(chunk))
                fh.seek(0, 2)
                if fh.tell() > 2 * chunk:
                    fh.seek(-chunk, 2)
                    h.update(fh.read(chunk))
            else:
                while block := fh.read(1024 * 1024):
                    h.update(block)
    except OSError:
        return None
    return h.digest()
