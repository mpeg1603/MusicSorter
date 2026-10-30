"""Online metadata lookup for files whose tags and names are not enough.

* AcoustID   - identifies a recording from its audio fingerprint (needs `fpcalc` and a free API key).
* MusicBrainz - finds the album a known artist + title was released on (no key, 1 request/second).
* Deezer     - fallback text search, also used for bare file names (no key).

Every answer is cached, so an interrupted or repeated run does not ask again.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass

from . import __version__
from .cache import Cache
from .model import AudioFile, Meta
from .names import artist_key, fold, is_various, primary_artist, same_artist, similar, strip_edition, title_key

USER_AGENT = f"MusicSorter/{__version__} ( {os.environ.get('MUSICSORTER_CONTACT', 'personal music library organiser')} )"

# Minimum seconds between requests to each host (MusicBrainz asks for at most one per second).
_INTERVALS = {"musicbrainz.org": 1.1, "api.deezer.com": 0.15, "api.acoustid.org": 0.4}

# Words in a recording's disambiguation that mean it is a different version of the song.
_VERSION_WORDS = ("live", "demo", "acoustic", "remix", "instrumental", "karaoke", "rehearsal", "unplugged", "edit")


class ServiceError(Exception):
    pass


@dataclass
class Match:
    source: str
    artist: str = ""
    title: str = ""
    album: str = ""  # empty when the recording was only found on singles or compilations
    album_artist: str = ""
    track: int = 0
    disc: int = 0
    year: str = ""
    single: bool = False


class Http:
    """JSON over HTTPS with per-host rate limiting and retries.

    Some networks drop Python's TLS handshake to certain hosts while curl's gets through (seen with
    MusicBrainz and AcoustID), so after a connection failure a host is switched over to curl."""

    def __init__(self):
        self.last: dict[str, float] = {}
        self.requests = 0
        self.curl = shutil.which("curl")
        self.via_curl: set[str] = set()

    def get_json(self, url: str, params: dict | None = None, form: dict | None = None) -> dict:
        host = urllib.parse.urlsplit(url).hostname or ""
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(form).encode() if form else None
        delay, problem = 2.0, ""
        for _ in range(6):
            wait = self.last.get(host, 0) + _INTERVALS.get(host, 0.5) - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last[host] = time.monotonic()
            self.requests += 1
            try:
                status, raw = (self._curl if host in self.via_curl else self._urllib)(url, body)
            except (OSError, subprocess.SubprocessError) as exc:
                problem = str(exc)
                if self.curl and host not in self.via_curl:
                    self.via_curl.add(host)
                    continue
            else:
                if status not in (429, 500, 502, 503, 504):
                    if status >= 400:
                        raise ServiceError(f"{host}: HTTP {status}")
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        payload, problem = None, "response was not JSON"
                    error = payload.get("error") if isinstance(payload, dict) else None
                    if isinstance(error, dict) and error.get("code") in (4, 700):  # Deezer: quota / busy
                        problem = str(error.get("message"))
                    elif payload is not None:
                        return payload
                else:
                    problem = f"HTTP {status}"
            time.sleep(delay)
            delay *= 2
        raise ServiceError(f"{host}: gave up after retries ({problem})")

    @staticmethod
    def _urllib(url: str, body: bytes | None) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _curl(self, url: str, body: bytes | None) -> tuple[int, bytes]:
        cmd = [self.curl, "-sS", "--max-time", "30", "-A", USER_AGENT, "-H", "Accept: application/json",
               "-w", "\n%{http_code}", url]
        if body is not None:
            cmd[1:1] = ["--data-binary", "@-"]
        out = subprocess.run(cmd, input=body, capture_output=True, timeout=45)
        if out.returncode != 0:
            raise OSError(out.stderr.decode("utf-8", "replace").strip() or f"curl exited with {out.returncode}")
        raw, _, status = out.stdout.rpartition(b"\n")
        return int(status), raw


def _credit(credits) -> str:
    """Join a MusicBrainz/AcoustID artist credit list into one name."""
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in credits or []).strip()


def _type_rank(primary: str, secondary, release_artist: str, artist: str) -> int | None:
    """Album < EP < Single. Compilations, live albums, remix albums etc. are not used for filing."""
    if {s.lower() for s in secondary or ()} - {"soundtrack"}:
        return None
    if release_artist and not same_artist(release_artist, artist):
        return None
    return {"album": 0, "ep": 1, "single": 2}.get((primary or "").lower())


def _lucene(text: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", text)


class MusicBrainz:
    URL = "https://musicbrainz.org/ws/2/recording/"

    def __init__(self, http: Http):
        self.http = http

    def search(self, artist: str, title: str, duration: float) -> Match | None:
        query = f'recording:"{_lucene(title)}" AND artist:"{_lucene(primary_artist(artist))}"'
        if duration:
            query += f" AND dur:[{int((duration - 10) * 1000)} TO {int((duration + 10) * 1000)}]"
        data = self.http.get_json(self.URL, {"query": query, "fmt": "json", "limit": 50})

        best, fallback = None, None
        for rec in data.get("recordings") or []:
            if not self._same_recording(rec, artist, title, duration):
                continue
            credit = _credit(rec.get("artist-credit")) or artist
            fallback = fallback or Match("musicbrainz", artist=credit, title=rec.get("title", ""))
            for release in rec.get("releases") or []:
                if (release.get("status") or "official").lower() != "official":
                    continue
                group = release.get("release-group") or {}
                rank = _type_rank(group.get("primary-type"), group.get("secondary-types"),
                                  _credit(release.get("artist-credit")), credit)
                if rank is None:
                    continue
                # earliest release wins; among same-day editions prefer one where the song is on disc 1
                disc = int(((release.get("media") or [{}])[0]).get("position") or 1)
                key = (rank, release.get("date") or "9999", disc, release.get("track-count") or 0)
                if best is None or key < best[0]:
                    best = (key, rec, credit, release)
        if best is None:
            return fallback
        (rank, date, _, _), rec, credit, release = best
        group = release.get("release-group") or {}
        match = Match("musicbrainz", artist=credit, title=rec.get("title", ""), year=date[:4] if date != "9999" else "")
        if rank == 2:
            match.single = True
            return match
        media = (release.get("media") or [{}])[0]
        number = str(((media.get("track") or [{}])[0]).get("number", ""))
        match.album = strip_edition(group.get("title") or release.get("title", ""))
        match.album_artist = _credit(release.get("artist-credit")) or credit
        match.track = int(number) if number.isdigit() else int(media.get("track-offset", -1)) + 1
        match.disc = int(media.get("position") or 0)
        return match

    @staticmethod
    def _same_recording(rec: dict, artist: str, title: str, duration: float) -> bool:
        if rec.get("score", 0) < 80 or rec.get("video"):
            return False
        if not same_artist(_credit(rec.get("artist-credit")), artist):
            return False
        if not similar(title_key(rec.get("title", "")), title_key(title)):
            return False
        length = (rec.get("length") or 0) / 1000
        if duration and length and abs(length - duration) > 10:
            return False
        note, wanted = fold(rec.get("disambiguation") or "").split(), fold(title).split()
        return not any(word in note and word not in wanted for word in _VERSION_WORDS)


class Deezer:
    URL = "https://api.deezer.com/"

    def __init__(self, http: Http, cache: Cache):
        self.http, self.cache = http, cache

    def search(self, query: str, artist: str, title: str, duration: float) -> Match | None:
        data = self.http.get_json(self.URL + "search", {"q": query, "limit": 25})
        hits = [hit for hit in data.get("data") or [] if self._acceptable(hit, query, artist, title, duration)]
        best = None
        for hit in hits[:5]:  # the same song usually appears on the album, singles and compilations
            match = self._match(hit)
            rank = 0 if match.album else 1 if match.single else 2
            if best is None or rank < best[0]:
                best = (rank, match)
        return best[1] if best else None

    @staticmethod
    def _acceptable(hit: dict, query: str, artist: str, title: str, duration: float) -> bool:
        hit_artist = (hit.get("artist") or {}).get("name", "")
        hit_title = hit.get("title") or ""
        gap = abs((hit.get("duration") or 0) - duration) if duration else 0
        if artist:
            return (gap <= 5 and same_artist(hit_artist, artist)
                    and similar(title_key(hit_title), title_key(title or query)))
        # No artist known: the query is a bare file name like "linkin park in the end" or "In the End".
        words = set(fold(query).split())
        title_words = set(title_key(hit.get("title_short") or hit_title).split())
        if not title_words or not title_words <= words:
            return False
        if set(fold(hit_artist, drop_article=True).split()) <= words:
            return gap <= 5
        return duration > 0 and gap <= 2 and title_key(hit_title) == fold(query)

    def _match(self, hit: dict) -> Match:
        artist = (hit.get("artist") or {}).get("name", "")
        match = Match("deezer", artist=artist, title=hit.get("title") or "")
        album = self._album((hit.get("album") or {}).get("id"))
        record_type = (album.get("record_type") or "").lower()
        album_artist = (album.get("artist") or {}).get("name", "")
        if record_type == "single":
            match.single = True
        elif record_type in ("album", "ep") and not is_various(album_artist) and same_artist(album_artist, artist):
            match.album = strip_edition(album.get("title") or "")
            match.album_artist = album_artist
            match.year = (album.get("release_date") or "")[:4]
            track_ids = [t.get("id") for t in (album.get("tracks") or {}).get("data") or []]
            if hit.get("id") in track_ids:
                match.track = track_ids.index(hit["id"]) + 1
        return match

    def _album(self, album_id) -> dict:
        if not album_id:
            return {}
        found, album = self.cache.get("deezer-album-v1", str(album_id))
        if not found:
            data = self.http.get_json(f"{self.URL}album/{album_id}")
            album = {} if "error" in data else {
                "title": data.get("title"), "record_type": data.get("record_type"),
                "release_date": data.get("release_date"), "artist": {"name": (data.get("artist") or {}).get("name")},
                "tracks": {"data": [{"id": t.get("id")} for t in (data.get("tracks") or {}).get("data") or []]},
            }
            self.cache.put("deezer-album-v1", str(album_id), album)
        return album


class AcoustID:
    URL = "https://api.acoustid.org/v2/lookup"

    def __init__(self, http: Http, key: str, fpcalc: str):
        self.http, self.key, self.fpcalc = http, key, fpcalc

    def identify(self, path: str) -> Match | None:
        try:
            out = subprocess.run([self.fpcalc, "-json", "-length", "120", path],
                                 capture_output=True, text=True, timeout=300, check=True)
            fingerprint = json.loads(out.stdout)
        except (OSError, subprocess.SubprocessError, ValueError):
            return None
        data = self.http.get_json(self.URL, form={
            "client": self.key, "format": "json", "meta": "recordings releasegroups compress",
            "duration": int(fingerprint["duration"]), "fingerprint": fingerprint["fingerprint"],
        })
        if data.get("status") != "ok":
            raise ServiceError(f"AcoustID: {(data.get('error') or {}).get('message', 'error')}")
        for result in sorted(data.get("results") or [], key=lambda r: -r.get("score", 0)):
            if result.get("score", 0) < 0.8:
                break
            for rec in result.get("recordings") or []:
                artist, title = _credit(rec.get("artists")), rec.get("title") or ""
                if not artist or not title:
                    continue
                match = Match("acoustid", artist=artist, title=title)
                best = None
                for group in rec.get("releasegroups") or []:
                    rank = _type_rank(group.get("type"), group.get("secondarytypes"), _credit(group.get("artists")), artist)
                    if rank is not None and (best is None or rank < best[0]):
                        best = (rank, group)
                if best and best[0] == 2:
                    match.single = True
                elif best:
                    match.album = strip_edition(best[1].get("title") or "")
                    match.album_artist = _credit(best[1].get("artists")) or artist
                return match
        return None


class Identifier:
    """Chooses which service to ask for a given file and caches every answer."""

    def __init__(self, cache: Cache, acoustid_key: str = "", warn=print):
        self.cache, self.warn = cache, warn
        self.http = Http()
        self.musicbrainz = MusicBrainz(self.http)
        self.deezer = Deezer(self.http, cache)
        self.fpcalc = shutil.which("fpcalc")
        self.acoustid = AcoustID(self.http, acoustid_key, self.fpcalc) if acoustid_key and self.fpcalc else None
        self.failures: Counter = Counter()
        self.disabled: set[str] = set()

    def lookup(self, audio: AudioFile, meta: Meta, name_query: str) -> Match | None:
        bucket = round(audio.duration / 4)
        if (not meta.artist or not meta.title) and self.acoustid:
            identity = f"{audio.size}:{audio.mtime_ns}:{os.path.basename(audio.path)}"
            match = self._ask("acoustid", identity, lambda: self.acoustid.identify(audio.path))
            if match:
                return match
        if meta.artist and meta.title:
            key = f"{artist_key(meta.artist)}|{title_key(meta.title)}|{bucket}"
            match = self._ask("musicbrainz", key, lambda: self.musicbrainz.search(meta.artist, meta.title, audio.duration))
            if match and (match.album or match.single):
                return match
            query = f"{primary_artist(meta.artist)} {meta.title}"
            return self._ask("deezer", key, lambda: self.deezer.search(query, meta.artist, meta.title, audio.duration)) or match
        query = " ".join(part for part in (meta.artist, meta.title or name_query) if part)
        if not query:
            return None
        key = f"q:{fold(query)}|{bucket}"
        return self._ask("deezer", key, lambda: self.deezer.search(query, "", meta.title, audio.duration))

    def _ask(self, service: str, key: str, ask) -> Match | None:
        found, value = self.cache.get(f"{service}-v1", key)
        if found:
            return Match(**value) if value else None
        if service in self.disabled:
            return None
        try:
            match = ask()
        except ServiceError as exc:
            self.failures[service] += 1
            if self.failures[service] >= 5:
                self.disabled.add(service)
                self.warn(f"\nWarning: {service} keeps failing ({exc}); not using it for the rest of this run.")
            return None
        self.failures[service] = 0
        self.cache.put(f"{service}-v1", key, asdict(match) if match else None)
        return match
