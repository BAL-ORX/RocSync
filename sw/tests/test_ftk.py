"""FusionTrack recordings in the legacy format and in the demo program's, on a synthetic board.

The board sits at a fixed pose and is sampled every 3 ms of tracker time, so its board time
is known for every frame. Each recording is written in every format, and must decode alike.
"""

import json
import sys
from pathlib import Path

import pytest

from rocsync.board_profiles import BOARD_V2
from rocsync.ftk import (
    FIDUCIAL_SUFFIX,
    MARKER_SUFFIX,
    process_ftk_recording,
    recording_name,
    recording_path,
)
from rocsync.main import main
from tests.test_fiducials import board_points_mm, place

N_FRAMES = 150
FRAME_US = 3000
FIRST_TS = 400_000_000  # the tracker clock at the first frame
FIRST_BOARD_MS = 20_000
RING_EDGE_MARGIN = 1  # a lit LED this close to the ring's ends cannot be decoded

MARKER_HEADER = (
    "Timestamp;Index;TrackingId;CoordX;CoordY;CoordZ;"
    + ";".join(f"R[{i},{j}]" for i in range(3) for j in range(3))
    + ";Registration;GeometryId;PresenceMask;Fid[0];Fid[1];Fid[2];Fid[3];"
)
FIDUCIAL_HEADER = (
    "Timestamp;Index;CoordX;CoordY;CoordZ;Epipolar;Triangulation;Probability;LeftIndex;RightIndex"
)


def _board_ms(frame):
    return FIRST_BOARD_MS + frame * FRAME_US // 1000


def _decodable(frame):
    """Whether the lit ring LED is far enough from the ring's ends to be decoded."""
    lit = _board_ms(frame) % BOARD_V2.period
    return RING_EDGE_MARGIN <= lit <= BOARD_V2.period - 1 - RING_EDGE_MARGIN


def _make_frames():
    """The tracker timestamp and fiducials of each frame."""
    frames = []
    for i in range(N_FRAMES):
        counter, lit = divmod(_board_ms(i), BOARD_V2.period)
        points, _, _ = place(board_points_mm(BOARD_V2, counter, [lit]))
        frames.append((FIRST_TS + i * FRAME_US, points))
    return frames


FRAMES = _make_frames()
_, ROTATION, ORIGIN = place(board_points_mm(BOARD_V2, 0, [10]))  # the pose of every frame

N_DECODABLE = sum(_decodable(i) for i in range(N_FRAMES))


def _xyz(point):
    return [f"{v:.4f}" for v in point]


def _rotation():
    return [f"{v:.7f}" for v in ROTATION.ravel()]


def write_legacy(path):
    rows = []
    for ts, points in FRAMES:
        marker_id = BOARD_V2.ftk_marker_id
        rows.append(["0", str(ts), "m", str(marker_id), *_xyz(ORIGIN), *_rotation(), "0.1"])
        rows += [["0", str(ts), "f", *_xyz(p), "0.05"] for p in points]
    path.write_text("".join(",".join(row) + "\n" for row in rows))


def write_demo(directory, stem, header=False, markers="rows", first_marker_frame=0):
    """Write the demo program's files, with its CRLF line endings.

    The marker file holds the board's marker rows from `first_marker_frame` on ("rows"),
    no rows ("empty"), or does not exist ("none").
    """
    marker_rows, fiducial_rows = [], []
    for i, (ts, points) in enumerate(FRAMES):
        stamp = f"0x{ts:016x}"
        geometry_id = str(BOARD_V2.ftk_marker_id)
        if i >= first_marker_frame:
            marker_rows.append(
                [
                    *[stamp, "0", str(i), *_xyz(ORIGIN), *_rotation(), "0.1"],
                    *[geometry_id, "31", "0", "1", "2", "3"],
                ]
            )
        fiducial_rows += [
            [stamp, str(j), *_xyz(p), "0.1", "0.05", "1.0000", "0", "0"]
            for j, p in enumerate(points)
        ]

    def text(rows, header_row):
        lines = ([header_row] if header else []) + [";".join(row) for row in rows]
        return "".join(line + "\r\n" for line in lines)

    if markers != "none":
        marker_text = text(marker_rows if markers == "rows" else [], MARKER_HEADER)
        (directory / (stem + MARKER_SUFFIX)).write_bytes(marker_text.encode())
    fiducial_text = text(fiducial_rows, FIDUCIAL_HEADER)
    (directory / (stem + FIDUCIAL_SUFFIX)).write_bytes(fiducial_text.encode())


