"""Read an Atracsys FusionTrack CSV recording and fit its board timeline.

A recording is either the Atracsys demo program's export, a ``<name>_Fiducial.csv`` file
and, if the tracker followed a registered marker, a ``<name>_Marker.csv`` file, or a single
legacy CSV file that holds both kinds of rows. The file names tell the two apart.

Each frame is decoded from its fiducials by ``rocsync.fiducials.decode_fiducials``: first
on the plane of a registered board marker, if the frame has one, then on the corner-LED
constellation if that fails.

matplotlib, tqdm and ``rocsync.timeline`` are imported where they are used, so importing
this module stays light.

``--window`` search spans are measured from the first frame's ``ftk_timestamp``, since the
tracker clock starts at its own boot, not at the start of the recording.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterator, Mapping
from operator import itemgetter
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import numpy as np

from rocsync.board_profiles import ALL_PROFILES, BoardProfile
from rocsync.fiducials import MAX_FIDUCIALS, decode_fiducials, plane_from_rotation
from rocsync.printer import errprint, print, warnprint
from rocsync.recording_statistics import print_statistics, warn_about_statistics
from rocsync.timecode import resolve_windows

if TYPE_CHECKING:
    from tqdm import tqdm

FIT_RESIDUAL_THRESHOLD_MS = 10  # RANSAC inlier band; the tracker is not frame-periodic
FTK_TICK_MS = 0.001  # the FusionTrack reports its frame clock in microseconds
FTK_TICK_S = FTK_TICK_MS / 1000.0

MARKER_SUFFIX = "_Marker.csv"
FIDUCIAL_SUFFIX = "_Fiducial.csv"


class _Layout(NamedTuple):
    """How the rows of one recording file are laid out."""

    delimiter: str
    timestamp_column: int
    timestamp_base: int
    columns: dict[str, list[str]]  # the column names of each row type
    type_column: int | None = None  # holds a row's type; None for a file of a single type


# Legacy format: comma-separated, no header, marker and fiducial rows in one file
marker_format = [
    "host_timestamp",
    "ftk_timestamp",
    "type",
    "marker_id",
    "x_position",
    "y_position",
    "z_position",
    "r00",
    "r01",
    "r02",
    "r10",
    "r11",
    "r12",
    "r20",
    "r21",
    "r22",
    "registration_error",
]

fiducial_format = [
    "host_timestamp",
    "ftk_timestamp",
    "type",
    "x_position",
    "y_position",
    "z_position",
    "triangulation_error",
]

# Demo program format: semicolon-separated, hexadecimal timestamps, one file per row type,
# with or without a header row. ``marker_id`` is the geometry id of the tracked marker.
demo_marker_format = [
    "ftk_timestamp",
    "index",
    "tracking_id",
    "x_position",
    "y_position",
    "z_position",
    "r00",
    "r01",
    "r02",
    "r10",
    "r11",
    "r12",
    "r20",
    "r21",
    "r22",
    "registration_error",
    "marker_id",
    "presence_mask",
    "fiducial0",
    "fiducial1",
    "fiducial2",
    "fiducial3",
]

demo_fiducial_format = [
    "ftk_timestamp",
    "index",
    "x_position",
    "y_position",
    "z_position",
    "epipolar_error",
    "triangulation_error",
    "probability",
    "left_index",
    "right_index",
]

_LEGACY = _Layout(
    delimiter=",",
    timestamp_column=1,
    timestamp_base=10,
    columns={"m": marker_format, "f": fiducial_format},
    type_column=2,
)
_DEMO_MARKERS = _Layout(
    delimiter=";", timestamp_column=0, timestamp_base=16, columns={"m": demo_marker_format}
)
_DEMO_FIDUCIALS = _Layout(
    delimiter=";", timestamp_column=0, timestamp_base=16, columns={"f": demo_fiducial_format}
)


def _demo_files(path: Path) -> tuple[Path | None, Path] | None:
    """The marker file, if there is one, and the fiducial file of the demo-format
    recording that `path` is one of. None for any other file.

    A recording without markers is possible, one without fiducials is not, so
    FileNotFoundError is raised if the fiducial file is missing.
    """
    for suffix in (MARKER_SUFFIX, FIDUCIAL_SUFFIX):
        if path.name.endswith(suffix):
            stem = path.name.removesuffix(suffix)
            break
    else:
        return None

    fiducials = path.with_name(stem + FIDUCIAL_SUFFIX)
    if not fiducials.is_file():
        raise FileNotFoundError(
            f"{fiducials} not found: a demo-format recording needs a {FIDUCIAL_SUFFIX} file"
        )
    markers = path.with_name(stem + MARKER_SUFFIX)
    return (markers if markers.is_file() else None), fiducials


def recording_path(path: Path) -> Path:
    """The path that identifies the recording `path` belongs to.

    Either file of a demo-format recording stands for it, as its fiducial file; any other
    file is a legacy recording by itself. Raises FileNotFoundError if the fiducial file of
    a demo-format recording is missing.
    """
    files = _demo_files(path)
    return files[1] if files else path


def _recording_sources(path: Path) -> list[tuple[Path, _Layout]]:
    """The files of the recording `path` belongs to, with how each is laid out."""
    files = _demo_files(path)
    if files is None:
        return [(path, _LEGACY)]
    markers, fiducials = files
    sources = [(fiducials, _DEMO_FIDUCIALS)]
    if markers is not None:
        sources.append((markers, _DEMO_MARKERS))
    return sources


def recording_name(path: Path) -> str:
    """The name of a recording or other input file: its file name without the extension,
    and for a demo-format recording without the ``_Fiducial`` suffix as well.
    """
    name = path.name.removesuffix(FIDUCIAL_SUFFIX)
    return path.stem if name == path.name else name


def _parse_row(line: str, layout: _Layout) -> tuple[int, str, list[str]] | None:
    """``(ftk_timestamp, row_type, fields)`` of a record row, or None if the row is none.

    A header, a short row, a row of an unknown type and a row with an unreadable
    timestamp are not records.
    """
    fields = [f.strip() for f in line.strip().split(layout.delimiter)]
    if layout.type_column is None:
        row_type = next(iter(layout.columns))
    elif layout.type_column < len(fields):
        row_type = fields[layout.type_column]
    else:
        return None

    columns = layout.columns.get(row_type)
    if columns is None or len(fields) < len(columns):
        return None
    try:
        timestamp = int(fields[layout.timestamp_column], layout.timestamp_base)
    except ValueError:
        return None
    return timestamp, row_type, fields[: len(columns)]


def _iter_records(
    path: Path, layout: _Layout, pbar: tqdm
) -> Iterator[tuple[int, str, dict[str, str]]]:
    """Yield ``(ftk_timestamp, row_type, record)`` for each record row of a file."""
    with open(path) as file:
        for line in file:
            pbar.update(1)
            row = _parse_row(line, layout)
            if row is not None:
                timestamp, row_type, fields = row
                yield timestamp, row_type, dict(zip(layout.columns[row_type], fields, strict=True))


def _iter_frames(
    sources: list[tuple[Path, _Layout]], pbar: tqdm
) -> Iterator[tuple[int, list[dict[str, str]], list[dict[str, str]]]]:
    """Group a recording into frames: ``(ftk_timestamp, markers, fiducials)``.

    The tracker writes one record per detected object per acquisition, in timestamp
    order, so a group is flushed as soon as the timestamp changes. The files of a
    demo-format recording are merged on their timestamps.
    """
    records = heapq.merge(
        *(_iter_records(path, layout, pbar) for path, layout in sources), key=itemgetter(0)
    )
    seen = set()
    ts, markers, fiducials = None, [], []
    for record_ts, row_type, record in records:
        if record_ts != ts:
            if ts is not None:
                yield ts, markers, fiducials
            if record_ts in seen:
                warnprint(f"Non-contiguous ftk_timestamp {record_ts}; frame may be split")
            seen.add(record_ts)
            ts, markers, fiducials = record_ts, [], []

        (markers if row_type == "m" else fiducials).append(record)

    if ts is not None:
        yield ts, markers, fiducials


def _frame_plane(marker):
    """The board plane from a registered marker's own reported position and rotation."""
    position = np.array(
        [float(marker["x_position"]), float(marker["y_position"]), float(marker["z_position"])]
    )
    rotation = np.array(
        [
            [marker["r00"], marker["r01"], marker["r02"]],
            [marker["r10"], marker["r11"], marker["r12"]],
            [marker["r20"], marker["r21"], marker["r22"]],
        ],
        dtype=float,
    )
    return plane_from_rotation(position, rotation)


