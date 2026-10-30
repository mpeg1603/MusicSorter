"""Data passed between the pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AudioFile:
    """An audio file as found on disk: stream properties plus the tags we care about."""

    path: str
    root: str  # the source folder the file was found under
    size: int = 0
    mtime_ns: int = 0
    format: str = ""  # "FLAC", "MP3", "AAC", ...
    lossless: bool = False
    bitrate: int = 0  # bits per second
    sample_rate: int = 0
    bits: int = 0  # bits per sample, lossless formats only
    duration: float = 0.0  # seconds
    tags: dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def quality(self) -> tuple:
        """Sort key: any lossless file beats any lossy one; then resolution or bitrate."""
        if self.lossless:
            return (1, (self.bits or 16) * (self.sample_rate or 44100), 0)
        return (0, 0, self.bitrate)

    @property
    def quality_label(self) -> str:
        if self.lossless:
            detail = "/".join(
                part
                for part in (
                    f"{self.bits}bit" if self.bits else "",
                    f"{self.sample_rate / 1000:g}kHz" if self.sample_rate else "",
                )
                if part
            )
            return f"{self.format} {detail}".strip()
        if self.bitrate:
            return f"{self.format} {round(self.bitrate / 1000)}kbps"
        return self.format or "unknown format"


@dataclass
class Meta:
    """Resolved description of a track, with a note of where each field came from."""

    artist: str = ""
    album_artist: str = ""
    album: str = ""
    title: str = ""
    track: int = 0
    disc: int = 0
    disc_total: int = 0
    year: str = ""
    release_type: str = ""  # "album", "single", "ep", "album; compilation"... from tags or a web service
    single: bool = False  # released as a single (goes to the Singles folder)
    compilation: bool = False  # various-artists compilation
    sources: dict[str, str] = field(default_factory=dict)

    def fill(self, name: str, value, source: str) -> None:
        """Set a field only if it is still empty."""
        if value and not getattr(self, name):
            setattr(self, name, value)
            self.sources[name] = source


@dataclass(eq=False)
class Item:
    """One audio file going through the plan. Compared by identity so it can key dicts."""

    audio: AudioFile
    meta: Meta
    duplicate_of: Item | None = None
    identical: bool = False  # byte-for-byte copy of the file it duplicates
    keep_name: bool = False  # has a matching .cue sheet, so the file name must not change
