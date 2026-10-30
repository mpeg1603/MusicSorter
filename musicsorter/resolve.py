"""Working out artist / album / title for a file: tags first, then file and folder names, then the web."""

from __future__ import annotations

import os
import re

from .lookup import Identifier, Match
from .model import AudioFile, Meta
from .names import fold, is_placeholder, is_various, parse_filename, parse_folders, same_artist


def _number_pair(value: str) -> tuple[int, int]:
    """'3/12' -> (3, 12); '03' -> (3, 0)."""
    m = re.match(r"\s*(\d+)(?:\s*(?:/|of)\s*(\d+))?", value or "")
    return (int(m[1]), int(m[2] or 0)) if m else (0, 0)


def meta_from_tags(tags: dict[str, str]) -> Meta:
    meta = Meta()
    for name, tag in (("artist", "artist"), ("album_artist", "albumartist"), ("album", "album"), ("title", "title")):
        value = tags.get(tag, "").strip()
        if value and not is_placeholder(value):
            meta.fill(name, value, "tags")
    track, _ = _number_pair(tags.get("track", ""))
    disc, disc_total = _number_pair(tags.get("disc", ""))
    meta.fill("track", track, "tags")
    meta.fill("disc", disc, "tags")
    meta.disc_total = disc_total or _number_pair(tags.get("disctotal", ""))[0]
    if year := re.search(r"\b(1[89]\d\d|20\d\d)\b", tags.get("date", "")):
        meta.fill("year", year[1], "tags")
    meta.release_type = tags.get("releasetype", "").lower()
    meta.single = "single" in meta.release_type
    flagged = tags.get("compilation", "").strip().lower() in ("1", "true", "yes")
    meta.compilation = is_various(meta.album_artist) or (flagged and not meta.album_artist)
    return meta


def resolve(audio: AudioFile, identifier: Identifier | None = None) -> Meta:
    meta = meta_from_tags(audio.tags)
    path = audio.path
    stem = os.path.splitext(os.path.basename(path))[0]
    _fill_from_filename(meta, stem)

    try:
        dirs = list(os.path.relpath(os.path.dirname(path), audio.root).split(os.sep))
    except ValueError:
        dirs = []
    folder = parse_folders([d for d in dirs if d not in (".", "")])
    # Folder names are only trusted for the album when they agree with the artist we already have,
    # e.g. "Linkin Park/2000 - Hybrid Theory/" or "Linkin Park - Hybrid Theory (2000)/".
    if meta.artist and not meta.album and not meta.single and folder.album:
        if same_artist(folder.artist, meta.artist) or same_artist(folder.parent_artist, meta.artist):
            meta.fill("album", folder.album, "folder")
            meta.fill("year", folder.year, "folder")
    if meta.album:
        meta.fill("disc", folder.disc, "folder")

    if identifier and (not meta.artist or not meta.title or not (meta.album or meta.single)):
        name = parse_filename(stem)
        name_query = " ".join(part for part in (name.artist, name.album, name.title) if part)
        match = identifier.lookup(audio, meta, name_query)
        if match:
            _apply_match(meta, match)

    if not meta.artist:
        # Last resort: "Artist - Album/", "Artist/2001 - Album/", or exactly <root>/Artist/Album/ and
        # <root>/Artist/Singles/ (the layout this tool produces, so re-runs keep untagged files in place).
        trusted = folder.album_like or folder.singles or len(dirs) == 2
        candidate = folder.artist or (folder.parent_artist if trusted else "")
        if candidate:
            meta.fill("artist", candidate, "folder")
            if not meta.single and not folder.singles:
                meta.fill("album", folder.album, "folder")
                meta.fill("year", folder.year, "folder")
    return meta


def _fill_from_filename(meta: Meta, stem: str) -> None:
    name = parse_filename(stem)
    artist, title = name.artist, name.title
    # "Title - Artist" instead of "Artist - Title": recognisable when one half matches a tag.
    if artist and title:
        if (meta.artist and same_artist(title, meta.artist) and not same_artist(artist, meta.artist)) or (
            meta.title and fold(artist) == fold(meta.title)
        ):
            artist, title = title, artist
    artist = "" if is_placeholder(artist or "-") else artist
    title = "" if is_placeholder(title or "-") else title
    meta.fill("artist", artist, "filename")
    meta.fill("title", title, "filename")
    if name.album and same_artist(artist, meta.artist):
        meta.fill("album", name.album, "filename")
    meta.fill("track", name.track, "filename")
    meta.fill("disc", name.disc, "filename")


def _apply_match(meta: Meta, match: Match) -> None:
    meta.fill("artist", match.artist, match.source)
    meta.fill("title", match.title, match.source)
    if meta.album or meta.single:
        return
    if match.single:
        meta.single = True
        meta.release_type = "single"
        meta.sources["single"] = match.source
    elif match.album:
        meta.release_type = "album"
        meta.fill("album", match.album, match.source)
        meta.fill("album_artist", match.album_artist, match.source)
        meta.fill("year", match.year, match.source)
        if meta.sources.get("track") != "tags":  # a number from the file name belongs to some other release
            meta.track, meta.disc = match.track, match.disc
            meta.sources["track"] = match.source
