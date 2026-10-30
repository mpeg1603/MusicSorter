"""Finding audio files and reading their tags and stream properties with mutagen. Read-only."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import mutagen
from mutagen.id3 import ID3

from .cache import Cache
from .model import AudioFile
from .ui import Progress

AUDIO_EXTENSIONS = {
    ".mp3", ".flac", ".m4a", ".aac", ".alac", ".ogg", ".oga", ".opus", ".wma", ".wav", ".aif", ".aiff",
    ".aifc", ".ape", ".wv", ".mpc", ".tta", ".ofr", ".dsf", ".dff", ".spx",
}

# mutagen class name -> (format label, lossless)
_FORMATS = {
    "MP3": ("MP3", False), "FLAC": ("FLAC", True), "OggFLAC": ("FLAC", True), "OggVorbis": ("Vorbis", False),
    "OggOpus": ("Opus", False), "OggSpeex": ("Speex", False), "MP4": ("AAC", False), "ASF": ("WMA", False),
    "WAVE": ("WAV", True), "AIFF": ("AIFF", True), "MonkeysAudio": ("APE", True), "WavPack": ("WavPack", True),
    "Musepack": ("MPC", False), "TrueAudio": ("TTA", True), "OptimFROG": ("OptimFROG", True),
    "DSF": ("DSF", True), "DSDIFF": ("DFF", True), "AAC": ("AAC", False), "AC3": ("AC3", False),
}

# Lower-cased tag names that carry each field in ID3, Vorbis comments, MP4, APEv2 and ASF.
TAG_ALIASES = {
    "artist": ["artist", "tpe1", "©art", "author"],
    "albumartist": ["albumartist", "album artist", "album_artist", "tpe2", "aart", "wm/albumartist",
                    "txxx:albumartist", "txxx:album artist"],
    "album": ["album", "talb", "©alb", "wm/albumtitle"],
    "title": ["title", "tit2", "©nam"],
    "track": ["tracknumber", "track", "trck", "trkn", "wm/tracknumber"],
    "tracktotal": ["tracktotal", "totaltracks"],
    "disc": ["discnumber", "disc", "tpos", "disk", "wm/partofset"],
    "disctotal": ["disctotal", "totaldiscs"],
    "date": ["originaldate", "date", "year", "tdor", "tdrc", "tyer", "©day", "wm/originalreleaseyear", "wm/year"],
    "releasetype": ["releasetype", "musicbrainz_albumtype", "txxx:musicbrainz album type", "txxx:releasetype",
                    "----:com.apple.itunes:musicbrainz album type", "----:com.apple.itunes:releasetype",
                    "musicbrainz/album type"],
    "compilation": ["compilation", "tcmp", "cpil", "wm/iscompilation", "txxx:compilation"],
}
_WANTED = {alias for aliases in TAG_ALIASES.values() for alias in aliases}
_CACHE_NS = "scan-v1"
_CACHED_FIELDS = ("format", "lossless", "bitrate", "sample_rate", "bits", "duration", "tags", "error")


def find_audio_files(roots: list[str], exclude: list[str] = ()) -> list[tuple[str, str]]:
    """Return (path, root) for every audio file under the roots. Hidden folders and symlinks are skipped."""
    excluded = {os.path.realpath(p) for p in exclude}
    seen: set[str] = set()
    found = []
    for root in roots:
        root = os.path.abspath(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames
                if not d.startswith(".") and os.path.realpath(os.path.join(dirpath, d)) not in excluded
            )
            for name in sorted(filenames):
                if name.startswith("._") or os.path.splitext(name)[1].lower() not in AUDIO_EXTENSIONS:
                    continue
                path = os.path.join(dirpath, name)
                if path in seen or os.path.islink(path):
                    continue
                seen.add(path)
                found.append((path, root))
    return found


def scan(files: list[tuple[str, str]], cache: Cache | None = None, workers: int = 8) -> list[AudioFile]:
    """Read every file (in parallel threads), reusing cached results for files that have not changed."""
    progress = Progress("Reading tags", len(files))
    results: list[AudioFile] = []
    todo = []
    for path, root in files:
        try:
            st = os.stat(path)
        except OSError as exc:
            results.append(AudioFile(path=path, root=root, error=str(exc)))
            progress.step()
            continue
        # Keyed by inode rather than path, so files the tool has already moved are still cached.
        key = f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"
        found, data = cache.get(_CACHE_NS, key) if cache else (False, None)
        if found:
            results.append(AudioFile(path=path, root=root, size=st.st_size, mtime_ns=st.st_mtime_ns, **data))
            progress.step()
        else:
            todo.append((path, root, st, key))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for (_, _, _, key), audio in zip(todo, pool.map(lambda job: read_audio(*job[:3]), todo), strict=False):
            results.append(audio)
            if cache:
                cache.put(_CACHE_NS, key, {name: getattr(audio, name) for name in _CACHED_FIELDS})
            progress.step()
    progress.finish()
    results.sort(key=lambda a: a.path)
    return results


def read_audio(path: str, root: str, st: os.stat_result) -> AudioFile:
    audio = AudioFile(path=path, root=root, size=st.st_size, mtime_ns=st.st_mtime_ns)
    try:
        f = mutagen.File(path)
    except Exception as exc:  # mutagen raises many different errors on damaged files
        audio.error = f"{type(exc).__name__}: {exc}"
        return audio
    if f is None:
        audio.error = "format not recognised"
        return audio

    kind = type(f).__name__
    info = f.info
    audio.format, audio.lossless = _FORMATS.get(kind, (kind, False))
    if kind == "MP4" and str(getattr(info, "codec", "")).startswith("alac"):
        audio.format, audio.lossless = "ALAC", True
    elif kind == "ASF" and "lossless" in str(getattr(info, "codec_name", "")).lower():
        audio.format, audio.lossless = "WMA Lossless", True
    audio.duration = float(getattr(info, "length", 0) or 0)
    audio.bitrate = int(getattr(info, "bitrate", 0) or 0)
    audio.sample_rate = int(getattr(info, "sample_rate", 0) or 0)
    audio.bits = int(getattr(info, "bits_per_sample", 0) or 0)
    try:
        audio.tags = extract_tags(f.tags)
    except Exception as exc:
        audio.error = f"unreadable tags: {exc}"
    return audio


def extract_tags(tags) -> dict[str, str]:
    """Map whatever tag format the file uses onto the field names in TAG_ALIASES."""
    if tags is None:
        return {}
    flat: dict[str, list[str]] = {}
    if isinstance(tags, ID3):
        for frame in tags.values():
            key = frame.FrameID.lower()
            if frame.FrameID == "TXXX":
                key += ":" + frame.desc.lower()
            if key in _WANTED:
                flat.setdefault(key, []).extend(_strings(frame))
    else:
        items = tags.as_dict().items() if hasattr(tags, "as_dict") else tags.items()
        for name, value in items:
            key = str(name).lower()
            if key in _WANTED:
                flat.setdefault(key, []).extend(_strings(value))

    result = {}
    for field, aliases in TAG_ALIASES.items():
        for alias in aliases:
            values = [v.strip() for v in flat.get(alias, []) if v and v.strip()]
            if values:
                # release type can be multi-valued ("album; compilation"); for the rest the first value wins
                result[field] = "; ".join(values) if field == "releasetype" else values[0]
                break
    return result


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return value.split("\x00")  # APEv2 and some ID3 writers separate multiple values with NUL
    if isinstance(value, bool):
        return ["1" if value else "0"]
    if isinstance(value, (bytes, bytearray)):  # MP4 freeform atoms
        return [bytes(value).decode("utf-8", "replace")]
    if isinstance(value, int):
        return [str(value)]
    if isinstance(value, tuple) and len(value) == 2 and all(isinstance(x, int) for x in value):
        number, total = value  # MP4 trkn / disk
        return [f"{number}/{total}" if total else str(number)]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in _strings(item)]
    if hasattr(value, "text"):  # ID3 text frames
        return [str(t) for t in value.text]
    if getattr(value, "kind", 0) == 0 and hasattr(value, "value"):  # APEv2 text / ASF attributes
        return _strings(value.value)
    return []
