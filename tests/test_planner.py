import os

from musicsorter.dedupe import find_duplicates
from musicsorter.model import AudioFile, Item, Meta
from musicsorter.planner import Settings, assign_targets, infer_album_artists

SETTINGS = Settings(sources=["/src"], dest="/lib", duplicates_dir="/dupes")


def item(path, duration=200.0, fmt="MP3", lossless=False, bitrate=320_000, **meta):
    audio = AudioFile(path=path, root="/src", size=1000, format=fmt, lossless=lossless, bitrate=bitrate,
                      duration=duration, bits=16 if lossless else 0, sample_rate=44100)
    return Item(audio, Meta(**meta))


def targets_of(items):
    targets, _ = assign_targets(items, SETTINGS)
    return [os.path.relpath(targets[it], "/") for it in items]


def test_title_track_without_release_type_counts_as_single():
    it = item("/src/a.mp3", artist="Linkin Park", title="In the End", album="In the End")
    assert targets_of([it]) == ["lib/Linkin Park/Singles/In the End.mp3"]


def test_title_track_confirmed_as_album_stays_in_album():
    it = item("/src/a.mp3", artist="Red Hot Chili Peppers", title="Californication", album="Californication",
              track=6, release_type="album")
    assert targets_of([it]) == ["lib/Red Hot Chili Peppers/Californication/06 - Californication.mp3"]


def test_artist_spellings_share_one_folder():
    items = [
        item("/src/1.mp3", artist="Red Hot Chili Peppers", title="Otherside", album="Californication", track=7),
        item("/src/2.mp3", artist="Red Hot Chili Peppers", title="Scar Tissue", album="Californication", track=3),
        item("/src/3.mp3", artist="red hot chili peppers", title="Parallel Universe", album="californication", track=2),
    ]
    assert {os.path.dirname(t) for t in targets_of(items)} == {"lib/Red Hot Chili Peppers/Californication"}


def test_collaboration_tracks_stay_with_the_album():
    items = [
        item("/src/ct/1.mp3", artist="Linkin Park", title="Papercut", album="Hybrid Theory", track=1),
        item("/src/ct/2.mp3", artist="Linkin Park", title="One Step Closer", album="Hybrid Theory", track=2),
        item("/src/ct/3.mp3", artist="Linkin Park & Jay-Z", title="Numb/Encore", album="Hybrid Theory", track=3),
    ]
    infer_album_artists(items)
    assert targets_of(items)[2] == "lib/Linkin Park/Hybrid Theory/03 - Numb-Encore.mp3"


def test_multi_disc_numbering():
    items = [
        item("/src/1.mp3", artist="Smashing Pumpkins", title="Tonight, Tonight", album="Mellon Collie", disc=1, track=2),
        item("/src/2.mp3", artist="Smashing Pumpkins", title="Thirty-Three", album="Mellon Collie", disc=2, track=6),
    ]
    assert [os.path.basename(t) for t in targets_of(items)] == ["1-02 - Tonight, Tonight.mp3", "2-06 - Thirty-Three.mp3"]


def test_best_quality_kept_and_different_cuts_survive():
    flac = item("/src/a.flac", 216.0, "FLAC", True, 900_000, artist="Linkin Park", title="In the End", album="Hybrid Theory", track=8)
    mp3_320 = item("/src/b.mp3", 216.4, artist="Linkin Park", title="In The End")
    mp3_128 = item("/src/c.mp3", 215.5, bitrate=128_000, artist="linkin park", title="In the End (Remastered)")
    extended = item("/src/d.mp3", 420.0, artist="Linkin Park", title="In the End")
    live = item("/src/e.mp3", 216.0, artist="Linkin Park", title="In the End (Live)")
    groups = find_duplicates([flac, mp3_320, mp3_128, extended, live], compare_content=False)
    assert groups == {flac: [mp3_320, mp3_128]}
    assert extended.duplicate_of is None and live.duplicate_of is None
    targets = targets_of([flac, mp3_320, mp3_128, extended, live])
    assert targets[1].startswith("dupes/Linkin Park/") and targets[2].startswith("dupes/Linkin Park/")
    assert targets[3] == "lib/Linkin Park/Singles/In the End.mp3"


def test_mixed_download_folder_is_not_a_compilation():
    items = [item(f"/src/Downloads/{n}.mp3", artist=artist, title=f"Song {n}", album="Greatest Hits", track=1)
             for n, artist in enumerate(["Queen", "Queen", "Queen", "ABBA", "Eminem"])]
    items += [item(f"/src/Downloads/x{n}.mp3", artist="Someone", title=f"Other {n}") for n in range(3)]
    infer_album_artists(items)
    assert not any(it.meta.compilation for it in items)
    assert {t.split("/")[1] for t in targets_of(items[:5])} == {"Queen", "ABBA", "Eminem"}


def test_untagged_compilation_folder_goes_to_various_artists():
    items = [item(f"/src/Now 45/{n}.mp3", artist=f"Artist {n}", title=f"Song {n}", album="Now 45", track=n + 1)
             for n in range(5)]
    infer_album_artists(items)
    assert targets_of(items)[0] == "lib/Various Artists/Now 45/01 - Artist 0 - Song 0.mp3"