def plot_timechart(x, y, x_range, y_pred, debug_dir):
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots()
    ax.scatter(x, y, color="blue", label="Measurements", marker=".")
    ax.plot(x_range, y_pred, color="r", label="Fitted Model")
    ax.set_xlabel("Device timestamp")
    ax.set_ylabel("RocSync timestamp [ms]")
    ax.set_title("Frame timing")
    ax.ticklabel_format(style="plain", useOffset=False)
    ax.legend(loc="upper left")
    ax.grid(True)
    fig.savefig(f"{debug_dir}/timestamps.png")
    plt.close(fig)


def fit_ftk_timestamps(
    timestamps: dict[int, tuple[int, int]],
    frame_times: dict[int, int],
    debug_dir=None,
    window_frame_times: list[dict[int, int]] | None = None,
    timeline_windowed: bool = False,
    source_extent: tuple[int, int] | None = None,
):
    """Fit board time against the tracker's own clock and describe the result.

    Thin wrapper around `summarize_timeline` -- the fit, dropout detection and
    reporting are exactly what the video path uses, just fed the tracker's raw
    microsecond ticks (`FTK_TICK_MS`) and a fixed inlier band instead of one derived
    from a frame period, since the tracker is not frame-periodic the way a container
    is. Raises ValueError when the timeline cannot be fitted.

    `window_frame_times`/`timeline_windowed` are forwarded to `summarize_timeline` so a
    gap between two disjoint `--window` spans is not counted as a dropout, and
    `source_extent` so the fit is judged at the recording's first and last frame.
    """
    from rocsync.timeline import summarize_timeline

    statistics, fit, _, _, _ = summarize_timeline(
        timestamps,
        frame_times,
        n_frames=len(frame_times),
        fps=None,
        window_frame_times=window_frame_times,
        timeline_windowed=timeline_windowed,
        source_tick_ms=FTK_TICK_MS,
        residual_threshold=FIT_RESIDUAL_THRESHOLD_MS,
        max_trials=10000,  # more trials for more consistent results
        source_extent=source_extent,
    )

    if debug_dir is not None:
        x = np.array(fit.order).reshape(-1, 1)
        y = np.array([timestamps[k][0] for k in fit.order])
        x_range = np.array([np.min(x), np.max(x)]).reshape(-1, 1)
        plot_timechart(x, y, x_range, fit.predict(x_range), debug_dir)
    return statistics


