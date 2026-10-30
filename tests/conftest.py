import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TXXX
from mutagen.oggvorbis import OggVorbis

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is needed to generate audio")

_CODECS = {".flac": ["-c:a", "flac"], ".mp3": ["-c:a", "libmp3lame"], ".m4a": ["-c:a", "aac"], ".ogg": ["-c:a", "libvorbis"]}


def make_audio(path: Path, seconds: float, bitrate: str = "", tags: dict | None = None, freq: int = 440) -> Path:
    """Generate a sine-wave file with ffmpeg and tag it with mutagen."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
           *_CODECS[path.suffix]]
    if bitrate:
        cmd += ["-b:a", bitrate]
    subprocess.run([*cmd, "-map_metadata", "-1", str(path)], check=True)
    if tags:
        tags = dict(tags)
        release_type = tags.pop("releasetype", None)
        if path.suffix == ".mp3":
            try:
                easy = EasyID3(str(path))
            except Exception:
                ID3().save(str(path))
                easy = EasyID3(str(path))
            for key, value in tags.items():
                easy[key] = value
            easy.save()
            if release_type:
                id3 = ID3(str(path))
                id3.add(TXXX(desc="MusicBrainz Album Type", text=[release_type]))
                id3.save()
        else:
            f = {".flac": FLAC, ".m4a": EasyMP4, ".ogg": OggVorbis}[path.suffix](str(path))
            for key, value in tags.items():
                f[key] = value
            if release_type and path.suffix != ".m4a":
                f["releasetype"] = release_type
            f.save()
    return path
