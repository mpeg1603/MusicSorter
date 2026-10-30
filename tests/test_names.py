import pytest

from musicsorter.names import (
    album_key, artist_key, is_placeholder, parse_filename, parse_folders, safe_component, same_artist, strip_edition,
    title_key,
)


@pytest.mark.parametrize("stem, expected", [
    ("01 - Papercut", ("", "", "Papercut", 0, 1)),
    ("01. Papercut", ("", "", "Papercut", 0, 1)),
    ("01 Papercut", ("", "", "Papercut", 0, 1)),
    ("1-03 Cure for the Itch", ("", "", "Cure for the Itch", 1, 3)),
    ("Linkin Park - In The End", ("Linkin Park", "", "In The End", 0, 0)),
    ("Linkin Park - In The End (Official Video)", ("Linkin Park", "", "In The End", 0, 0)),
    ("Linkin Park - In the End [128kbps]", ("Linkin Park", "", "In the End", 0, 0)),
    ("Linkin Park - Hybrid Theory - 08 - In the End", ("Linkin Park", "Hybrid Theory", "In the End", 0, 8)),
    ("Deftones - 03. Change", ("Deftones", "", "Change", 0, 3)),
    ("05 - Deftones - Change", ("Deftones", "", "Change", 0, 5)),
    ("01-linkin_park-in_the_end-ftd", ("Linkin Park", "", "In The End", 0, 1)),
    ("99 Problems", ("", "", "99 Problems", 0, 0)),
    ("50 Cent - In Da Club", ("50 Cent", "", "In Da Club", 0, 0)),
    ("Artist - 1999", ("Artist", "", "1999", 0, 0)),
])
def test_parse_filename(stem, expected):
    h = parse_filename(stem)
    assert (h.artist, h.album, h.title, h.disc, h.track) == expected


@pytest.mark.parametrize("dirs, artist, album, year, parent, disc", [
    (["Linkin Park", "2000 - Hybrid Theory"], "", "Hybrid Theory", "2000", "Linkin Park", 0),
    (["Deftones - White Pony (2000) [FLAC]"], "Deftones", "White Pony", "2000", "", 0),
    (["Smashing Pumpkins", "Mellon Collie", "CD2"], "", "Mellon Collie", "", "Smashing Pumpkins", 2),
    (["Downloads"], "", "", "", "", 0),
    (["Linkin Park", "Meteora [320kbps]"], "", "Meteora", "", "Linkin Park", 0),
    (["Music", "Hybrid Theory"], "", "Hybrid Theory", "", "", 0),
])
def test_parse_folders(dirs, artist, album, year, parent, disc):
    h = parse_folders(dirs)
    assert (h.artist, h.album, h.year, h.parent_artist, h.disc) == (artist, album, year, parent, disc)


def test_parse_singles_folder():
    h = parse_folders(["Deftones", "Singles"])
    assert h.singles and h.parent_artist == "Deftones" and not h.album


def test_keys_ignore_noise_but_keep_versions():
    assert artist_key("The Red Hot Chili Peppers") == artist_key("red hot chili peppers")
    assert artist_key("Linkin Park feat. Jay-Z") == artist_key("Linkin Park")
    assert title_key("In the End (Remastered 2011)") == title_key("In The End")
    assert title_key("Song - 2009 Remaster") == title_key("Song")
    assert title_key("In the End (feat. Someone)") == title_key("In the End")
    assert title_key("In the End (Live)") != title_key("In the End")
    assert title_key("One (Extended Mix)") != title_key("One")
    assert album_key("In the End - Single") == album_key("In The End")


def test_same_artist():
    assert same_artist("Linkin Park & Jay-Z", "Linkin Park")
    assert same_artist("The Doors", "Doors")
    assert not same_artist("Pink", "Pink Floyd")
    assert not same_artist("Pink", "Pinky")


def test_placeholders():
    for junk in ("Unknown Artist", "Track 05", "-", "www.example.com", "<unknown>"):
        assert is_placeholder(junk)
    assert not is_placeholder("Hybrid Theory")


def test_safe_component():
    assert safe_component("AC/DC") == "AC-DC"
    assert safe_component("Album: Part 2?") == "Album - Part 2"
    assert safe_component("...And Justice for All") == "…And Justice for All"
    assert safe_component("CON") == "_CON"
    assert safe_component("   ") == "Unknown"
    assert len(safe_component("x" * 500).encode()) <= 200


def test_strip_edition():
    assert strip_edition("Hybrid Theory (Bonus Edition)") == "Hybrid Theory"
    assert strip_edition("Meteora (20th Anniversary Edition) [Remastered]") == "Meteora"
    assert strip_edition("In the End - Single") == "In the End"