def _window_label(windows):
    """The `--window` spans, formatted like `process_video_window`'s own label."""
    return ", ".join(
        f"[{start:.3f}s, " + ("end]" if math.isinf(end) else f"{end:.3f}s]")
        for start, end in windows
    )


def _scan_extent(sources: list[tuple[Path, _Layout]]) -> tuple[int, int, int]:
    """Line count plus the first and last ``ftk_timestamp`` over a recording's files, in
    the one pass the progress bar's total already costs. The tracker writes in timestamp
    order, so these bound the recording. Raises ValueError if the files hold no record.

    Warns once about the rows that are no records, apart from blank lines and the first
    row of a file, which may be a header.
    """
    total_lines = 0
    extents = []
    skipped = {}
    for path, layout in sources:
        first_ts = last_ts = None
        n_skipped = 0
        with open(path) as file:
            for i, line in enumerate(file):
                total_lines += 1
                row = _parse_row(line, layout)
                if row is None:
                    if i > 0 and line.strip():
                        n_skipped += 1
                    continue
                if first_ts is None:
                    first_ts = row[0]
                last_ts = row[0]
        if first_ts is not None and last_ts is not None:
            extents.append((first_ts, last_ts))
        if n_skipped:
            skipped[path.name] = n_skipped

    if not extents:
        names = ", ".join(path.name for path, _ in sources)
        raise ValueError(f"no FusionTrack records found in {names}")
    if skipped:
        detail = ", ".join(f"{n} in {name}" for name, n in skipped.items())
        warnprint(f"Skipped {sum(skipped.values())} unreadable rows: {detail}")
    return total_lines, min(first for first, _ in extents), max(last for _, last in extents)


