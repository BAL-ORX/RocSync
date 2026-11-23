"""FusionTrack recordings in the legacy format and in the demo program's, on a synthetic board.

The board sits at a fixed pose and is sampled every 3 ms of tracker time, so its board time
is known for every frame. Each recording is written in every format, and must decode alike.
"""

import json
import sys

import pytest

from rocsync.board_profiles import BOARD_V2
from rocsync.ftk import FIDUCIAL_SUFFIX, MARKER_SUFFIX, process_ftk_recording, recording_path
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

# A frame whose lit ring LED is too close to the ring's ends is rejected
N_DECODABLE = sum(
    RING_EDGE_MARGIN <= _board_ms(i) % BOARD_V2.period <= BOARD_V2.period - 1 - RING_EDGE_MARGIN
    for i in range(N_FRAMES)
)


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


def write_demo(directory, stem, header=False, markers=True):
    """Write the demo program's pair of files, with its CRLF line endings."""
    marker_rows, fiducial_rows = [], []
    for i, (ts, points) in enumerate(FRAMES):
        stamp = f"0x{ts:016x}"
        geometry_id = str(BOARD_V2.ftk_marker_id)
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

    marker_path = directory / (stem + MARKER_SUFFIX)
    fiducial_path = directory / (stem + FIDUCIAL_SUFFIX)
    marker_path.write_bytes(text(marker_rows if markers else [], MARKER_HEADER).encode())
    fiducial_path.write_bytes(text(fiducial_rows, FIDUCIAL_HEADER).encode())
    return marker_path, fiducial_path


@pytest.fixture(scope="module")
def recordings(tmp_path_factory):
    directory = tmp_path_factory.mktemp("ftk")
    write_legacy(directory / "legacy.csv")
    for stem, header in (("plain", False), ("headed", True)):
        write_demo(directory, stem, header=header)
    write_demo(directory, "no_markers", markers=False)
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


def test_demo_recording_without_marker_rows_falls_back_to_the_constellation(recordings):
    result = _decode(recordings / ("no_markers" + FIDUCIAL_SUFFIX), board=BOARD_V2)

    assert result["n_pose_decodes"] == 0
    assert result["n_constellation_decodes"] == N_DECODABLE


def test_recording_without_records_fails(tmp_path):
    junk = tmp_path / "junk.csv"
    junk.write_text("a,b\n1,2\n")

    assert process_ftk_recording(junk) is None


def test_recording_path_names_a_demo_recording_by_its_fiducial_file(recordings):
    fiducials = recordings / ("plain" + FIDUCIAL_SUFFIX)

    assert recording_path(fiducials) == fiducials
    assert recording_path(recordings / ("plain" + MARKER_SUFFIX)) == fiducials
    assert recording_path(recordings / "legacy.csv") == recordings / "legacy.csv"


@pytest.mark.parametrize(
    "present, missing", [(MARKER_SUFFIX, FIDUCIAL_SUFFIX), (FIDUCIAL_SUFFIX, MARKER_SUFFIX)]
)
def test_a_demo_recording_needs_both_files(tmp_path, present, missing):
    (tmp_path / ("lonely" + present)).write_text("")

    with pytest.raises(FileNotFoundError, match="lonely" + missing):
        recording_path(tmp_path / ("lonely" + present))


def test_command_line_runs_a_demo_recording_once(recordings, tmp_path, monkeypatch):
    """A directory holds both files of a recording, which are one input."""
    directory = tmp_path / "recording"
    directory.mkdir()
    for suffix in (MARKER_SUFFIX, FIDUCIAL_SUFFIX):
        (directory / ("plain" + suffix)).write_bytes((recordings / ("plain" + suffix)).read_bytes())
    output = tmp_path / "out.json"
    monkeypatch.setattr(sys, "argv", ["rocsync", "-y", "-o", str(output), str(directory)])

    assert main() is None

    results = json.loads(output.read_text())
    assert list(results) == [str((directory / ("plain" + FIDUCIAL_SUFFIX)).resolve())]
    assert results[next(iter(results))]["type"] == "ftk"


def test_command_line_rejects_a_lone_demo_file(tmp_path, monkeypatch):
    lonely = tmp_path / ("lonely" + MARKER_SUFFIX)
    lonely.write_text("")
    monkeypatch.setattr(sys, "argv", ["rocsync", "-y", "-o", str(tmp_path / "o.json"), str(lonely)])

    assert main() == 1