@pytest.fixture(scope="module")
def recordings(tmp_path_factory):
    directory = tmp_path_factory.mktemp("ftk")
    write_legacy(directory / "legacy.csv")
    for stem, header in (("plain", False), ("headed", True)):
        write_demo(directory, stem, header=header)
    write_demo(directory, "no_marker_rows", markers="empty")
    write_demo(directory, "no_marker_file", markers="none")
    write_demo(directory, "late_markers", first_marker_frame=30)
    return directory


def _decode(path, **kwargs):
    """The fitted timeline of a recording that must have one, as the board decodes it."""
    result = process_ftk_recording(path, **kwargs)
    assert result is not None
    assert result["n_decoded"] == N_DECODABLE
    assert result["n_rejects"] == {"ring": N_FRAMES - N_DECODABLE}
    # Board milliseconds per tracker microsecond
    assert result["clock_rate"] == pytest.approx(1e-3, rel=1e-6)
    assert result["rmse_after"] == pytest.approx(0, abs=1e-6)
    return result


def test_legacy_recording(recordings):
    result = _decode(recordings / "legacy.csv")

    assert result["n_pose_decodes"] == N_DECODABLE


@pytest.mark.parametrize("stem", ["plain", "headed"])
def test_demo_recording_decodes_like_the_legacy_one(recordings, stem):
    """The header row is optional, and the recording is the same in either format."""
    assert _decode(recordings / (stem + FIDUCIAL_SUFFIX)) == _decode(recordings / "legacy.csv")


def test_either_file_of_a_demo_recording_stands_for_it(recordings):
    by_marker = _decode(recordings / ("plain" + MARKER_SUFFIX))
    by_fiducial = _decode(recordings / ("plain" + FIDUCIAL_SUFFIX))

    assert by_marker == by_fiducial


@pytest.mark.parametrize("stem", ["no_marker_rows", "no_marker_file"])
def test_demo_recording_without_markers_falls_back_to_the_constellation(recordings, stem):
    result = _decode(recordings / (stem + FIDUCIAL_SUFFIX), board=BOARD_V2)

    assert result["n_pose_decodes"] == 0
    assert result["n_constellation_decodes"] == N_DECODABLE


def test_demo_recording_without_markers_needs_a_board_version(recordings):
    assert process_ftk_recording(recordings / ("no_marker_file" + FIDUCIAL_SUFFIX)) is None


def test_recording_without_records_fails(tmp_path):
    junk = tmp_path / "junk.csv"
    junk.write_text("a,b\n1,2\n")

    assert process_ftk_recording(junk) is None


def _with_rows_in_the_middle(path, rows):
    """The text of the file `path` with `rows` inserted halfway, in its line ending."""
    lines = path.read_bytes().decode().splitlines(keepends=True)
    ending = "\r\n" if lines[0].endswith("\r\n") else "\n"
    middle = len(lines) // 2
    return "".join([*lines[:middle], *(row + ending for row in rows), *lines[middle:]])


@pytest.mark.parametrize(
    "name", ["legacy.csv", "plain" + FIDUCIAL_SUFFIX, "headed" + FIDUCIAL_SUFFIX]
)
def test_a_clean_recording_warns_about_no_skipped_rows(recordings, capsys, name):
    """A header row is not an unreadable one."""
    _decode(recordings / name)

    assert "Skipped" not in capsys.readouterr().out


def test_unreadable_rows_in_a_legacy_file_are_skipped_with_one_warning(
    recordings, tmp_path, capsys
):
    junk = tmp_path / "junk.csv"
    junk.write_bytes(
        _with_rows_in_the_middle(
            recordings / "legacy.csv", ["not,a,record", "0,abc,f,1,2,3,0.05"]
        ).encode()
    )

    assert process_ftk_recording(junk) == process_ftk_recording(recordings / "legacy.csv")
    assert capsys.readouterr().out.count("Skipped 2 unreadable rows: 2 in junk.csv") == 1


def test_unreadable_rows_in_demo_files_are_skipped_with_one_warning(recordings, tmp_path, capsys):
    junk_rows = {
        FIDUCIAL_SUFFIX: ["garbage", "0xZZ;0;1;2;3;0.1;0.05;1.0000;0;0"],
        MARKER_SUFFIX: ["0xZZ;0;0"],
    }
    for suffix, rows in junk_rows.items():
        junk = tmp_path / ("junk" + suffix)
        junk.write_bytes(_with_rows_in_the_middle(recordings / ("headed" + suffix), rows).encode())

    assert process_ftk_recording(tmp_path / ("junk" + FIDUCIAL_SUFFIX)) == process_ftk_recording(
        recordings / ("headed" + FIDUCIAL_SUFFIX)
    )
    expected = "Skipped 3 unreadable rows: 2 in junk_Fiducial.csv, 1 in junk_Marker.csv"
    assert capsys.readouterr().out.count(expected) == 1