def process_ftk_recording(
    filename: str | Path,
    debug_dir=None,
    board: BoardProfile | None = None,
    max_fiducials: int = MAX_FIDUCIALS,
    marker_ids: Mapping[int, BoardProfile] | None = None,
    windows: list[tuple[float, float]] | None = None,
) -> dict | None:
    """Fit a board timeline from a FusionTrack recording.

    ``filename`` is a legacy CSV file, or either file of a demo-format recording.
    ``marker_ids`` maps registered marker ids to the board they track, defaulting to each
    profile's ``ftk_marker_id``. A frame with such a marker is decoded as that board;
    any other frame as ``board``, or skipped if ``board`` is None. ``windows`` restricts
    the search to time spans from the first frame; None or empty searches everything.
    """
    from tqdm import tqdm

    if marker_ids is None:
        marker_ids = {p.ftk_marker_id: p for p in ALL_PROFILES}

    sources = _recording_sources(Path(filename))
    try:
        total_lines, first_ts, last_ts = _scan_extent(sources)
    except ValueError as e:
        errprint(f"Error: {e}")
        return None

    # Whether the reported span and dropouts describe the file or just the windows
    timeline_windowed = bool(windows)
    try:
        windows = resolve_windows(windows, lambda: (last_ts - first_ts) * FTK_TICK_S)
    except ValueError as e:
        errprint(f"Error: Unable to resolve the search windows: {e}")
        return None

    timestamps = {}
    frame_times = {}
    window_frame_times = [{} for _ in windows]
    current = 0  # windows and frames are both in ascending time order
    n_marker_frames = 0
    n_pose_decodes = 0
    n_constellation_decodes = 0
    n_rejects = {}

    window_label = _window_label(windows)

    def describe():
        return (
            f"Analyzing frames in time window {window_label} --> Found {len(timestamps)} timestamps"
        )

    with tqdm(total=total_lines, desc=describe(), position=1) as pbar:
        for ftk_timestamp, markers, fiducials in _iter_frames(sources, pbar):
            # Window times run from the first frame, not from the tracker's boot
            offset_s = (ftk_timestamp - first_ts) * FTK_TICK_S
            while current < len(windows) and offset_s > windows[current][1]:
                current += 1
            if current == len(windows) or offset_s < windows[current][0]:
                continue

            # Every reported frame counts towards the frame count and dropouts
            frame_times[ftk_timestamp] = ftk_timestamp
            window_frame_times[current][ftk_timestamp] = ftk_timestamp

            registered = next((m for m in markers if int(m["marker_id"]) in marker_ids), None)
            if registered is not None:
                n_marker_frames += 1
                frame_board = marker_ids[int(registered["marker_id"])]
            else:
                frame_board = board
            if frame_board is None:
                continue

            points_3d = np.array(
                [
                    [float(f["x_position"]), float(f["y_position"]), float(f["z_position"])]
                    for f in fiducials
                ]
            )

            ax = None
            fig = None
            if debug_dir is not None and len(points_3d) >= 4:
                import matplotlib.pyplot as plt

                fig, ax = plt.subplots(figsize=(6, 6))
                ax.invert_yaxis()
                ax.grid(True)
                ax.set_aspect("equal")

            result = None
            source = "pose"
            if registered is not None:
                plane = _frame_plane(registered)
                result = decode_fiducials(
                    points_3d, frame_board, plane, max_fiducials=max_fiducials, ax=ax
                )
                if result.reject is None:
                    n_pose_decodes += 1

            # A pose reject falls through to the constellation search
            if result is None or result.reject is not None:
                source = "constellation"
                result = decode_fiducials(
                    points_3d, frame_board, None, max_fiducials=max_fiducials, ax=ax
                )
                if result.reject is None:
                    n_constellation_decodes += 1

            if fig is not None and ax is not None:
                import matplotlib.pyplot as plt

                ax.set_title(f"{frame_board.name} {result.reject or source}")
                fig.savefig(f"{debug_dir}/{ftk_timestamp}.png", bbox_inches="tight")
                plt.close(fig)

            if result.reject is None:
                timestamps[ftk_timestamp] = (result.ring_start, result.ring_end)
                pbar.set_description(describe())
            else:
                n_rejects[result.reject] = n_rejects.get(result.reject, 0) + 1

    stats = {
        "n_marker_frames": n_marker_frames,
        "n_decoded": len(timestamps),
        "n_pose_decodes": n_pose_decodes,
        "n_constellation_decodes": n_constellation_decodes,
        "n_rejects": n_rejects,
    }
    if board is not None:
        stats["board_version"] = board.name

    if timeline_windowed and not frame_times:
        errprint("Error: no frame of this recording falls inside the requested search window(s).")
        _print_decode_stats(stats)
        return None

    if len(timestamps) > 0:
        try:
            statistics = fit_ftk_timestamps(
                timestamps,
                frame_times,
                debug_dir,
                window_frame_times,
                timeline_windowed,
                source_extent=(first_ts, last_ts),
            )
        except ValueError as e:
            errprint(f"Error: Unable to fit the FTK timeline: {e}")
            _print_decode_stats(stats)
            return None

        _print_decode_stats(stats)
        warn_about_statistics(statistics)
        print_statistics(statistics)
        return {**statistics.to_dict(), **stats}

    if board is None and n_marker_frames == 0:
        errprint(
            "Error: no registered geometry found in this recording; "
            "pass --board-version to decode from raw fiducials"
        )
    else:
        errprint("Error: no frame in this recording decoded a timestamp.")
    _print_decode_stats(stats)
    return None


def _print_decode_stats(stats: dict):
    """Print how many frames decoded, from which plane, and why the others were rejected."""
    print(
        f"Marker frames: {stats['n_marker_frames']}, decoded: {stats['n_decoded']} "
        f"(pose: {stats['n_pose_decodes']}, constellation: {stats['n_constellation_decodes']})"
    )
    for reason, count in stats["n_rejects"].items():
        print(f"  Rejected {count} frame(s): {reason}")
