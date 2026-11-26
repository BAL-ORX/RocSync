# RocSync decoding software

This folder contains the Python application for detecting and decoding the RocSync device in videos and images where it is visible.

## How it works:
1. **Find ArUco marker**: Detect the ArUco marker to determine the approximate position and orientation.
2. **Coarse homographic reprojection**: Use the detected marker's corners to perform a coarse homographic reprojection of the image.
3. **Locate corner LEDs**: Identify the corner LEDs in the reprojected image.
4. **Accurate reprojection**: Use the corner LEDs to refine the reprojection for higher accuracy.
5. **Decode LEDs**: Decode the circle and binary counter LEDs by thresholding their general areas to obtain an exact timestamp.
6. **Timestamp fitting**: If the input was a video, perform robust linear regression on all extracted timestamps to reject outliers and estimate timestamps for all frames.

## Installation
RocSync uses [uv](https://docs.astral.sh/uv/) as its package manager. Install it first if you
do not have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then install RocSync as a tool, which puts the `rocsync` and `rocsync-align` commands on your
`PATH` in their own isolated environment (the `rocsync-annotate`, `rocsync-validate`, and
`rocsync-evaluate` benchmark tools come along with them):

```bash
git clone https://github.com/BAL-ORX/RocSync.git
uv tool install ./RocSync/sw
```

To run it once without installing anything permanently:

```bash
uvx --from ./RocSync/sw rocsync -h
```

<details>
<summary>Installing with pip instead</summary>

```bash
git clone https://github.com/BAL-ORX/RocSync.git
pip install ./RocSync/sw
```
</details>

Time-syncing videos with `rocsync-align` additionally requires `ffmpeg` on your `PATH`; hardware
encoding needs an `ffmpeg` built with `hevc_nvenc`.

## Usage
```bash
rocsync [OPTIONS] PATH [PATH ...]
```

Each `PATH` is a video, an image, an FTK tracker recording (`.csv`, see
[FusionTrack recordings](#fusiontrack-recordings)), or a directory containing them. Results for
every input are collected into a single JSON file, keyed by path, with a `type` of `video`,
`image`, or `ftk`.

Infrared input needs an explicit `--board-version`: auto-detection identifies the board from its
ArUco marker, which is not visible in IR.

```bash
rocsync -c ir --board-version v2 ir_recording.mp4
```

| Option | Description |
| --- | --- |
| `-c, --camera_type {rgb,ir}` | Type of camera (default: `rgb`; does not apply to FusionTrack recordings) |
| `-i, --interval SECONDS` | Analyze at least one frame every SECONDS, placed relative to keyframes so skipped frames need not be decoded (default: `1`; `0` = every frame; videos only) |
| `-e, --export_frames DIRECTORY` | Directory to store all raw frames as PNGs with timestamp (videos only) |
| `-o, --output FILE` | JSON file to store results (default: `output.json`) |
| `-y, --yes` | Do not ask for confirmation when processing multiple files |
| `--debug DIRECTORY` | Directory to store debug images (very slow) |
| `--board-version {auto,v1,v2}` | Board hardware revision (default: `auto`, detected from the ArUco marker ID; also used for a FusionTrack CSV whose recording carries no registered geometry) |
| `--max-fiducials N` | Skip a FusionTrack frame with more than N raw fiducials as too cluttered to search (default: `64`); the search cost is quadratic in this |
| `--ftk-marker-id N` | FusionTrack geometry id of the board's registered marker; requires `--board-version` (default: `240` for v1, `241` for v2) |
| `--try-hard` | Relaxed mode: refine the homography from partial corner LED detections and accept the board at any distance (videos and images only) |
| `--window START END` | Time span to search (videos and FusionTrack recordings), in `hh:mm:ss` format with optionally fractional seconds; `end` is the end of the recording and `end-hh:mm:ss` counts back from it, e.g. `--window end-0:00:30 end`. For a FusionTrack recording the span is measured from its first frame. Repeat for several spans; overlapping ones are merged (default: whole recording) |
| `--recurse_in_dir` | Recursively search for videos, images and FusionTrack recordings in directories |

Run `rocsync -h` for the authoritative list.

Results for the inputs that could be time-synced are written either way; the exit status is
non-zero if any input could not be, so a script can tell.


## Example
```
$ rocsync recording.MP4
Working on recording.MP4
Analyzing frames: 100%|████████████████████| 6520/6520 [02:21<00:00, 45.93it/s]
-----------------------------------------------------------------------
Number of considered frames:                                       1885
Number of rejected outliers:                                          3
R2 (before/after outlier rejection):                      0.9998/1.0000
RMSE (before/after outlier rejection):                     1.83/0.44 ms
Dropped frames:                              0 in 0 gap(s), max 0.000 s
First frame:                                                   -4.333 s
Last frame:                                                    22.858 s
Framerate (nominal/measured):                       239.760/239.740 fps
Clock rate (board/container):                                 1.000074x
Duration (container/board):                 27.190/27.192 s (Δ=2.00 ms)
Exposure time (mean/min/max/std):                3.97/3.00/5.00/0.42 ms
-----------------------------------------------------------------------
Processing files: 100%|█████████████████████████| 1/1 [02:22<00:00, 142.04s/it]
```

In the terminal the five checked statistics — from `Number of considered frames` down to
`Dropped frames` — are colour-coded green when they pass their sanity threshold and red when
they do not. All times are board times, which is why the first frame here is negative: the
recording started before the board's clock reached zero.

## FusionTrack recordings
`rocsync` also reads a recording of an Atracsys FusionTrack tracker that has the board in view.
The board is decoded from the 3D positions of its infrared LEDs, so `-c/--camera_type` has no
effect.

A recording is the Atracsys demo program's export: a `<name>_Fiducial.csv` file and, if the
tracker followed a registered marker, a `<name>_Marker.csv` file. Pass either file and `rocsync`
finds the other next to it. Without a marker file, the board is found among the fiducials, which
needs `--board-version`; a marker file without its fiducial file is an error. A directory
argument counts each recording once, and the results are keyed by the fiducial file's path. Keep
the demo program's file names: they are what tells `rocsync` a CSV file is such an export, and
not a [legacy recording](#legacy-format).

```bash
rocsync recording_Fiducial.csv                      # board taken from its registered marker
rocsync --board-version v2 recording_Marker.csv     # also decode frames without a registered marker
rocsync --board-version v2 lone_Fiducial.csv        # no marker file: find the board among the fiducials
```

### File format
Both files have one row per detected object, with fields separated by semicolons. Some versions
of the demo program write a header row; it is skipped, as are rows that are too short or whose
timestamp is unreadable, and the columns are read by position. `rocsync` warns once about how
many rows it skipped, not counting blank lines or a file's first row. A timestamp is hexadecimal
(`0x…`) microseconds on the tracker's frame clock. The rows of a frame share one, and each file
lists frames in ascending timestamp order. All positions are in millimetres.

<!-- The tables mirror `demo_marker_format` and `demo_fiducial_format` in `rocsync/ftk.py`. -->

Marker file, 22 columns, named by the demo program's header:

| # | Column | Use |
| --- | --- | --- |
| 1 | `Timestamp` | groups rows into frames and is fitted against board time |
| 2-3 | `Index`, `TrackingId` | ignored |
| 4-6 | `CoordX`, `CoordY`, `CoordZ` | origin of the board plane |
| 7-15 | `R[0,0]`, `R[0,1]`, `R[0,2]`, `R[1,0]`, `R[1,1]`, `R[1,2]`, `R[2,0]`, `R[2,1]`, `R[2,2]` | 3×3 rotation matrix, row by row; its first two columns are the board's in-plane axes |
| 16 | `Registration` | ignored |
| 17 | `GeometryId` | matched against the registered marker ids below |
| 18-22 | `PresenceMask`, `Fid[0]`, `Fid[1]`, `Fid[2]`, `Fid[3]` | ignored |

Fiducial file, 10 columns:

| # | Column | Use |
| --- | --- | --- |
| 1 | `Timestamp` | as above |
| 2 | `Index` | ignored |
| 3-5 | `CoordX`, `CoordY`, `CoordZ` | position of one LED |
| 6-10 | `Epipolar`, `Triangulation`, `Probability`, `LeftIndex`, `RightIndex` | ignored |

The marker file lists only the frames in which the tracker recognised a registered marker, the
fiducial file every frame.

### Legacy format
A single `.csv` file with any other name is read as a legacy recording, which `rocsync` supports
for existing recordings. The file has no header row and its fields are separated by commas. Every
row is one detected object: a registered marker (`m`) or a single fiducial (`f`), as given by the
third column. Rows of any other type, and rows with fewer columns than their type needs, are
skipped and counted in the same warning; extra trailing columns are ignored. The rows of one
acquisition sit next to each other and share an integer `ftk_timestamp` in microseconds, and
acquisitions follow in ascending timestamp order. Any `.csv` file in a directory argument is
taken to be such a recording, unless it is named like a demo export; a file that is not one is
reported as failed.

<!-- The tables mirror `marker_format` and `fiducial_format` in `rocsync/ftk.py`. -->

Marker rows, 17 columns:

| # | Column | Use |
| --- | --- | --- |
| 1 | `host_timestamp` | ignored |
| 2 | `ftk_timestamp` | integer microseconds on the tracker's frame clock; groups rows into frames and is fitted against board time |
| 3 | `type` | `m` |
| 4 | `marker_id` | matched against the registered marker ids below |
| 5-7 | `x_position`, `y_position`, `z_position` | origin of the board plane |
| 8-16 | `r00`, `r01`, `r02`, `r10`, `r11`, `r12`, `r20`, `r21`, `r22` | 3×3 rotation matrix, row by row; its first two columns are the board's in-plane axes |
| 17 | `registration_error` | ignored |

Fiducial rows, 7 columns:

| # | Column | Use |
| --- | --- | --- |
| 1 | `host_timestamp` | ignored |
| 2 | `ftk_timestamp` | as above |
| 3 | `type` | `f` |
| 4-6 | `x_position`, `y_position`, `z_position` | position of one LED |
| 7 | `triangulation_error` | ignored |

### How a frame is decoded
- A frame with a registered marker takes the board plane from that marker's pose, and the board
  revision from its geometry id: `240` for v1, `241` for v2. These are the geometry ids in
  [`misc/ftk_geometry_rev1.ini`](../misc/ftk_geometry_rev1.ini) and
  [`misc/ftk_geometry_rev2.ini`](../misc/ftk_geometry_rev2.ini); register the one that matches
  your board with the tracker. `--ftk-marker-id N` replaces both ids with `N`, for the
  `--board-version` board.
- A frame without a registered marker, or one whose pose decode fails, is searched for the four
  corner LEDs among its fiducials instead. This needs `--board-version`; without it, such frames
  are skipped, and a recording with no registered marker at all fails with an error.
- A frame with more than `--max-fiducials` fiducials is skipped as too cluttered.

`--window` spans count from the first frame's `ftk_timestamp`, since the tracker's clock starts
when it boots, not when the recording does.

## Time-syncing videos
`rocsync-align` turns the JSON that `rocsync` wrote into actually aligned video files. It reads
each entry's fitted clock, trims every video to the span they all share and puts its timestamps on
board time, so the outputs can be played back side by side. Entries that are not videos are
ignored.

```bash
rocsync-align output.json --output_dir synced
```

| Option | Description |
| --- | --- |
| `--output_dir DIR` | Where synchronized videos are written; a relative path is taken inside each video's own folder, an absolute one is used as is (default: `synced`) |
| `--reencode` | Re-encode the video, so it starts and ends on exact frames without a container edit list; significantly slower |
| `--fps FPS` | Resample every video to this constant frame rate, so all outputs have the same number of frames; implies `--reencode` |
| `--jobs N` | Maximum concurrent `ffmpeg` processes, or `0` for no limit (default: `4`) |

Unless `--fps` is given, every output keeps its source's frames and frame timing, a variable
frame rate included, and covers the board-time span that all videos share. It starts on the
source frame nearest the span's start and ends on the one nearest its end, so each is at most
half a source frame off.

Each timestamp is the board time since that first frame, so the clock drift is gone: the
videos stay in step to their last frame, and every output starts at time 0. An audio track is
resampled the same way and stored as AAC at the source's bit rate.

- A stream copy, the default, leaves the video frames as they are. It cuts at a keyframe and
  leaves the exact start to a container edit list, which some players ignore. Its end can run a
  few frames long when the video has B-frames.
- With `--reencode`, the video is encoded without B-frames, and cut exactly at the first and
  last frame.
- With `--fps FPS`, the output frames are on a constant grid from the span's start: frame `k`
  is the source frame nearest board time `k / FPS` after it, repeated or skipped as the source
  rate requires, and the audio is cut to the same span. Every video gets the same number of
  frames, and each shows its nearest source frame, at most half a source frame off.

This needs `ffmpeg` on your `PATH`; `--reencode` uses `hevc_nvenc` when the available `ffmpeg`
provides it.

Re-encoded video (`--reencode` and `--fps`) is near-lossless, not lossless: constant quality, at about the size of a camera's
own file (`libx265 -crf 20`, or `hevc_nvenc -qp 22`). An `ffmpeg` or driver version that ignores
these settings would change the quality and size, so check a file if either matters.

### FFmpeg
To visually inspect the synchronization you can use FFmpeg to combine the aligned videos side-by-side:
```bash
ffmpeg \
	-i synced/video1.mp4 \
	-i synced/video2.mp4 \
	-filter_complex "hstack=inputs=2" \
	-c:v h264_nvenc \
	-preset p1 \
	synchronized.mp4
```

## Evaluation toolkit
`evaluation/` holds scripts for working with a whole multi-camera dataset once every camera has
been time-synced:

| Script | Purpose |
| --- | --- |
| `check_time_sync_all.py` | Extract one moment from every camera to verify the sync holds, including at the end of a long recording |
| `extract_synced_videos.py` | Cut time-aligned video clips across all cameras |
| `extract_clips_as_png.py` | Cut the same clips as PNG frame sequences |

They expect a dataset folder containing `raw_videos/` and a `time sync/` subfolder. See
[`evaluation/README.md`](evaluation/README.md) for the dataset layout and the full argument list.

## Benchmarking
`rocsync/benchmark/` measures the vision pipeline against a folder of validation images and videos,
every frame of a video counting as one benchmark frame:

| Tool | Purpose |
| --- | --- |
| `annotate.py` | Interactive GUI for building a `ground_truth.json` from validation frames |
| `retime.py` | Remux a clip onto a clock built from its annotations, so the timing a benchmark run has to recover is known exactly rather than fitted |
| `validate.py` | Run the pipeline over every frame and record what it decoded, with per-step timings, then fit each video's clock |
| `evaluate.py` | Score a validation run against the ground truth and report where it fails, per frame and per fitted clock |

See [`rocsync/benchmark/README.md`](rocsync/benchmark/README.md) for the annotation shortcuts and
the full argument list.

## Development
Everyone works against the same pinned environment, described by `pyproject.toml` and locked in
`uv.lock`. Create it with:

```bash
cd RocSync/sw
uv sync
```

This creates `.venv/` with the runtime dependencies plus the `dev` group (ruff, basedpyright,
pytest) at exactly the locked versions, and installs `rocsync` itself in editable mode. Re-run
`uv sync` after pulling; it is fast and idempotent. Prefix commands with `uv run` to use that
environment without activating it.

| Task | Command |
| --- | --- |
| Run the test suite | `uv run pytest` |
| Run the end-to-end tests | `uv run pytest -m e2e` (see below) |
| Format the code | `uv run ruff format .` |
| Check formatting only | `uv run ruff format --check .` |
| Lint | `uv run ruff check .` |
| Lint and apply safe fixes | `uv run ruff check --fix .` |
| Type-check | `uv run basedpyright` |

Ruff handles both formatting and linting, so no separate formatter is needed. All three tools
read their configuration from `pyproject.toml`; do not pass overriding flags on the command line,
otherwise results differ between machines. Run the format, lint, and type-check commands before
opening a pull request.

Some tests synthesize video fixtures with `ffmpeg` and skip themselves automatically when it is
not installed. Install `ffmpeg` to run the full suite.

To add or change a dependency, edit `pyproject.toml` and run `uv sync`, then commit the updated
`uv.lock` alongside it.

### End-to-end tests

The default suite leaves out the tests marked `e2e`, which run the installed command-line tools
on whole videos and take minutes to hours. Select them with `-m e2e`; a tier whose data is not
configured skips itself.

| Tier | What it runs | Needs | Takes |
| --- | --- | --- | --- |
| `tests/test_e2e_align.py` | `rocsync-align` on synthetic cameras whose clock is known, by stream copy, with `--reencode` and with `--fps` | `ffmpeg` | under a minute |
| `tests/test_e2e_benchmark.py` | `rocsync` on the annotated benchmark videos, scored against their reference clocks | `ROCSYNC_BENCHMARK_DIR` | minutes |
| `tests/test_e2e_real.py` | `rocsync`, `rocsync-align` by stream copy, with `--reencode` and with `--fps`, and `rocsync` again on full-length recordings, whose synced files must then show the same board time | `ROCSYNC_REAL_SAMPLES_DIR`, `ffmpeg` | about half an hour, longer on the first run |

```bash
ROCSYNC_BENCHMARK_DIR=/path/to/validation_data uv run pytest -m e2e tests/test_e2e_benchmark.py
```

The real-sample tier runs on copies of the recordings downscaled to 540 lines, which keep every
frame's timestamp. They are made on first use and kept in `~/.cache/rocsync/e2e`, or wherever
`ROCSYNC_E2E_CACHE_DIR` points.

Where each recording shows the board, and how its result is judged, comes from
`tests/e2e_real_samples.json`. To build that manifest, the generator searches the first 5 and
last 10 minutes of every copy. It widens a search that ends while the board is still in view.
`--search START END` adds a span for a board shown mid-recording. `--try-hard` searches, fits,
and later tests with rocsync's `--try-hard`, for a board too small to be found otherwise.
Regenerate it after adding recordings, review the table it prints, and commit it:

```bash
uv run python -m tests.generate_real_samples_manifest /path/to/real_samples [--only SESSION]
uv run python -m tests.generate_real_samples_manifest /path/to/real_samples \
    --only SESSION/FILE --search 0:37:00 0:40:00 --try-hard
```