def test_search_windows_count_from_the_first_frame_of_either_file(recordings):
    """The marker file starts at frame 30, but the recording at frame 0."""
    window = (0.0605, 0.3005)  # seconds from the first frame, between two 3 ms frames
    in_window = [i for i in range(N_FRAMES) if window[0] <= i * FRAME_US / 1e6 <= window[1]]

    result = process_ftk_recording(
        recordings / ("late_markers" + FIDUCIAL_SUFFIX), board=BOARD_V2, windows=[window]
    )

    assert result is not None
    assert result["n_frames"] == len(in_window)
    assert result["n_decoded"] == sum(_decodable(i) for i in in_window)


def test_recording_name():
    assert recording_name(Path("some/dir/rec_Fiducial.csv")) == "rec"
    assert recording_name(Path("some/dir/legacy.csv")) == "legacy"
    assert recording_name(Path("some/dir/video.mp4")) == "video"


def test_recording_path_names_a_demo_recording_by_its_fiducial_file(recordings):
    fiducials = recordings / ("plain" + FIDUCIAL_SUFFIX)

    assert recording_path(fiducials) == fiducials
    assert recording_path(recordings / ("plain" + MARKER_SUFFIX)) == fiducials
    assert recording_path(recordings / "legacy.csv") == recordings / "legacy.csv"


def test_a_lone_fiducial_file_is_a_recording(recordings):
    fiducials = recordings / ("no_marker_file" + FIDUCIAL_SUFFIX)

    assert recording_path(fiducials) == fiducials


def test_a_demo_recording_needs_its_fiducial_file(tmp_path):
    lonely = tmp_path / ("lonely" + MARKER_SUFFIX)
    lonely.write_text("")

    with pytest.raises(FileNotFoundError, match="lonely" + FIDUCIAL_SUFFIX):
        recording_path(lonely)


@pytest.mark.parametrize(
    "select",
    [
        lambda directory, marker, fiducial: [directory],
        lambda directory, marker, fiducial: [marker, fiducial],
        lambda directory, marker, fiducial: [marker, fiducial, directory],
        lambda directory, marker, fiducial: [marker],
        lambda directory, marker, fiducial: [fiducial],
    ],
    ids=["directory", "both files", "both files and directory", "marker file", "fiducial file"],
)
def test_command_line_runs_a_demo_recording_once(recordings, tmp_path, monkeypatch, select):
    """However its files are given, the recording is one input."""
    directory = tmp_path / "recording"
    directory.mkdir()
    for suffix in (MARKER_SUFFIX, FIDUCIAL_SUFFIX):
        (directory / ("plain" + suffix)).write_bytes((recordings / ("plain" + suffix)).read_bytes())
    marker, fiducial = (
        directory / ("plain" + suffix) for suffix in (MARKER_SUFFIX, FIDUCIAL_SUFFIX)
    )
    output = tmp_path / "out.json"
    paths = [str(path) for path in select(directory, marker, fiducial)]
    monkeypatch.setattr(sys, "argv", ["rocsync", "-y", "-o", str(output), *paths])

    assert main() is None

    results = json.loads(output.read_text())
    assert list(results) == [str(fiducial.resolve())]
    assert results[str(fiducial.resolve())]["type"] == "ftk"


def test_command_line_names_the_debug_directory_after_the_recording(
    recordings, tmp_path, monkeypatch
):
    debug = tmp_path / "debug"
    arguments = ["rocsync", "-y", "--debug", str(debug), "-o", str(tmp_path / "out.json")]
    # A few frames are enough: only the directory's name is checked
    arguments += [
        "--window",
        "0:00:00",
        "0:00:00.03",
        str(recordings / ("plain" + FIDUCIAL_SUFFIX)),
    ]
    monkeypatch.setattr(sys, "argv", arguments)

    main()

    assert [path.name for path in debug.iterdir()] == ["plain"]


def test_command_line_rejects_a_lone_demo_file(tmp_path, monkeypatch):
    lonely = tmp_path / ("lonely" + MARKER_SUFFIX)
    lonely.write_text("")
    monkeypatch.setattr(sys, "argv", ["rocsync", "-y", "-o", str(tmp_path / "o.json"), str(lonely)])

    assert main() == 1
