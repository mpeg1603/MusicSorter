# MusicSorter

Sorts a messy music library into `Artist/Album` folders and moves duplicate tracks into a separate,
equally sorted, duplicates folder, with a report of which file superseded which.

```
Music/
├── Deftones/
│   └── White Pony/
│       ├── 01 - Feiticeira.flac
│       └── 10 - Change (In the House of Flies).flac
├── Linkin Park/
│   ├── Hybrid Theory/
│   │   ├── 01 - Papercut.flac
│   │   └── 08 - In the End.flac
│   └── Singles/
│       └── New Divide.mp3
└── Various Artists/
    └── Now 45/
        └── 01 - Somebody - Some Song.mp3
Duplicates/
├── duplicates_report.txt
└── Linkin Park/
    └── Hybrid Theory/
        ├── 08 - In the End.mp3        <- 320 kbps copy, superseded by the FLAC
        └── 08 - In the End (2).mp3    <- 128 kbps copy
```

## Setup

Needs Python 3.10+. The launcher creates its own virtualenv in `.venv` on first run:

```sh
./musicsorter.sh --help
ln -s "$PWD/musicsorter.sh" ~/.local/bin/musicsorter   # optional: call it as `musicsorter`
```

Optional, for identifying files that have neither useful tags nor a useful name (audio fingerprinting):

```sh
sudo pacman -S chromaprint                 # provides fpcalc
# get a free API key at https://acoustid.org/new-application, then:
export ACOUSTID_API_KEY=xxxxxxxx
```

## Usage

It works in three steps, and only `apply` changes anything on disk.

```sh
# 1. Plan: scans and decides everything; writes a plan folder; touches nothing in the library.
musicsorter plan /mnt/music/Music --dest /mnt/music/Music

# 2. Read the plan folder it prints: plan.txt (summary + things worth checking),
#    moves.tsv (every single move), duplicates_report.preview.txt.

# 3. Apply it. Re-running apply after an interruption continues where it stopped.
musicsorter apply ./musicsorter-plan-20261030-120000

# Changed your mind? Everything is journalled:
musicsorter undo ./musicsorter-plan-20261030-120000
```

`--dest` may be the source folder itself (sort in place, files are just renamed: instant and needs no
extra space) or a different folder. With `--copy` the originals are left untouched and the sorted
library is built as a copy, which needs as much free space as the music itself.

The duplicates go to a `Duplicates` folder next to `--dest` (`--duplicates-dir` to change it).
Running `plan` again on a sorted library is a no-op. To merge new music into it, give both folders as
sources, so new tracks are also checked against what's already there:
`musicsorter plan ~/Downloads/new /mnt/music/Music --dest /mnt/music/Music`.

Useful options for `plan`:

| option | effect |
| --- | --- |
| `--copy` | copy instead of move |
| `--offline` | no web lookups |
| `--duration-tolerance 3` | how many seconds two files may differ and still be the same recording |
| `--compilations artist` | file various-artists compilations under each track's artist instead of `Various Artists` |
| `--keep-filenames` | keep original file names instead of `NN - Title.ext` |
| `--no-sidecars` | leave cover art, logs and cue sheets where they are |
| `--no-content-check` | skip hashing same-size files to find byte-identical copies |

## How it decides

**Artist / album / title**, in this order, each step only filling what is still missing:

1. The file's tags (ID3, Vorbis comments, MP4, APE, WMA). Placeholders such as `Unknown Artist` or `Track 05` are ignored.
2. The file name: `01 - Title`, `Artist - Title`, `Artist - Album - 03 - Title`, `1-04 Title`, scene-style `01-artist_name-title-grp`, and so on.
3. The folders: `Artist/2000 - Album/`, `Artist - Album (2000) [FLAC]/`, `CD1/`. A folder name is only trusted for the album when it agrees with the artist.
4. The web:
   * [MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API) to find which album a known artist and title belongs to. It prefers the original studio album, then an EP, then a single, and ignores compilations, live albums and bootlegs.
   * [Deezer](https://developers.deezer.com/api) as a fallback, and for bare file names.
   * [AcoustID](https://acoustid.org/webservice) when nothing else identifies the file (needs `fpcalc` and a key).

   Results are cached in `~/.cache/musicsorter/`, so a second plan run doesn't repeat any lookups.
   Everything found online is listed in `plan.txt` for a spot check. MusicBrainz allows one request
   per second, so a library with many incomplete tags takes a while the first time.

**Album or single.** A track goes to `Artist/Singles/` when it was released as a single (from a release-type
tag or the web), when its "album" is named like the song and nothing says it's an album, or when no
album can be determined.

**Artist folder.** The album artist if tagged, otherwise the track artist without "feat. …".
Different spellings (`Red Hot Chili Peppers`, `red hot chili peppers`, `The Red Hot Chili Peppers`)
share one folder. Collaborations on an album (`Linkin Park & Jay-Z`) stay with the album.

**Duplicates.** Two files are the same recording if they are byte-for-byte identical, or if they share
artist and title and their lengths differ by at most `--duration-tolerance` seconds. So a 7-minute extended
cut and the 3-minute original are both kept, and so are `(Live)`, `(Acoustic)`, remixes and similar.
Notes like `(Remastered 2011)` or `feat. X` don't make a different song.
From each group the best copy stays in the library: lossless (FLAC, ALAC, WAV, APE…) beats lossy, then
higher bit depth/sample rate or higher bitrate wins. On equal quality, the better-tagged copy that sits on
the original album wins. The others are **moved** (never deleted) into the duplicates folder, with
`duplicates_report.txt` listing for every song the kept file and each superseded file, with formats,
lengths, old and new locations, and the reason.

## Safety

* `plan` only reads. Files are never re-encoded and their tags are never modified.
* `apply` never overwrites an existing file, and skips any file that changed since it was planned.
  Moves to another disk are copied, size-checked and only then removed from the source.
  Free space is checked before starting.
* Every completed step goes to `journal.jsonl` in the plan folder, so `undo` can reverse it even after a crash or Ctrl+C.
* Nothing is deleted. Empty folders left behind by the moves are removed, and `undo` recreates them.

Things to know:

* Playlists (`.m3u`) that point at the old paths will no longer match.
* A FLAC transcoded from an MP3 counts as lossless, because the tool can't detect fake lossless.
* AAC/Opus are compared with MP3 by bitrate alone, although they sound better at the same bitrate.
* Files inside hidden folders (`.something/`) and symlinks are ignored.

## Development

```sh
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

The end-to-end tests generate small audio files with `ffmpeg`.
