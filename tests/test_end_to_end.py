"""Build a small messy library, then plan / apply / undo it offline."""

import json
import shutil
from pathlib import Path

import pytest

from musicsorter.cli import main

from .conftest import make_audio, needs_ffmpeg

LP = {"artist": "Linkin Park", "album": "Hybrid Theory"}


@pytest.fixture
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "Music"
    make_audio(lib / "Linkin Park/Hybrid Theory/01 Papercut.flac", 20, tags={**LP, "title": "Papercut", "tracknumber": "1"})
    make_audio(lib / "Linkin Park/Hybrid Theory/08 In the End.flac", 30, tags={**LP, "title": "In the End", "tracknumber": "8/12"})
    (lib / "Linkin Park/Hybrid Theory/cover.jpg").write_bytes(b"jpeg")
    # Lower-quality copies of In the End: tagged 320 kbps, and an untagged 128 kbps file named by the downloader.
    make_audio(lib / "Downloads/Linkin Park - In The End.mp3", 30, "320k", {"artist": "Linkin Park", "title": "In The End"})
    make_audio(lib / "Downloads/Linkin Park - In the End [128kbps].mp3", 30, "128k")
    # Same artist and title but twice as long: an extended cut, which must be kept.
    make_audio(lib / "Downloads/In the End.mp3", 60, "320k", {"artist": "Linkin Park", "title": "In the End"}, freq=500)
    # Untagged file whose folder tells us the album.
    make_audio(lib / "Deftones - White Pony (2000) [FLAC]/Deftones - Change (In the House of Flies).mp3", 25, "192k")
    # A single, by release type, and a various-artists compilation.
    make_audio(lib / "misc/rhcp.ogg", 22, tags={"artist": "Red Hot Chili Peppers", "title": "Under the Bridge",
                                                "album": "Under the Bridge", "releasetype": "single"})
    make_audio(lib / "Now 45/01.mp3", 18, "192k", {"artist": "Somebody", "title": "Some Song", "album": "Now 45",
                                                   "albumartist": "Various Artists", "tracknumber": "1"})
    # A byte-for-byte copy of Papercut lying around elsewhere.
    (lib / "backup").mkdir()
    shutil.copy(lib / "Linkin Park/Hybrid Theory/01 Papercut.flac", lib / "backup/papercut copy.flac")
    return lib


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}


@needs_ffmpeg
def test_plan_apply_undo(library: Path, tmp_path: Path):
    before = _tree(library)
    plan_dir = tmp_path / "plan"
    assert main(["plan", str(library), "--dest", str(library), "--out", str(plan_dir), "--offline", "--no-cache"]) == 0
    assert _tree(library) == before, "planning must not touch the library"

    plan = json.loads((plan_dir / "plan.json").read_text())
    assert plan["settings"]["duplicates_dir"] == str(tmp_path / "Duplicates")

    assert main(["apply", str(plan_dir), "--yes"]) == 0
    assert _tree(library) == {
        "Linkin Park/Hybrid Theory/01 - Papercut.flac",
        "Linkin Park/Hybrid Theory/08 - In the End.flac",
        "Linkin Park/Hybrid Theory/cover.jpg",
        "Linkin Park/Singles/In the End.mp3",  # the 60-second extended cut survives
        "Deftones/White Pony/Change (In the House of Flies).mp3",
        "Red Hot Chili Peppers/Singles/Under the Bridge.ogg",
        "Various Artists/Now 45/01 - Somebody - Some Song.mp3",
    }
    dupes = tmp_path / "Duplicates"
    assert _tree(dupes) == {
        "Linkin Park/Singles/In The End.mp3",
        "Linkin Park/Singles/In the End (2).mp3",
        "Linkin Park/Hybrid Theory/01 - Papercut.flac",
        "duplicates_report.txt",
    }
    report = (dupes / "duplicates_report.txt").read_text()
    assert "lower quality" in report and "identical copy" in report
    assert "MP3 128kbps" in report and "FLAC 16bit/44.1kHz" in report
    assert not (library / "Downloads").exists(), "emptied folders are removed"

    # Running again finds nothing left to do.
    plan2 = tmp_path / "plan2"
    assert main(["plan", str(library), "--dest", str(library), "--out", str(plan2), "--offline", "--no-cache"]) == 0
    assert json.loads((plan2 / "plan.json").read_text())["moves"] == []

    assert main(["undo", str(plan_dir), "--yes"]) == 0
    assert _tree(library) == before
    assert not dupes.exists()


@needs_ffmpeg
def test_copy_mode_leaves_originals(library: Path, tmp_path: Path):
    before = _tree(library)
    sorted_lib = tmp_path / "Sorted"
    plan_dir = tmp_path / "plan"
    assert main(["plan", str(library), "--dest", str(sorted_lib), "--out", str(plan_dir), "--offline",
                 "--no-cache", "--copy"]) == 0
    assert main(["apply", str(plan_dir), "--yes"]) == 0
    assert _tree(library) == before
    assert "Linkin Park/Hybrid Theory/08 - In the End.flac" in _tree(sorted_lib)
    assert main(["undo", str(plan_dir), "--yes"]) == 0
    assert not sorted_lib.exists()
    assert _tree(library) == before


@needs_ffmpeg
def test_apply_never_overwrites(library: Path, tmp_path: Path):
    plan_dir = tmp_path / "plan"
    assert main(["plan", str(library), "--dest", str(library), "--out", str(plan_dir), "--offline", "--no-cache"]) == 0
    blocker = library / "Deftones/White Pony/Change (In the House of Flies).mp3"
    blocker.parent.mkdir(parents=True)
    blocker.write_bytes(b"something else")
    assert main(["apply", str(plan_dir), "--yes"]) == 2
    assert blocker.read_bytes() == b"something else"
    assert "target already exists" in (plan_dir / "apply_problems.txt").read_text()
