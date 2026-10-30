"""Lookup logic against canned API responses (no network)."""

from musicsorter.cache import Cache
from musicsorter.lookup import Deezer, MusicBrainz


class FakeHttp:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get_json(self, url, params=None, form=None):
        self.urls.append(url)
        for prefix, payload in self.responses.items():
            if url.startswith(prefix):
                return payload
        raise AssertionError(f"unexpected request {url}")


def _release(title, primary, secondary=(), date="", artist="Linkin Park", status="Official", number="8"):
    return {
        "title": title, "status": status, "date": date,
        "artist-credit": [{"name": artist}],
        "release-group": {"title": title, "primary-type": primary, "secondary-types": list(secondary)},
        "media": [{"position": 1, "track-offset": int(number) - 1, "track": [{"number": number}]}],
    }


def _recording(releases, length=216000, disambiguation="", title="In the End"):
    return {"score": 100, "title": title, "length": length, "disambiguation": disambiguation,
            "artist-credit": [{"name": "Linkin Park"}], "releases": releases}


def test_musicbrainz_prefers_the_studio_album_over_singles_compilations_and_live():
    http = FakeHttp({"https://musicbrainz.org/": {"recordings": [
        _recording([_release("Live in Texas", "Album", ["Live"], "2003")], disambiguation="live, 2003"),
        _recording([_release("Now 2001", "Album", ["Compilation"], "2001", artist="Various Artists")]),
        _recording([_release("In the End", "Single", date="2001-10-08", number="1"),
                    _release("Hybrid Theory (Bonus Edition)", "Album", date="2000-10-24"),
                    _release("Hybrid Theory", "Album", date="2000-10-24", status="Bootleg")]),
    ]}})
    match = MusicBrainz(http).search("Linkin Park", "In the End", 216)
    assert (match.album, match.track, match.year, match.single) == ("Hybrid Theory", 8, "2000", False)


def test_musicbrainz_reports_a_single_when_no_album_exists():
    http = FakeHttp({"https://musicbrainz.org/": {"recordings": [
        _recording([_release("New Divide", "Single", date="2009", number="1")], length=268000, title="New Divide"),
    ]}})
    match = MusicBrainz(http).search("Linkin Park", "New Divide", 268)
    assert match.single and not match.album


def test_musicbrainz_ignores_other_cuts():
    http = FakeHttp({"https://musicbrainz.org/": {"recordings": [
        _recording([_release("Hybrid Theory", "Album", date="2000")], length=420000),
    ]}})
    assert MusicBrainz(http).search("Linkin Park", "In the End", 216) is None


def test_deezer_free_text_search_from_a_file_name():
    http = FakeHttp({
        "https://api.deezer.com/search": {"data": [
            {"id": 1, "title": "In the End", "title_short": "In the End", "duration": 216,
             "artist": {"name": "Linkin Park"}, "album": {"id": 10}},
        ]},
        "https://api.deezer.com/album/10": {"title": "Hybrid Theory (Bonus Edition)", "record_type": "album",
                                            "release_date": "2000-10-24", "artist": {"name": "Linkin Park"},
                                            "tracks": {"data": [{"id": n} for n in range(-6, 2)]}},
    })
    match = Deezer(http, Cache(None)).search("linkin park in the end", "", "", 217)
    assert (match.artist, match.album, match.track) == ("Linkin Park", "Hybrid Theory", 8)


def test_deezer_rejects_name_matches_with_the_wrong_length():
    http = FakeHttp({"https://api.deezer.com/search": {"data": [
        {"id": 1, "title": "In the End", "duration": 216, "artist": {"name": "Somebody"}, "album": {"id": 10}},
    ]}})
    assert Deezer(http, Cache(None)).search("In the End", "", "In the End", 300) is None
