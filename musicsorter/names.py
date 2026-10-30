"""Text helpers: comparison keys, parsing of file and folder names, filesystem-safe names."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

YEAR = r"(?:19|20)\d{2}"
DASH = "[-–—]"

_FEAT_PAREN = re.compile(r"\s*[(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s[^)\]]*[)\]]", re.I)
_FEAT_TAIL = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.I)
_JOINERS = re.compile(r"\s*(?:,|;|/|&|\s(?:and|x|vs\.?|with)\s)\s*", re.I)
_REMASTER_PAREN = re.compile(r"\s*[(\[][^)\]]*\bremaster(?:ed)?\b[^)\]]*[)\]]", re.I)
_REMASTER_DASH = re.compile(rf"\s+{DASH}\s+[^-–—]*\bremaster(?:ed)?\b[^-–—]*$", re.I)
_SINGLE_MARK = re.compile(rf"\s*(?:{DASH}\s*single|[(\[]\s*single\s*[)\]])\s*$", re.I)
_EDITION = re.compile(
    r"\s*[(\[][^)\]]*\b(?:deluxe|edition|remaster(?:ed)?|expanded|bonus|anniversary|special|version)\b"
    r"[^)\]]*[)\]]\s*$",
    re.I,
)
_URL = re.compile(r"(?:https?://)?(?:www\.)?[\w-]+\.(?:com|net|org|ru|info|biz|su|ua|io)\b\S*", re.I)

_PLACEHOLDERS = {
    "unknown", "unknown artist", "unknown album", "unknown title", "untitled", "no title", "no artist",
    "no album", "n a", "none", "null", "artist", "album", "title", "track",
    "неизвестен", "неизвестный исполнитель", "неизвестный альбом", "без названия",
}
_TRACK_PLACEHOLDER = re.compile(r"^(?:track|трек|дорожка|piste|audio ?track)\s*\d+$")
_VARIOUS = {
    "various artists", "various", "va", "v a", "сборник", "разные исполнители", "varios artistas",
    "verschiedene interpreten", "artistes divers",
}


# --- comparison keys -------------------------------------------------------------------------

def fold(text: str, drop_article: bool = False) -> str:
    """Key that ignores case, accents, punctuation and spacing ('AC/DC' == 'ac-dc')."""
    s = unicodedata.normalize("NFKD", text)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).casefold().replace("&", " and ")
    s = re.sub(r"[\W_]+", " ", s).strip()
    if drop_article and s.startswith("the "):
        s = s[4:]
    return s or text.strip().casefold()


def primary_artist(artist: str) -> str:
    """'Linkin Park feat. Jay-Z' -> 'Linkin Park'."""
    s = _FEAT_TAIL.sub("", _FEAT_PAREN.sub("", artist)).strip()
    return s or artist.strip()


def lead_artist(artist: str) -> str:
    """First name of a collaboration: 'Linkin Park & Jay-Z' -> 'Linkin Park'."""
    s = primary_artist(artist)
    return _JOINERS.split(s, maxsplit=1)[0].strip() or s


def artist_key(artist: str) -> str:
    return fold(primary_artist(artist), drop_article=True)


def title_key(title: str) -> str:
    """Key for a song title. Featured artists and 'remastered' notes do not make a different song,
    but 'Live', 'Extended Mix', 'Acoustic' etc. do and are kept."""
    s = _FEAT_TAIL.sub("", _FEAT_PAREN.sub("", title))
    s = _REMASTER_DASH.sub("", _REMASTER_PAREN.sub("", s))
    return fold(s.strip() or title)


def album_key(album: str) -> str:
    return fold(_SINGLE_MARK.sub("", album).strip() or album)


def strip_edition(album: str) -> str:
    """'Hybrid Theory (Bonus Edition)' -> 'Hybrid Theory'. Used for album names that come from the web."""
    s = _SINGLE_MARK.sub("", album)
    while (stripped := _EDITION.sub("", s)) != s:
        s = stripped
    return s.strip() or album.strip()


def is_single_title(album: str) -> bool:
    """iTunes-style 'Song - Single' / 'Song (Single)' album names."""
    return bool(_SINGLE_MARK.search(album))


def is_various(name: str) -> bool:
    return bool(name) and fold(name) in _VARIOUS


def is_placeholder(value: str) -> bool:
    """Tag values that carry no information: 'Unknown Artist', 'Track 05', a bare URL, '-'."""
    key = fold(value)
    if not any(ch.isalnum() for ch in key):
        return True
    if key in _PLACEHOLDERS or _TRACK_PLACEHOLDER.match(key):
        return True
    return bool(_URL.fullmatch(value.strip()))


def similar(a: str, b: str, threshold: float = 0.88) -> bool:
    """Fuzzy equality of two keys. Short keys must match exactly ('pink' is not 'pinky')."""
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 8:
        return False
    return SequenceMatcher(None, a, b).ratio() >= threshold


def same_artist(a: str, b: str) -> bool:
    """True for 'Linkin Park' vs 'linkin park', 'The Doors' vs 'Doors', 'Linkin Park & Jay-Z' vs 'Linkin Park'."""
    if not a or not b:
        return False
    ka, kb = artist_key(a), artist_key(b)
    if similar(ka, kb):
        return True
    return fold(lead_artist(a), True) == kb or ka == fold(lead_artist(b), True)


# --- filesystem names ------------------------------------------------------------------------

# Characters that are illegal on NTFS/exFAT/FAT (and '/' everywhere), so the library stays portable.
_ILLEGAL = str.maketrans({"/": "-", "\\": "-", ":": " -", "*": "", "?": "", '"': "'", "<": "", ">": "", "|": "-"})
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_component(name: str, fallback: str = "Unknown", max_bytes: int = 200) -> str:
    """Turn a name into a single path component that is valid on Linux and Windows filesystems."""
    s = unicodedata.normalize("NFC", name)
    s = "".join(ch for ch in s if ch.isprintable()).translate(_ILLEGAL)
    s = re.sub(r"\s+", " ", s).strip()
    if s.startswith("..."):
        s = "…" + s[3:]  # "...And Justice for All" must not become a hidden folder
    s = s.lstrip(".").rstrip(". ")
    if s.split(".")[0].strip().lower() in _RESERVED:
        s = "_" + s
    encoded = s.encode()
    if len(encoded) > max_bytes:
        s = encoded[:max_bytes].decode("utf-8", "ignore").rstrip(". ")
    return s or fallback


def smart_case(text: str) -> str:
    """'in the end' -> 'In The End'; text that already has capitals is left alone."""
    if text and text == text.lower() and any(ch.isalpha() for ch in text):
        return re.sub(r"(^|[\s(\[/-])(\w)", lambda m: m[1] + m[2].upper(), text)
    return text


# --- file names ------------------------------------------------------------------------------

_JUNK_BRACKET = re.compile(
    r"\s*[(\[{][^)\]}]*\b(?:official|video|audio|lyrics?|hq|hd|\d*\s*kbps|mp3|flac|free download|"
    r"visuali[sz]er|clip|www)\b[^)\]}]*[)\]}]",
    re.I,
)


@dataclass
class NameHint:
    artist: str = ""
    album: str = ""
    title: str = ""
    track: int = 0
    disc: int = 0


def _split_number(text: str) -> tuple[int, int, str]:
    """'01 - Title' -> (0, 1, 'Title'), '1-03 Title' -> (1, 3, 'Title'), '99 Problems' -> (0, 0, '99 Problems')."""
    if m := re.match(r"^(\d)[-.](\d{2})(?:\s*[-._)]\s*|\s+)(.+)$", text):
        return int(m[1]), int(m[2]), m[3]
    # A number counts as a track number only with a separator after it, or when zero-padded,
    # so titles such as "99 Problems" or "50 Ways to Leave Your Lover" survive.
    m = re.match(r"^(\d{1,3})(?:\s*[._)]\s*|\s*-\s*(?!\d))(.+)$", text) or re.match(r"^(0\d|\d{3})\s+(.+)$", text)
    if m:
        number = int(m[1])
        if number > 100 and number % 100:  # "101 Title" = disc 1, track 1
            return number // 100, number % 100, m[2]
        return 0, number, m[2]
    return 0, 0, text


def parse_filename(stem: str) -> NameHint:
    """Guess artist/album/title/track from names like '01 - Title', 'Artist - Title',
    'Artist - Album - 03 - Title', '1-04. Title' or scene-style '01-artist_name-title-grp'."""
    scene = " " not in stem and "_" in stem
    text = stem.replace("_", " ") if scene else stem
    text = _JUNK_BRACKET.sub(" ", _URL.sub(" ", text))
    text = re.sub(r"\s+", " ", text).strip(" -_")
    separator = re.compile(r"\s*-\s*") if scene else re.compile(rf"\s+{DASH}\s+")

    hint = NameHint()
    hint.disc, hint.track, rest = _split_number(text)
    parts = [p.strip() for p in separator.split(rest) if p.strip()]
    if scene and len(parts) >= 3:
        parts = parts[:-1]  # trailing release-group tag
    if not parts:
        return hint

    before: list[str] = []
    after = parts
    if not hint.track:
        # "Artist - 03 - Title", "Artist - Album - 03 - Title", "Artist - 03. Title"
        for i in range(1, len(parts)):
            if re.fullmatch(r"\d{1,3}", parts[i]) and i + 1 < len(parts):
                hint.track, before, after = int(parts[i]), parts[:i], parts[i + 1:]
                break
            disc, track, remainder = _split_number(parts[i])
            if track:
                hint.disc, hint.track = disc, track
                before, after = parts[:i], [remainder, *parts[i + 1:]]
                break

    if before:
        hint.artist, hint.album, hint.title = before[0], " - ".join(before[1:]), " - ".join(after)
    elif len(parts) == 1:
        hint.title = parts[0]
    else:
        hint.artist, hint.title = parts[0], " - ".join(parts[1:])
    hint.artist, hint.album, hint.title = smart_case(hint.artist), smart_case(hint.album), smart_case(hint.title)
    return hint


# --- folder names ----------------------------------------------------------------------------

_DISC_DIR = re.compile(r"^(?:cd|disc|disk|диск)\s*[-_.]?\s*(\d{1,2})(?:\s*(?:of|из|/)\s*\d{1,2})?$", re.I)
_DISC_SUFFIX = re.compile(r"\s*[(\[]?\s*(?:cd|disc|disk|диск)\s*(\d{1,2})\s*[)\]]?\s*$", re.I)
_DIR_JUNK = re.compile(
    r"\s*[(\[{][^)\]}]*\b(?:flac|mp3|aac|ogg|alac|wav|ape|lossless|web|vinyl|\d*\s*kbps|vbr|cbr|v0|v2|hi ?res|"
    r"\d{2,3}|\d{2} ?[-/] ?\d{2,3}|24 ?bit|16 ?bit)\b[^)\]}]*[)\]}]",
    re.I,
)
_GENERIC_DIRS = {
    "music", "my music", "музыка", "mp3", "flac", "audio", "songs", "tracks", "download", "downloads",
    "загрузки", "new folder", "новая папка", "unsorted", "misc", "other", "temp", "tmp", "singles",
    "unknown", "unknown artist", "unknown album", "various", "various artists", "va", "playlist",
    "playlists", "collection", "library", "itunes", "itunes media", "media", "desktop", "torrents",
    "albums", "artists",
}
_FOLDER_PATTERNS = (
    re.compile(rf"^(?P<artist>.+?)\s+{DASH}\s+[(\[]?(?P<year>{YEAR})[)\]]?\s+{DASH}\s+(?P<album>.+)$"),
    re.compile(rf"^(?P<artist>.+?)\s+{DASH}\s+(?P<album>.+?)\s*[(\[](?P<year>{YEAR})[)\]]$"),
    re.compile(rf"^[(\[]?(?P<year>{YEAR})[)\]]?\s*(?:{DASH}|\.)?\s+(?P<album>[^(\[].*)$"),
    re.compile(rf"^(?P<album>.+?)\s*[(\[](?P<year>{YEAR})[)\]]$"),
    re.compile(rf"^(?P<artist>.+?)\s+{DASH}\s+(?P<album>.+)$"),
)


@dataclass
class FolderHint:
    artist: str = ""  # from an "Artist - Album" folder name
    album: str = ""
    year: str = ""
    disc: int = 0
    parent_artist: str = ""  # name of the folder above the album folder
    album_like: bool = False  # the folder name looked like an album folder (year or artist prefix)
    singles: bool = False  # an "<Artist>/Singles" folder


def _clean_dir(name: str) -> str:
    s = name.replace("_", " ") if " " not in name else name
    s = _DIR_JUNK.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def parse_folders(dirs: list[str]) -> FolderHint:
    """Read hints from the folders between the source root and the file, e.g.
    ['Linkin Park', '2000 - Hybrid Theory', 'CD1'] or ['Deftones - White Pony (2000) [FLAC]']."""
    hint = FolderHint()
    if dirs and (m := _DISC_DIR.match(dirs[-1].strip())):
        hint.disc = int(m[1])
        dirs = dirs[:-1]
    if not dirs:
        return hint
    name = _clean_dir(dirs[-1])
    if m := _DISC_SUFFIX.search(name):
        hint.disc = hint.disc or int(m[1])
        name = name[: m.start()].strip(" -")
    if fold(name) == "singles" and len(dirs) >= 2:
        hint.singles = True
        hint.parent_artist = _clean_dir(dirs[-2])
        return hint
    if not name or fold(name) in _GENERIC_DIRS:
        return hint
    for pattern in _FOLDER_PATTERNS:
        if m := pattern.match(name):
            groups = m.groupdict()
            hint.artist = (groups.get("artist") or "").strip()
            hint.album = groups["album"].strip()
            hint.year = groups.get("year") or ""
            hint.album_like = True
            break
    else:
        hint.album = name
    if len(dirs) >= 2:
        parent = _clean_dir(dirs[-2])
        if parent and fold(parent) not in _GENERIC_DIRS:
            hint.parent_artist = parent
    return hint
