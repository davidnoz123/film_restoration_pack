"""film_restore.py — heavy_cleanup pipeline for old 8mm / Super 8 transfers

Profile: heavy_cleanup
  stabilize → deflicker → stronger denoise → sharpen → color → optional upscale

Usage:
  python film_restore.py dupcheck     # GATING STEP A: simple duplicate detection (clean telecine sources)
  python film_restore.py tempseg      # GATING STEP B: temporal segmentation (noisy / irregular sources)
  python film_restore.py fftcheck     # FFT of mean-luma signal: reveals 16 fps artefact in the frequency domain
  python film_restore.py analyze      # step 1: motion analysis (run once per clip)
  python film_restore.py preview      # step 2: short test render — review before continuing
  python film_restore.py master       # step 3: full render at native resolution
  python film_restore.py delivery     # step 4 (optional): 2x lanczos upscale version
  python film_restore.py sweep        # step 5 (optional): test many stabilisation candidates + auto-score
  python film_restore.py score        # re-score all files in outputs/sweep/ without re-rendering
  python film_restore.py compare      # generate original vs best-candidate side-by-side clip

Recommended order:
  0. Run 'dupcheck' (fast, for clean telecine) OR 'tempseg' (full temporal segmentation,
     preferred for noisy / irregular sources).  Both issue a GO / NO-GO decision.
  1. Run 'analyze' once per clip.
  2. Run 'preview' and review outputs/tests/preview.mp4.
  3. If jitter is still visible during pans, run 'sweep' to find better stabilisation settings.
  4. 'sweep' auto-scores every candidate with an optical-flow jitter metric (lower = smoother).
  5. Run 'compare' to generate a side-by-side of the original vs the best candidate.
  6. Adjust STABILIZE / tmix / minterpolate in SWEEP_CANDIDATES (or PIPELINE) as needed.
  7. When satisfied, run 'master'. Optionally run 'delivery' for a 2x lanczos upscale,
     or pass the master through Topaz Video AI for AI-assisted upscaling.

Requires for scoring / dupcheck / tempseg: pip install opencv-python numpy
Optional for plots:                        pip install matplotlib
"""

from __future__ import annotations

import csv
import math
import subprocess
import sys
from pathlib import Path


# ── Input / output ─────────────────────────────────────────────────────────────

INPUT           = Path("sample_clips.mp4")
TRANSFORMS_FILE = Path("transforms.trf")

OUTPUTS      = Path("outputs")
MASTER_DIR   = OUTPUTS / "master_native"
DELIVERY_DIR = OUTPUTS / "delivery_upscaled"
TESTS_DIR    = OUTPUTS / "tests"
SWEEP_DIR    = OUTPUTS / "sweep"


# ── Preview settings ───────────────────────────────────────────────────────────
# Preview always processes from the start of the file to keep frame numbers
# aligned with the transforms.trf written by 'analyze'. Adjust the duration
# to cover a representative section of your clip.

PREVIEW_DURATION = 20  # seconds


# ── Encoder ────────────────────────────────────────────────────────────────────

ENCODE = ["-c:v", "libx264", "-crf", "18", "-preset", "slow", "-c:a", "copy"]


# ── Duplicate frame detection thresholds ──────────────────────────────────────
# Tune these if the detection is too aggressive or too loose.
#
# MAD_THRESHOLD:       Mean Absolute Difference per pixel (0–255 scale, 8-bit).
#                      Frames are "identical" when MAD < this value.
#                      Typical digitised duplicate: 1–3; distinct frames: > 8.
# MIN_UNIQUE_FPS:      Lower bound on inferred unique fps to issue a GO.
# MAX_UNIQUE_FPS:      Upper bound on inferred unique fps to issue a GO.
# TARGET_UNIQUE_FPS:   Expected true frame rate of the source material.
# MAX_DEDUP_FRACTION:  Safety guard — warn if deduplication removes more than
#                      this fraction of total frames (suggests wrong threshold).

MAD_THRESHOLD      = 4.0    # MAD per pixel below which a frame counts as duplicate
MIN_UNIQUE_FPS     = 14.0   # GO/NO-GO lower bound
MAX_UNIQUE_FPS     = 18.0   # GO/NO-GO upper bound
TARGET_UNIQUE_FPS  = 16.0   # expected native frame rate of the source
MAX_DEDUP_FRACTION = 0.70   # warn if more than 70 % of frames would be dropped


# ── Temporal segmentation thresholds ─────────────────────────────────────────
# These control the hysteresis-threshold temporal segmenter used by 'tempseg'.
#
# This is a TIME-SERIES segmentation problem, not a clustering problem.
# All segment boundaries are detected using only local, temporal evidence.
# Segments are always contiguous and strictly ordered in time.
#
# SEG_NORM_MAD_WEIGHT:  weight of brightness-normalised MAD in combined score
# SEG_EDGE_DIFF_WEIGHT: weight of Laplacian edge-map difference in combined score
#                       Both weights should sum to 1.0.
# SEG_OPEN_THRESHOLD:   combined score at or above which a new segment boundary
#                       fires.  Lower = more sensitive (shorter segments).
#                       Typical range for noisy 8mm: 0.06 – 0.18.
# SEG_CLOSE_THRESHOLD:  hysteresis — after a boundary, signal must fall below
#                       this before the next boundary can fire.  Prevents
#                       double-splits around a single scene change.
#                       Must be < SEG_OPEN_THRESHOLD.
# SEG_SMOOTH_WINDOW:    moving-average window (frames) applied to the score
#                       signal before segmentation.  Suppresses flicker spikes.
# SEG_REP_MODE:         how the representative frame is chosen for each segment.
#                       "sharpest" — highest Laplacian variance
#                       "middle"   — centre frame of the run
#                       "average"  — frame closest to pixel-mean of the run
# SEG_MAX_RUN_WARN:     warn when mean run length exceeds this value.
# SEG_CONTACT_RUNS:     number of example runs shown in the contact sheet.
# SEG_MAX_SECONDS:      limit analysis to first N seconds; None = whole video.

SEG_NORM_MAD_WEIGHT  = 0.60
SEG_EDGE_DIFF_WEIGHT = 0.40
SEG_OPEN_THRESHOLD   = 0.10
SEG_CLOSE_THRESHOLD  = 0.04
SEG_SMOOTH_WINDOW    = 3
SEG_REP_MODE         = "sharpest"    # "sharpest" | "middle" | "average"
SEG_MAX_RUN_WARN     = 8
SEG_CONTACT_RUNS     = 12
SEG_MAX_SECONDS: int | None = None   # e.g. 60 to analyse only the first minute


# ── Heavy cleanup filter chain ─────────────────────────────────────────────────
# Tune each value here before committing to a full render.
#
# vidstabtransform smoothing=20: higher = smoother but more crop
# hqdn3d: luma_spatial:chroma_spatial:luma_temporal:chroma_temporal
#   baseline 1.2:1.2:4.5:4.5 — mild
#   heavy    3:3:9:9          — strong; back off if faces go plastic
# unsharp: luma_msize_x:luma_msize_y:luma_amount:chroma_... (0.8 is gentle)
# eq: contrast/saturation/brightness; tweak to taste

# Winner settings from sweep: s40_tmix3_mi50 (jitter_mean=0.0604, best of 13 candidates)
STABILIZE    = f"vidstabtransform=input={TRANSFORMS_FILE}:smoothing=40"
DEFLICKER    = "deflicker=size=5"
TMIX         = "tmix=frames=3"
DENOISE      = "hqdn3d=3:3:9:9"
SHARPEN      = "unsharp=5:5:0.8:5:5:0.0"
COLOR              = "eq=contrast=1.10:saturation=1.15:brightness=0.01"
# minterpolate modes:
#   mi_mode=blend  — fast cross-dissolve; produces ghost frames on fast pans
#   mi_mode=mci    — motion-compensated interpolation; smooth pans, 5-10x slower
MINTERPOLATE       = "minterpolate=fps=50:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"
MINTERPOLATE_BLEND = "minterpolate=fps=50:mi_mode=blend"  # fast fallback
UPSCALE            = "scale=2*iw:2*ih:flags=lanczos"

# Deduplication: mpdecimate drops near-identical frames before any processing.
# hi/lo/frac values mirror the MAD_THRESHOLD above (hi = 64*MAD, lo = 32*MAD).
# NOTE: source is already native 16 fps — not used in the default pipeline.
DEDUPLICATE  = "mpdecimate=hi=256:lo=128:frac=0.33"

# Pipeline order:
#   deflicker FIRST — normalises frame brightness BEFORE the stabiliser and
#   interpolator see the frames.  If flickery frames are fed into minterpolate
#   the synthesised tween frames inherit the brightness discontinuity.
#   stabilize SECOND — works on brightness-corrected frames.
#   minterpolate LAST — synthesises smooth in-between frames from clean input.
PIPELINE              = ",".join([DEFLICKER, STABILIZE, TMIX, DENOISE, SHARPEN, COLOR, MINTERPOLATE])
PIPELINE_WITH_UPSCALE = ",".join([DEFLICKER, STABILIZE, TMIX, DENOISE, SHARPEN, COLOR, MINTERPOLATE, UPSCALE])


# ── Sweep candidates ──────────────────────────────────────────────────────────
# Each entry tests a different combination of stabilisation smoothing, optional
# temporal blending (tmix), and optional motion interpolation (minterpolate).
# Add or remove entries freely. Labels become output filenames.
#
# Pipeline order for all candidates:
#   deflicker → stabilize → [tmix] → denoise → sharpen → color → [minterpolate]
#
# blend: fast cross-dissolve tween — ghosting on fast pans
# mci:   motion-compensated tween — correctly tracks motion; 5-10x slower

SWEEP_CANDIDATES: list[dict] = [
    # ── Stabilisation-only variants ──────────────────────────────────
    {"label": "s10",                 "smoothing": 10, "tmix": None,            "minterpolate": None},
    {"label": "s20",                 "smoothing": 20, "tmix": None,            "minterpolate": None},
    {"label": "s30",                 "smoothing": 30, "tmix": None,            "minterpolate": None},
    {"label": "s40",                 "smoothing": 40, "tmix": None,            "minterpolate": None},
    {"label": "s50",                 "smoothing": 50, "tmix": None,            "minterpolate": None},
    # ── Stabilisation + tmix ─────────────────────────────────────────
    {"label": "s20_tmix3",           "smoothing": 20, "tmix": "tmix=frames=3", "minterpolate": None},
    {"label": "s30_tmix3",           "smoothing": 30, "tmix": "tmix=frames=3", "minterpolate": None},
    {"label": "s30_tmix5",           "smoothing": 30, "tmix": "tmix=frames=5", "minterpolate": None},
    {"label": "s40_tmix3",           "smoothing": 40, "tmix": "tmix=frames=3", "minterpolate": None},
    # ── blend interpolation: fast, lower quality on pans ─────────────
    {"label": "s30_mi50_blend",      "smoothing": 30, "tmix": None,            "minterpolate": "minterpolate=fps=50:mi_mode=blend"},
    {"label": "s40_tmix3_mi50_blend","smoothing": 40, "tmix": "tmix=frames=3", "minterpolate": "minterpolate=fps=50:mi_mode=blend"},
    # ── MCI interpolation: motion-compensated, smooth pans ───────────
    {"label": "s30_mi50_mci",        "smoothing": 30, "tmix": None,            "minterpolate": "minterpolate=fps=50:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"},
    {"label": "s40_mi50_mci",        "smoothing": 40, "tmix": None,            "minterpolate": "minterpolate=fps=50:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"},
    {"label": "s40_tmix3_mi50_mci",  "smoothing": 40, "tmix": "tmix=frames=3", "minterpolate": "minterpolate=fps=50:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"},
    {"label": "s40_mi60_mci",        "smoothing": 40, "tmix": None,            "minterpolate": "minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"},
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def run(cmd: list[str], label: str) -> None:
    print(f"\n── {label}")
    print("  " + " ".join(cmd))
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(f"\nFAILED: {label} (exit {result.returncode})")
        sys.exit(result.returncode)
    print(f"OK: {label}")


def ensure_dirs() -> None:
    for d in (MASTER_DIR, DELIVERY_DIR, TESTS_DIR, SWEEP_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ── Duplicate frame detection ─────────────────────────────────────────────────

def _read_frames_gray(path: Path):
    """Yield (frame_index, gray_ndarray) for every frame via OpenCV."""
    import cv2  # type: ignore

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {path}")
    idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        yield idx, cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        idx += 1
    cap.release()


def _classify_duplicates(path: Path, threshold: float = MAD_THRESHOLD):
    """Return per-frame duplicate flags, MAD values, and container fps.

    Returns
    -------
    results : list[dict]  — one entry per frame with keys:
        frame_index, mad, is_duplicate, run_length (filled in later)
    fps     : float       — container frame rate reported by OpenCV
    """
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    cap.release()

    results: list[dict] = []
    prev_gray = None
    for idx, gray in _read_frames_gray(path):
        if prev_gray is None:
            mad = 0.0
            is_dup = False
        else:
            mad = float(np.mean(np.abs(gray.astype(np.int16) - prev_gray.astype(np.int16))))
            is_dup = mad < threshold
        results.append({"frame_index": idx, "mad": mad, "is_duplicate": is_dup, "run_length": 1})
        prev_gray = gray

    # Fill run_length: for each duplicate extend the current run counter
    run = 1
    for i in range(1, len(results)):
        if results[i]["is_duplicate"]:
            run += 1
        else:
            run = 1
        results[i]["run_length"] = run

    return results, fps


def _compute_run_stats(results: list[dict]):
    """Return histogram and stats for runs of duplicates.

    A "run" is a contiguous block of identical frames.  The run length is the
    total block size including the first (non-duplicate) frame.
    """
    import numpy as np  # type: ignore

    # Collect run lengths by scanning transitions
    runs: list[int] = []
    current_run = 1
    for i in range(1, len(results)):
        if results[i]["is_duplicate"]:
            current_run += 1
        else:
            runs.append(current_run)
            current_run = 1
    runs.append(current_run)  # last run

    arr = np.array(runs, dtype=float)
    histogram: dict[int, int] = {}
    for r in runs:
        histogram[r] = histogram.get(r, 0) + 1

    stats = {
        "run_count":  len(runs),
        "mean":       float(arr.mean()),
        "median":     float(np.median(arr)),
        "std":        float(arr.std()),
        "min":        int(arr.min()),
        "max":        int(arr.max()),
    }
    return runs, histogram, stats


def _infer_unique_fps(results: list[dict], container_fps: float) -> tuple[float, list[tuple[int, int]]]:
    """Compute unique frames per second for each 1-second window.

    Returns (overall_mean_unique_fps, per_second_list[(second, unique_count)]).
    """
    window: dict[int, int] = {}  # second → unique frame count
    for r in results:
        sec = int(r["frame_index"] / container_fps)
        if not r["is_duplicate"]:
            window[sec] = window.get(sec, 0) + 1

    per_second = sorted(window.items())
    if not per_second:
        return 0.0, []
    mean_fps = sum(v for _, v in per_second) / len(per_second)
    return mean_fps, per_second


def step_dupcheck(save_csv: bool = True, save_plot: bool = True) -> None:
    """GATING STEP — analyse duplicated frames to verify ~16 fps source material.

    Method
    ------
    1.  Read every frame via OpenCV.
    2.  Compute Mean Absolute Difference (MAD) between each pair of consecutive
        frames (per-pixel, 8-bit luma channel).
    3.  Classify a frame as a duplicate when MAD < MAD_THRESHOLD.
    4.  Identify contiguous runs of duplicate frames.
    5.  Segment the video into 1-second windows and count unique frames/second.
    6.  Issue a GO / NO-GO decision:
          GO     — inferred unique fps falls in [MIN_UNIQUE_FPS, MAX_UNIQUE_FPS]
          NO-GO  — print a clear warning and halt the pipeline.

    Outputs (in outputs/tests/)
    ---------------------------
    dupcheck.csv  — frame_index, mad, is_duplicate, run_length
    dupcheck.png  — MAD over time with duplicate runs highlighted (needs matplotlib)
    """
    if not INPUT.exists():
        print(f"ERROR: input file not found: {INPUT}")
        sys.exit(1)

    try:
        import numpy as np  # type: ignore
    except ImportError:
        print("ERROR: numpy is required.  pip install opencv-python numpy")
        sys.exit(1)

    print("\n── Duplicate frame analysis")
    print(f"  Input:         {INPUT}")
    print(f"  MAD threshold: {MAD_THRESHOLD}")

    results, container_fps = _classify_duplicates(INPUT, MAD_THRESHOLD)
    total_frames = len(results)
    dup_count    = sum(1 for r in results if r["is_duplicate"])
    unique_count = total_frames - dup_count

    print(f"  Container fps: {container_fps:.3f}")
    print(f"  Total frames:  {total_frames}")
    print(f"  Duplicate:     {dup_count}  ({100*dup_count/max(total_frames,1):.1f} %)")
    print(f"  Unique:        {unique_count}  ({100*unique_count/max(total_frames,1):.1f} %)")

    # ── Safety guard: dedup fraction ─────────────────────────────────────────
    dedup_fraction = dup_count / max(total_frames, 1)
    if dedup_fraction > MAX_DEDUP_FRACTION:
        print(
            f"\n  WARN: {dedup_fraction*100:.1f} % of frames would be removed by deduplication "
            f"(threshold: {MAX_DEDUP_FRACTION*100:.0f} %).  "
            "Consider raising MAD_THRESHOLD — the threshold may be too loose."
        )

    # ── Run-length statistics ─────────────────────────────────────────────────
    runs, histogram, stats = _compute_run_stats(results)

    print("\n  Run-length histogram (run length → count of runs):")
    for length in sorted(histogram):
        bar = "█" * min(histogram[length], 60)
        print(f"    {length:>3}x : {histogram[length]:>5}  {bar}")

    print(f"\n  Run statistics:")
    print(f"    count  = {stats['run_count']}")
    print(f"    mean   = {stats['mean']:.2f}")
    print(f"    median = {stats['median']:.2f}")
    print(f"    std    = {stats['std']:.2f}")
    print(f"    min    = {stats['min']},  max = {stats['max']}")

    # ── Per-second unique frame count ─────────────────────────────────────────
    mean_unique_fps, per_second = _infer_unique_fps(results, container_fps)

    print(f"\n  Per-second unique frame count (target ≈ {TARGET_UNIQUE_FPS:.0f} fps):")
    col = 10
    for i, (sec, cnt) in enumerate(per_second):
        end = "  " if (i + 1) % col else "\n    "
        print(f"    s{sec:>3}: {cnt:>2}{end}", end="")
    print()

    print(f"\n  Inferred unique fps (mean across seconds): {mean_unique_fps:.2f}")

    # ── Debug CSV ─────────────────────────────────────────────────────────────
    if save_csv:
        ensure_dirs()
        csv_path = TESTS_DIR / "dupcheck.csv"
        with csv_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["frame_index", "mad", "is_duplicate", "run_length"])
            writer.writeheader()
            writer.writerows(results)
        print(f"\n  CSV saved: {csv_path}")

    # ── Optional plot ─────────────────────────────────────────────────────────
    if save_plot:
        _plot_dupcheck(results, per_second, container_fps)

    # ── GO / NO-GO decision ───────────────────────────────────────────────────
    go = MIN_UNIQUE_FPS <= mean_unique_fps <= MAX_UNIQUE_FPS
    consistency_ok = stats["std"] <= 1.5  # runs should be fairly uniform in length

    print("\n" + "═" * 60)
    if go and consistency_ok:
        print("  [GO]   Duplicated-frame pattern confirmed.")
        print(f"     Inferred source fps: {mean_unique_fps:.2f}  "
              f"(target {TARGET_UNIQUE_FPS:.0f}, window [{MIN_UNIQUE_FPS}–{MAX_UNIQUE_FPS}])")
        print("     Proceed with: analyze → preview → master")
    else:
        reasons = []
        if not go:
            reasons.append(
                f"inferred unique fps {mean_unique_fps:.2f} is outside "
                f"[{MIN_UNIQUE_FPS}–{MAX_UNIQUE_FPS}]"
            )
        if not consistency_ok:
            reasons.append(
                f"run-length std dev {stats['std']:.2f} > 1.5 "
                "(inconsistent duplication pattern)"
            )
        print("  [NO-GO] Duplication pattern does NOT match ~16 fps source.")
        for r in reasons:
            print(f"     Reason: {r}")
        print("     Do NOT proceed with interpolation or stabilisation.")
        print("     Check the source clip and review dupcheck.csv / dupcheck.png.")
        sys.exit(2)
    print("═" * 60)


def _plot_dupcheck(results: list[dict], per_second: list[tuple[int, int]], container_fps: float) -> None:
    """Save a two-panel plot: MAD over time + unique fps per second."""
    try:
        import matplotlib  # type: ignore
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        print("  (matplotlib not installed — skipping plot.  pip install matplotlib)")
        return

    ensure_dirs()
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 7), tight_layout=True)

    # Panel 1: MAD over time
    indices = [r["frame_index"] for r in results]
    mads    = [r["mad"] for r in results]
    is_dup  = [r["is_duplicate"] for r in results]

    ax1.plot(indices, mads, linewidth=0.5, color="steelblue", label="MAD")
    ax1.axhline(MAD_THRESHOLD, color="red", linestyle="--", linewidth=1, label=f"threshold={MAD_THRESHOLD}")

    # Shade duplicate spans
    in_span = False
    span_start = 0
    for i, dup in enumerate(is_dup):
        if dup and not in_span:
            span_start = i
            in_span = True
        elif not dup and in_span:
            ax1.axvspan(span_start, i, alpha=0.2, color="orange")
            in_span = False
    if in_span:
        ax1.axvspan(span_start, len(is_dup), alpha=0.2, color="orange")

    ax1.set_xlabel("Frame index")
    ax1.set_ylabel("MAD (luma, 8-bit)")
    ax1.set_title(f"Frame MAD — duplicate runs highlighted  (container fps={container_fps:.2f})")
    ax1.legend(fontsize=8)
    ax1.set_ylim(bottom=0)

    # Panel 2: Unique frames per second
    if per_second:
        secs, cnts = zip(*per_second)
        colors = ["green" if MIN_UNIQUE_FPS <= c <= MAX_UNIQUE_FPS else "red" for c in cnts]
        ax2.bar(secs, cnts, color=colors, width=0.8)
        ax2.axhline(TARGET_UNIQUE_FPS, color="blue", linestyle="--", linewidth=1,
                    label=f"target={TARGET_UNIQUE_FPS:.0f} fps")
        ax2.axhspan(MIN_UNIQUE_FPS, MAX_UNIQUE_FPS, alpha=0.1, color="blue",
                    label=f"GO window [{MIN_UNIQUE_FPS}–{MAX_UNIQUE_FPS}]")
        ax2.set_xlabel("Second")
        ax2.set_ylabel("Unique frames")
        ax2.set_title("Unique frames per second (green = within GO window)")
        ax2.legend(fontsize=8)

    plot_path = TESTS_DIR / "dupcheck.png"
    fig.savefig(str(plot_path), dpi=120)
    plt.close(fig)
    print(f"  Plot saved: {plot_path}")


# ── Temporal segmentation helpers ─────────────────────────────────────────────
# These functions implement the temporal segmentation pipeline used by
# 'tempseg'.  They are deliberately separate from the dupcheck functions so
# the two approaches can be developed and tuned independently.


def _edge_map(gray):
    """Return a uint8 Laplacian-magnitude edge map, normalised per-frame."""
    import cv2          # type: ignore
    import numpy as np  # type: ignore

    lap = cv2.Laplacian(gray, cv2.CV_32F, ksize=3)
    mag = np.abs(lap)
    peak = float(mag.max())
    if peak < 1.0:
        return np.zeros_like(gray, dtype=np.uint8)
    return np.clip(mag / peak * 255.0, 0, 255).astype(np.uint8)


def _sharpness(gray) -> float:
    """Return Laplacian variance as a sharpness proxy (higher = sharper)."""
    import cv2  # type: ignore
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _load_frames_gray(path: Path, max_seconds=None):
    """Return (list_of_gray_frames, container_fps).

    Reuses _read_frames_gray so only one decode path exists.
    If max_seconds is given, only the first N seconds are loaded.
    """
    import cv2  # type: ignore

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    cap.release()

    max_frames = int(fps * max_seconds) if max_seconds else None
    frames = []
    for _idx, gray in _read_frames_gray(path):
        frames.append(gray)
        if max_frames and len(frames) >= max_frames:
            break
    return frames, fps


def _compute_temporal_features(frames: list) -> list[dict]:
    """Compute per-transition dissimilarity features between consecutive frames.

    For each pair (frames[i-1], frames[i]) this computes:

      norm_mad   brightness-normalised mean absolute difference
                 = MAD / (mean_brightness + 1)
                 Dividing by mean brightness makes the score invariant to
                 overall exposure level (important for flickering 8mm).

      edge_diff  normalised Laplacian edge-map MAD
                 = mean|edges_i - edges_{i-1}| / 255
                 Captures structural/motion change independently of brightness.

      score      weighted combination:
                 SEG_NORM_MAD_WEIGHT * norm_mad + SEG_EDGE_DIFF_WEIGHT * edge_diff

    Frame 0 has no predecessor so all features are 0.0.

    Typical values for digitised 8mm:
      Within a latent-frame run (noise/flicker): score ~ 0.01 – 0.05
      Genuine scene boundary:                    score ~ 0.08 – 0.30
    """
    import numpy as np  # type: ignore

    features: list[dict] = []
    prev_gray  = None
    prev_edges = None

    for i, gray in enumerate(frames):
        curr_edges = _edge_map(gray)

        if prev_gray is None:
            features.append({"frame_index": i, "norm_mad": 0.0,
                              "edge_diff": 0.0, "score": 0.0})
        else:
            mean_brightness = (float(prev_gray.mean()) + float(gray.mean())) / 2.0
            mad_raw  = float(np.mean(np.abs(
                gray.astype(np.int16) - prev_gray.astype(np.int16)
            )))
            norm_mad = mad_raw / (mean_brightness + 1.0)

            edge_diff = float(np.mean(np.abs(
                curr_edges.astype(np.int16) - prev_edges.astype(np.int16)
            ))) / 255.0

            score = SEG_NORM_MAD_WEIGHT * norm_mad + SEG_EDGE_DIFF_WEIGHT * edge_diff
            features.append({"frame_index": i, "norm_mad": norm_mad,
                              "edge_diff": edge_diff, "score": score})

        prev_gray  = gray
        prev_edges = curr_edges

    return features


def _smooth_scores(scores: list[float], window: int) -> list[float]:
    """Centred moving-average smoothing with edge-replication padding."""
    import numpy as np  # type: ignore

    if window <= 1 or len(scores) < window:
        return scores
    arr = np.array(scores)
    kernel = np.ones(window) / window
    return list(np.convolve(arr, kernel, mode="same"))


def _segment_hysteresis(
    smoothed_scores: list[float],
    open_thresh: float,
    close_thresh: float,
) -> list[tuple[int, int]]:
    """Hysteresis-threshold temporal segmenter.

    CRITICAL DESIGN NOTE
    --------------------
    This is a TIME-SERIES segmenter, not a clusterer.
    Segments are always contiguous blocks of consecutive frames.
    The only information used at each step is the local dissimilarity score
    between adjacent frames.  There is no global comparison.

    Algorithm
    ---------
    Scan forward through the (already-smoothed) dissimilarity scores:

    1.  Start in READY state (willing to fire a boundary).
    2.  When score[i] >= open_thresh  →  boundary at frame i.
        Append the completed segment (seg_start, i-1).
        Reset seg_start = i.
        Enter COOLDOWN state.
    3.  In COOLDOWN, wait for score to drop below close_thresh,
        then return to READY.
        This prevents a single sharp scene-change from generating multiple
        back-to-back boundaries.
    4.  After the last frame, close the final segment.

    Returns list of (start_frame, end_frame) tuples (both inclusive).
    """
    n = len(smoothed_scores)
    if n == 0:
        return []

    segments: list[tuple[int, int]] = []
    seg_start = 0
    ready = True   # ready to fire on the very first high-score transition

    for i in range(1, n):
        s = smoothed_scores[i]
        if ready:
            if s >= open_thresh:
                segments.append((seg_start, i - 1))
                seg_start = i
                ready = False          # enter cooldown
        else:
            if s < close_thresh:
                ready = True           # cooldown complete

    segments.append((seg_start, n - 1))  # close final segment
    return segments


def _select_representative(
    seg_start: int,
    seg_end: int,
    frames: list,
    mode: str = "sharpest",
) -> int:
    """Return the index of the representative frame for a segment.

    Modes
    -----
    "sharpest"  highest Laplacian variance in the run
    "middle"    centre frame of the run
    "average"   frame whose pixel values are closest to the per-pixel mean
                of the entire run (always returns an actual frame index)
    """
    import numpy as np  # type: ignore

    run_frames = frames[seg_start: seg_end + 1]

    if len(run_frames) == 1:
        return seg_start

    if mode == "middle":
        return (seg_start + seg_end) // 2

    if mode == "average":
        stack = np.stack(run_frames, axis=0).astype(np.float32)
        avg   = stack.mean(axis=0)
        best_idx  = seg_start
        best_mse  = float("inf")
        for offset, frm in enumerate(run_frames):
            mse = float(np.mean((frm.astype(np.float32) - avg) ** 2))
            if mse < best_mse:
                best_mse = mse
                best_idx = seg_start + offset
        return best_idx

    # default: "sharpest"
    best_idx   = seg_start
    best_score = _sharpness(run_frames[0])
    for offset, frm in enumerate(run_frames[1:], 1):
        s = _sharpness(frm)
        if s > best_score:
            best_score = s
            best_idx   = seg_start + offset
    return best_idx


def _build_segment_list(
    spans: list[tuple[int, int]],
    features: list[dict],
    frames: list,
    rep_mode: str = "sharpest",
) -> list[dict]:
    """Build annotated segment records from raw (start, end) spans."""
    import numpy as np  # type: ignore

    segments = []
    for seg_id, (start, end) in enumerate(spans):
        rep_idx   = _select_representative(start, end, frames, rep_mode)
        seg_scores = [features[i]["score"] for i in range(start, end + 1)]
        segments.append({
            "seg_id":     seg_id,
            "start":      start,
            "end":        end,
            "length":     end - start + 1,
            "rep_frame":  rep_idx,
            "mean_score": float(np.mean(seg_scores)),
            "max_score":  float(np.max(seg_scores)),
        })
    return segments


def _latent_fps_from_segments(
    segments: list[dict],
    container_fps: float,
) -> tuple[float, list[tuple[int, int]]]:
    """Return (mean_latent_fps, [(second, latent_count), ...]).

    Each segment contributes one latent frame.  Its timestamp is taken from
    the representative frame index.
    """
    window: dict[int, int] = {}
    for seg in segments:
        sec = int(seg["rep_frame"] / container_fps)
        window[sec] = window.get(sec, 0) + 1

    per_second = sorted(window.items())
    if not per_second:
        return 0.0, []
    mean_fps = sum(v for _, v in per_second) / len(per_second)
    return mean_fps, per_second


def _seg_run_stats(segments: list[dict]) -> tuple[dict[int, int], dict]:
    """Return (histogram, stats) for segment run lengths."""
    import numpy as np  # type: ignore

    lengths = [s["length"] for s in segments]
    arr     = np.array(lengths, dtype=float)
    histogram: dict[int, int] = {}
    for ln in lengths:
        histogram[ln] = histogram.get(ln, 0) + 1

    stats = {
        "count":  len(lengths),
        "mean":   float(arr.mean()),
        "median": float(np.median(arr)),
        "std":    float(arr.std()),
        "min":    int(arr.min()),
        "max":    int(arr.max()),
    }
    return histogram, stats


# ── Temporal segmentation step ────────────────────────────────────────────────

def step_tempseg() -> None:
    """Temporal segmentation — recover a latent ~16 fps frame sequence.

    This step is the preferred gating step when the source has irregular
    duplication, near-duplicates, flicker, or mixed cadence.

    Unlike 'dupcheck' (which classifies frames as exact/near duplicates),
    'tempseg' treats the problem as TIME-SERIES SEGMENTATION:

        Partition the container-fps frame sequence into contiguous runs,
        each representing one underlying latent source frame.

    No global clustering is performed.  All boundaries are detected using
    only local, temporal evidence — the dissimilarity between adjacent frames.

    Phase 1 — feature extraction (per consecutive frame pair)
        norm_mad    brightness-normalised Mean Absolute Difference
        edge_diff   normalised Laplacian edge-map MAD
        score       SEG_NORM_MAD_WEIGHT * norm_mad + SEG_EDGE_DIFF_WEIGHT * edge_diff

    Phase 2 — hysteresis segmentation
        Score signal smoothed with moving average (SEG_SMOOTH_WINDOW).
        Boundary fires when smoothed score >= SEG_OPEN_THRESHOLD.
        Hysteresis: signal must fall below SEG_CLOSE_THRESHOLD before the
        next boundary can fire (prevents double-splits).

    Phase 3 — run collapse
        Each segment → one representative latent frame (SEG_REP_MODE).

    Phase 4 — GO / NO-GO validation
        Inferred latent fps must fall in [MIN_UNIQUE_FPS, MAX_UNIQUE_FPS].

    Outputs written to outputs/tests/
        tempseg_features.csv   per-frame dissimilarity features
        tempseg_segments.csv   per-segment summary
        tempseg_plots.png      3-panel diagnostic plot
        tempseg_contact.png    contact sheet of example runs
    """
    if not INPUT.exists():
        print(f"ERROR: input file not found: {INPUT}")
        sys.exit(1)

    try:
        import numpy as np  # type: ignore
    except ImportError:
        print("ERROR: numpy required.  pip install opencv-python numpy")
        sys.exit(1)

    ensure_dirs()

    print("\n── Temporal segmentation")
    print(f"  Input:            {INPUT}")
    if SEG_MAX_SECONDS:
        print(f"  Limiting to:      first {SEG_MAX_SECONDS} s")
    print(f"  Rep mode:         {SEG_REP_MODE}")
    print(f"  Open threshold:   {SEG_OPEN_THRESHOLD}")
    print(f"  Close threshold:  {SEG_CLOSE_THRESHOLD}")
    print(f"  Smooth window:    {SEG_SMOOTH_WINDOW}")

    # ── Phase 1: load frames and extract features ─────────────────────────────
    print("  Loading frames...", end=" ", flush=True)
    frames, container_fps = _load_frames_gray(INPUT, max_seconds=SEG_MAX_SECONDS)
    total_frames = len(frames)
    print(f"{total_frames} frames  (container fps={container_fps:.3f})")

    if total_frames < 4:
        print("ERROR: too few frames to segment.")
        sys.exit(1)

    print("  Extracting features...", end=" ", flush=True)
    features = _compute_temporal_features(frames)
    print("done")

    # ── Phase 2: smooth + hysteresis segmentation ─────────────────────────────
    print("  Segmenting...", end=" ", flush=True)
    raw_scores     = [f["score"] for f in features]
    smoothed_scores = _smooth_scores(raw_scores, SEG_SMOOTH_WINDOW)
    spans = _segment_hysteresis(smoothed_scores, SEG_OPEN_THRESHOLD, SEG_CLOSE_THRESHOLD)
    print(f"{len(spans)} segments")

    # ── Phase 3: run collapse ─────────────────────────────────────────────────
    print("  Selecting representative frames...", end=" ", flush=True)
    segments = _build_segment_list(spans, features, frames, rep_mode=SEG_REP_MODE)
    print("done")

    # ── Statistics ────────────────────────────────────────────────────────────
    histogram, run_stats = _seg_run_stats(segments)
    mean_latent_fps, per_second = _latent_fps_from_segments(segments, container_fps)

    print(f"\n  Container fps:    {container_fps:.3f}")
    print(f"  Total frames:     {total_frames}")
    print(f"  Segments found:   {len(segments)}")

    print("\n  Run-length histogram (segment length → count of segments):")
    for length in sorted(histogram):
        bar = "#" * min(histogram[length], 60)
        print(f"    {length:>3} frame(s): {histogram[length]:>5}  {bar}")

    print(f"\n  Run-length statistics:")
    print(f"    count  = {run_stats['count']}")
    print(f"    mean   = {run_stats['mean']:.2f}")
    print(f"    median = {run_stats['median']:.2f}")
    print(f"    std    = {run_stats['std']:.2f}")
    print(f"    min    = {run_stats['min']},  max = {run_stats['max']}")

    if run_stats["mean"] > SEG_MAX_RUN_WARN:
        print(
            f"\n  WARN: mean run length {run_stats['mean']:.2f} > {SEG_MAX_RUN_WARN}. "
            "Segmentation may be under-splitting.  Try lowering SEG_OPEN_THRESHOLD."
        )

    collapse_fraction = (total_frames - len(segments)) / max(total_frames, 1)
    if collapse_fraction > MAX_DEDUP_FRACTION:
        print(
            f"\n  WARN: {collapse_fraction * 100:.1f} % of frames collapsed. "
            "If this seems too aggressive, raise SEG_OPEN_THRESHOLD."
        )

    print(f"\n  Per-second latent frame count  (target ~{TARGET_UNIQUE_FPS:.0f}):")
    col = 10
    for i, (sec, cnt) in enumerate(per_second):
        end_str = "  " if (i + 1) % col else "\n    "
        print(f"    s{sec:>3}: {cnt:>2}{end_str}", end="")
    print()
    print(f"\n  Inferred latent fps (mean): {mean_latent_fps:.2f}")

    # ── Diagnostics ───────────────────────────────────────────────────────────
    _save_tempseg_csvs(features, segments)
    _plot_tempseg(raw_scores, smoothed_scores, segments, per_second, container_fps)
    _save_contact_sheet(frames, segments)

    # ── GO / NO-GO ────────────────────────────────────────────────────────────
    fps_ok        = MIN_UNIQUE_FPS <= mean_latent_fps <= MAX_UNIQUE_FPS
    # More lenient std threshold than dupcheck — irregular cadence is expected
    consistent    = run_stats["std"] <= 2.5

    print("\n" + "=" * 62)
    if fps_ok and consistent:
        print("  [GO]   Temporal segmentation plausible.")
        print(
            f"         Inferred latent fps: {mean_latent_fps:.2f}  "
            f"(target {TARGET_UNIQUE_FPS:.0f}, window [{MIN_UNIQUE_FPS}\u2013{MAX_UNIQUE_FPS}])"
        )
        print("         Inspect tempseg_contact.png and tempseg_plots.png.")
        print("         Then run: analyze -> preview -> master")
    else:
        reasons = []
        if not fps_ok:
            reasons.append(
                f"inferred latent fps {mean_latent_fps:.2f} is outside "
                f"[{MIN_UNIQUE_FPS}\u2013{MAX_UNIQUE_FPS}]"
            )
        if not consistent:
            reasons.append(
                f"run-length std {run_stats['std']:.2f} > 2.5 "
                "(highly irregular cadence)"
            )
        print("  [NO-GO] Temporal segmentation does NOT confirm ~16 fps latent sequence.")
        for r in reasons:
            print(f"         Reason: {r}")
        print("         Review tempseg_plots.png.  Adjust SEG_OPEN_THRESHOLD and retry.")
        print("         Do NOT proceed with interpolation until this resolves.")
        sys.exit(2)
    print("=" * 62)


def _save_tempseg_csvs(features: list[dict], segments: list[dict]) -> None:
    """Write tempseg_features.csv and tempseg_segments.csv."""
    # Build frame_index → segment lookup for annotating feature rows
    frame_to_seg: dict[int, dict] = {}
    for seg in segments:
        for fi in range(seg["start"], seg["end"] + 1):
            frame_to_seg[fi] = seg

    feat_path = TESTS_DIR / "tempseg_features.csv"
    with feat_path.open("w", newline="") as f:
        fields = [
            "frame_index", "norm_mad", "edge_diff", "score",
            "seg_id", "seg_start", "seg_end", "seg_length", "rep_frame",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for feat in features:
            seg = frame_to_seg.get(feat["frame_index"], {})
            writer.writerow({
                "frame_index": feat["frame_index"],
                "norm_mad":    f"{feat['norm_mad']:.6f}",
                "edge_diff":   f"{feat['edge_diff']:.6f}",
                "score":       f"{feat['score']:.6f}",
                "seg_id":      seg.get("seg_id", ""),
                "seg_start":   seg.get("start", ""),
                "seg_end":     seg.get("end", ""),
                "seg_length":  seg.get("length", ""),
                "rep_frame":   seg.get("rep_frame", ""),
            })
    print(f"\n  CSV: {feat_path}")

    seg_path = TESTS_DIR / "tempseg_segments.csv"
    with seg_path.open("w", newline="") as f:
        fields = ["seg_id", "start", "end", "length", "rep_frame",
                  "mean_score", "max_score"]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for seg in segments:
            writer.writerow(
                {k: (f"{v:.6f}" if isinstance(v, float) else v)
                 for k, v in seg.items()}
            )
    print(f"  CSV: {seg_path}")


def _plot_tempseg(
    raw_scores: list[float],
    smoothed_scores: list[float],
    segments: list[dict],
    per_second: list[tuple[int, int]],
    container_fps: float,
) -> None:
    """3-panel diagnostic plot:
       1. Raw + smoothed dissimilarity over time with segment boundaries
       2. Run-length histogram
       3. Latent frames per second
    """
    try:
        import matplotlib           # type: ignore
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # type: ignore
        import numpy as np          # type: ignore
    except ImportError:
        print("  (matplotlib not installed — skipping plots.  pip install matplotlib)")
        return

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(16, 11), tight_layout=True)

    # ── Panel 1: dissimilarity signal + segment boundaries ───────────────────
    indices = list(range(len(raw_scores)))
    ax1.plot(indices, raw_scores,      linewidth=0.4, color="steelblue",
             alpha=0.6, label="raw score")
    ax1.plot(indices, smoothed_scores, linewidth=1.0, color="navy",
             label=f"smoothed (w={SEG_SMOOTH_WINDOW})")
    ax1.axhline(SEG_OPEN_THRESHOLD,  color="red",    linestyle="--",
                linewidth=1.0, label=f"open={SEG_OPEN_THRESHOLD}")
    ax1.axhline(SEG_CLOSE_THRESHOLD, color="darkorange", linestyle="--",
                linewidth=1.0, label=f"close={SEG_CLOSE_THRESHOLD}")
    for seg in segments[1:]:     # skip seg 0 — boundary is the left edge of the plot
        ax1.axvline(seg["start"], color="green", linewidth=0.5, alpha=0.4)
    ax1.set_xlabel("Frame index")
    ax1.set_ylabel("Dissimilarity score")
    ax1.set_title(
        f"Temporal dissimilarity — {len(segments)} segments  "
        f"(container fps={container_fps:.2f})"
    )
    ax1.legend(fontsize=8)
    ax1.set_ylim(bottom=0)

    # ── Panel 2: run-length histogram ─────────────────────────────────────────
    lengths = [s["length"] for s in segments]
    max_len = max(lengths) if lengths else 1
    bins    = list(range(1, max_len + 2))
    ax2.hist(lengths, bins=bins, align="left", color="steelblue",
             edgecolor="white", linewidth=0.5)
    ax2.axvline(float(np.mean(lengths)),   color="red",    linestyle="--",
                linewidth=1.0, label=f"mean={np.mean(lengths):.2f}")
    ax2.axvline(float(np.median(lengths)), color="orange", linestyle="--",
                linewidth=1.0, label=f"median={np.median(lengths):.2f}")
    ax2.set_xlabel("Segment length (frames)")
    ax2.set_ylabel("Count of segments")
    ax2.set_title("Run-length histogram")
    ax2.legend(fontsize=8)

    # ── Panel 3: latent fps per second ────────────────────────────────────────
    if per_second:
        secs, cnts = zip(*per_second)
        colors = ["green" if MIN_UNIQUE_FPS <= c <= MAX_UNIQUE_FPS else "red"
                  for c in cnts]
        ax3.bar(secs, cnts, color=colors, width=0.8)
        ax3.axhline(TARGET_UNIQUE_FPS, color="blue", linestyle="--", linewidth=1,
                    label=f"target={TARGET_UNIQUE_FPS:.0f}")
        ax3.axhspan(MIN_UNIQUE_FPS, MAX_UNIQUE_FPS, alpha=0.10, color="blue",
                    label=f"GO [{MIN_UNIQUE_FPS}\u2013{MAX_UNIQUE_FPS}]")
        ax3.set_xlabel("Second")
        ax3.set_ylabel("Latent frames")
        ax3.set_title("Recovered latent frames per second  (green = within GO window)")
        ax3.legend(fontsize=8)

    plot_path = TESTS_DIR / "tempseg_plots.png"
    fig.savefig(str(plot_path), dpi=120)
    plt.close(fig)
    print(f"  Plot: {plot_path}")


def _save_contact_sheet(frames: list, segments: list[dict]) -> None:
    """Contact sheet: one row per sampled segment.

    Columns:  [label] [frame 0] [frame 1] … [frame N-1]
    The representative frame is highlighted with a green border.

    Shows the SEG_CONTACT_RUNS longest segments (spread across the video)
    so you can judge whether the segmentation makes visual sense.
    """
    try:
        import cv2                      # type: ignore
        import matplotlib               # type: ignore
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # type: ignore
    except ImportError:
        print("  (cv2/matplotlib not installed — skipping contact sheet)")
        return

    n = min(SEG_CONTACT_RUNS, len(segments))
    if n == 0:
        return

    # Pick the N longest runs, then restore chronological order for readability
    sampled = sorted(segments, key=lambda s: s["length"], reverse=True)[:n]
    sampled.sort(key=lambda s: s["start"])

    max_run_cols = min(max(s["length"] for s in sampled), 10)

    # Thumbnail dimensions
    thumb_h = 80
    h, w    = frames[0].shape[:2]
    thumb_w = int(w * thumb_h / max(h, 1))

    n_cols = max_run_cols + 1   # +1 for the text label column
    fig, axes = plt.subplots(
        n, n_cols,
        figsize=(n_cols * 1.6, n * 1.3),
        squeeze=False,
    )
    fig.suptitle(
        f"Contact sheet — {n} segments (longest, in time order)  |  "
        f"rep mode: {SEG_REP_MODE}  |  green border = representative frame",
        fontsize=8,
    )

    for row, seg in enumerate(sampled):
        run_indices = list(range(seg["start"], seg["end"] + 1))
        rep = seg["rep_frame"]

        for col in range(n_cols):
            ax = axes[row][col]
            ax.axis("off")

            if col == 0:
                # Row label
                ax.text(
                    0.5, 0.5,
                    f"seg {seg['seg_id']}\n"
                    f"[{seg['start']}\u2013{seg['end']}]\n"
                    f"len={seg['length']}",
                    ha="center", va="center", fontsize=6,
                    transform=ax.transAxes,
                )
            else:
                fi_offset = col - 1
                if fi_offset < len(run_indices):
                    fidx  = run_indices[fi_offset]
                    thumb = cv2.resize(frames[fidx], (thumb_w, thumb_h))
                    ax.imshow(thumb, cmap="gray", vmin=0, vmax=255)
                    ax.set_title(f"f{fidx}", fontsize=5, pad=1)
                    if fidx == rep:
                        for spine in ax.spines.values():
                            spine.set_visible(True)
                            spine.set_edgecolor("green")
                            spine.set_linewidth(2.5)

    sheet_path = TESTS_DIR / "tempseg_contact.png"
    fig.savefig(str(sheet_path), dpi=120, bbox_inches="tight")
    plt.close(fig)
    print(f"  Contact sheet: {sheet_path}")


# ── Pipeline steps ─────────────────────────────────────────────────────────────

def _require_dupcheck_passed() -> None:
    """Warn if the user has not run dupcheck (csv sentinel absent)."""
    csv_path = TESTS_DIR / "dupcheck.csv"
    if not csv_path.exists():
        print(
            "WARN: dupcheck has not been run yet (outputs/tests/dupcheck.csv not found).\n"
            "      Run 'python film_restore.py dupcheck' first to verify source fps.\n"
            "      Proceeding anyway — but results may be misleading if the source\n"
            "      is not a ~16 fps telecined clip."
        )

def step_analyze() -> None:
    """Analyze motion in the input file and write transforms.trf.

    Must run before any other step. Re-run if you change the input file.
    """
    if not INPUT.exists():
        print(f"ERROR: input file not found: {INPUT}")
        sys.exit(1)
    _require_dupcheck_passed()
    run(
        ["ffmpeg", "-y", "-i", str(INPUT),
         "-vf", f"vidstabdetect=result={TRANSFORMS_FILE}",
         "-f", "null", "-"],
        "Stabilization analysis",
    )


def step_preview() -> None:
    """Render the first PREVIEW_DURATION seconds with the full pipeline.

    Processes from the start so frame numbers stay aligned with transforms.trf.
    Review outputs/tests/preview.mp4 and adjust filter values if needed.

    NOTE: This is also a good point to run the same clip through Topaz Video AI
    or a similar AI tool for a side-by-side comparison before committing to a
    full render.
    """
    _check_transforms()
    out = TESTS_DIR / "preview.mp4"
    run(
        ["ffmpeg", "-y", "-i", str(INPUT),
         "-t", str(PREVIEW_DURATION),
         "-vf", PIPELINE,
         *ENCODE, str(out)],
        f"Preview render (first {PREVIEW_DURATION}s)",
    )
    print(f"\n  Output: {out}")
    print("  Review the clip and adjust filter values in this script if needed.")
    print("  Then re-run 'preview' to confirm, then run 'master'.")


def step_master() -> None:
    """Full render at native resolution → outputs/master_native/.

    Keep this as your archive master. Do not discard the original.
    """
    _check_transforms()
    out = MASTER_DIR / f"{INPUT.stem}_restored.mp4"
    run(
        ["ffmpeg", "-y", "-i", str(INPUT),
         "-vf", PIPELINE,
         *ENCODE, str(out)],
        "Master render (native resolution)",
    )
    print(f"\n  Output: {out}")


def step_delivery() -> None:
    """Full render with 2x lanczos upscale → outputs/delivery_upscaled/.

    For better results, pass the master through Topaz Video AI instead.
    This step is a traditional (non-AI) upscale fallback.
    """
    _check_transforms()
    out = DELIVERY_DIR / f"{INPUT.stem}_restored_2x.mp4"
    run(
        ["ffmpeg", "-y", "-i", str(INPUT),
         "-vf", PIPELINE_WITH_UPSCALE,
         *ENCODE, str(out)],
        "Delivery render (2x lanczos upscale)",
    )
    print(f"\n  Output: {out}")


def _check_transforms() -> None:
    if not TRANSFORMS_FILE.exists():
        print(f"ERROR: {TRANSFORMS_FILE} not found. Run 'analyze' first.")
        sys.exit(1)


# ── Pipeline builder (used by sweep) ─────────────────────────────────────────

def build_pipeline(
    smoothing: int = 20,
    tmix: str | None = None,
    minterpolate: str | None = None,
    upscale: bool = False,
    deduplicate: bool = False,
) -> str:
    """Construct a -vf filter chain for a given parameter set.

    Pipeline order: [deduplicate] → deflicker → stabilize → [tmix] → denoise
                    → sharpen → color → [minterpolate] → [upscale]

    deflicker runs BEFORE stabilize and minterpolate so both receive
    brightness-corrected frames.  Feeding flickery frames into minterpolate
    causes the synthesised tween frames to inherit the brightness discontinuity.
    """
    filters = []
    if deduplicate:
        filters.append(DEDUPLICATE)
    filters += [
        DEFLICKER,
        f"vidstabtransform=input={TRANSFORMS_FILE}:smoothing={smoothing}",
    ]
    if tmix:
        filters.append(tmix)
    filters += [DENOISE, SHARPEN, COLOR]
    if minterpolate:
        filters.append(minterpolate)
    if upscale:
        filters.append(UPSCALE)
    return ",".join(filters)


# ── Jitter scoring ─────────────────────────────────────────────────────────────

def score_video(
    path: Path,
    source_fps: float = TARGET_UNIQUE_FPS,
    reference: Path | None = None,
) -> dict:
    """Score a video clip across multiple quality dimensions.

    Parameters
    ----------
    path        : Path to the video to score.
    source_fps  : True content frame rate of the source material.
                  Used to compute a stride so that only genuine source frames
                  (not synthesised MCI/blend in-betweens) are evaluated for
                  jitter, sharpness, artifact, BRISQUE, SSIM, and PSNR.
                  Luma/flicker is sampled at the full output fps.
    reference   : Path to the original unprocessed clip.  Required for SSIM,
                  PSNR, and VMAF.  If None those metrics are skipped.

    Metrics
    -------
    Jitter  (stride-sampled)
        jitter_mean, jitter_p95  — RANSAC affine global-motion acceleration.
                                   Lower = smoother.
    Flicker (every frame)
        flicker_std              — std of frame-to-frame luma delta.
        flicker_hf_energy        — FFT energy above 3 Hz in the luma signal.
    Sharpness (stride-sampled)
        sharpness_median, sharpness_p10  — Laplacian variance, centre-80% crop.
    Artifact  (stride-sampled)
        artifact_score           — fine/coarse Laplacian ratio, clamped to 50.
    BRISQUE   (stride-sampled, optional — pip install image-quality)
        brisque_mean             — no-reference blind quality (lower = better).
    SSIM / PSNR  (stride-sampled, optional — pip install scikit-image)
        ssim_mean, psnr_mean     — vs reference clip.
    VMAF  (optional — requires FFmpeg libvmaf, reference clip)
        vmaf_mean                — pooled mean VMAF (higher = better, 0–100).
                                   Both clips are resampled to source_fps before
                                   comparison so synthetic in-betweens are
                                   excluded from the score.

    All values are None on failure or missing soft dependency.
    """
    NULL = {
        "jitter_mean": None, "jitter_p95": None,
        "flicker_std": None, "flicker_hf_energy": None,
        "sharpness_median": None, "sharpness_p10": None,
        "artifact_score": None,
        "brisque_mean": None,
        "ssim_mean": None, "psnr_mean": None,
        "vmaf_mean": None,
    }
    try:
        import cv2          # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        print("  WARN: opencv-python/numpy not installed — skipping score.")
        print("        pip install opencv-python numpy")
        return NULL

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return NULL

    fps    = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
    # Stride: evaluate stride-sampled metrics only on genuine source frames,
    # not on synthesised MCI/blend in-betweens.
    stride = max(1, round(fps / source_fps))

    # ── optional soft dependencies ─────────────────────────────────────────────
    try:
        from brisque import BRISQUE as _BRISQUE     # type: ignore
        from PIL import Image as _PIL               # type: ignore
        _brisque_obj  = _BRISQUE()
        _have_brisque = True
    except ImportError:
        _have_brisque = False

    _have_skimage = False
    ref_cap = None
    if reference is not None and reference.exists():
        try:
            from skimage.metrics import (                               # type: ignore
                structural_similarity as _ssim,
                peak_signal_noise_ratio as _psnr,
            )
            _have_skimage = True
            ref_cap = cv2.VideoCapture(str(reference))
            if not ref_cap.isOpened():
                ref_cap = None
        except ImportError:
            pass

    luma_means:     list[float] = []
    sharpness_vals: list[float] = []
    artifact_vals:  list[float] = []
    brisque_vals:   list[float] = []
    ssim_vals:      list[float] = []
    psnr_vals:      list[float] = []
    motions:        list[tuple[float, float, float, float]] = []
    prev_gray_s = None   # gray at previous stride frame
    frame_idx   = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        gray_full = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # ── luma: every frame (flicker is an output-fps signal) ────────────────
        luma_means.append(float(np.mean(gray_full)))

        # ── stride-sampled metrics ─────────────────────────────────────────────
        if frame_idx % stride == 0:
            h, w   = gray_full.shape
            ch, cw = int(h * 0.8), int(w * 0.8)
            y0, x0 = (h - ch) // 2, (w - cw) // 2
            gray   = gray_full[y0:y0 + ch, x0:x0 + cw]

            sharpness_vals.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))

            g1 = cv2.GaussianBlur(gray, (0, 0), 0.8)
            g2 = cv2.GaussianBlur(gray, (0, 0), 2.0)
            e1 = float(np.mean(np.abs(cv2.Laplacian(g1, cv2.CV_64F))))
            e2 = float(np.mean(np.abs(cv2.Laplacian(g2, cv2.CV_64F))))
            artifact_vals.append(min(e1 / max(e2, 0.1), 50.0))

            if _have_brisque:
                try:
                    # BRISQUE needs an RGB PIL image (not grayscale)
                    rgb_crop = cv2.cvtColor(
                        frame[y0:y0 + ch, x0:x0 + cw], cv2.COLOR_BGR2RGB
                    )
                    brisque_vals.append(
                        float(_brisque_obj.score(_PIL.fromarray(rgb_crop)))
                    )
                except Exception:
                    pass

            # SSIM / PSNR: read one reference frame per stride frame
            if ref_cap is not None and _have_skimage:
                ret_r, ref_frame = ref_cap.read()
                if ret_r:
                    ref_gray = cv2.cvtColor(ref_frame, cv2.COLOR_BGR2GRAY)
                    if ref_gray.shape != gray_full.shape:
                        ref_gray = cv2.resize(ref_gray, (gray_full.shape[1], gray_full.shape[0]))
                    ref_crop = ref_gray[y0:y0 + ch, x0:x0 + cw]
                    try:
                        ssim_vals.append(float(_ssim(ref_crop, gray, data_range=255.0)))
                        psnr_vals.append(float(_psnr(ref_crop, gray, data_range=255.0)))
                    except Exception:
                        pass

            # RANSAC affine global-motion (on stride frames only)
            if prev_gray_s is not None:
                prev_pts = cv2.goodFeaturesToTrack(
                    prev_gray_s, maxCorners=400, qualityLevel=0.01,
                    minDistance=8, blockSize=7,
                )
                if prev_pts is not None and len(prev_pts) >= 10:
                    pts, status, _ = cv2.calcOpticalFlowPyrLK(prev_gray_s, gray, prev_pts, None)
                    if pts is not None and status is not None:
                        good_prev = prev_pts[status.ravel() == 1]
                        good_next = pts[status.ravel() == 1]
                        if len(good_prev) >= 10:
                            M, _ = cv2.estimateAffinePartial2D(
                                good_prev, good_next,
                                method=cv2.RANSAC,
                                ransacReprojThreshold=3.0,
                                maxIters=2000,
                                confidence=0.99,
                                refineIters=10,
                            )
                            if M is not None:
                                a, b = float(M[0, 0]), float(M[0, 1])
                                motions.append((
                                    float(M[0, 2]),
                                    float(M[1, 2]),
                                    math.atan2(b, a),
                                    math.sqrt(a * a + b * b),
                                ))
            prev_gray_s = gray

        frame_idx += 1

    cap.release()
    if ref_cap is not None:
        ref_cap.release()

    if len(luma_means) < 4:
        return NULL

    luma  = np.array(luma_means,     dtype=np.float64)
    sharp = np.array(sharpness_vals, dtype=np.float64)
    art   = np.array(artifact_vals,  dtype=np.float64)

    flicker_std = float(np.std(np.diff(luma)))

    if len(luma) >= 8:
        x     = luma - np.mean(luma)
        spec  = np.fft.rfft(x)
        freqs = np.fft.rfftfreq(len(x), d=1.0 / fps)
        mask  = freqs >= 3.0
        flicker_hf = float(np.mean(np.abs(spec[mask]) ** 2)) if np.any(mask) else 0.0
    else:
        flicker_hf = 0.0

    jitter_mean = jitter_p95 = 0.0
    if len(motions) >= 3:
        m    = np.array(motions, dtype=np.float64)
        ddx  = np.diff(m[:, 0])
        ddy  = np.diff(m[:, 1])
        dang = np.diff(m[:, 2])
        dscl = np.diff(m[:, 3])
        jitter = np.sqrt(ddx**2 + ddy**2 + (25.0 * dang)**2 + (200.0 * dscl)**2)
        jitter_mean = float(jitter.mean())
        jitter_p95  = float(np.percentile(jitter, 95))

    result = {
        "jitter_mean":       jitter_mean,
        "jitter_p95":        jitter_p95,
        "flicker_std":       flicker_std,
        "flicker_hf_energy": flicker_hf,
        "sharpness_median":  float(np.median(sharp)) if len(sharp) else 0.0,
        "sharpness_p10":     float(np.percentile(sharp, 10)) if len(sharp) else 0.0,
        "artifact_score":    float(np.mean(art)) if len(art) else 0.0,
        "brisque_mean":      float(np.mean(brisque_vals)) if brisque_vals else None,
        "ssim_mean":         float(np.mean(ssim_vals))    if ssim_vals    else None,
        "psnr_mean":         float(np.mean(psnr_vals))    if psnr_vals    else None,
        "vmaf_mean":         None,
    }

    if reference is not None and reference.exists():
        result["vmaf_mean"] = _score_vmaf(path, reference, source_fps)

    return result


def _score_vmaf(path: Path, reference: Path, source_fps: float) -> float | None:
    """Run FFmpeg libvmaf and return the pooled mean VMAF score.

    Both clips are resampled to source_fps before comparison so that
    synthesised in-between frames (from minterpolate) are excluded.
    Input 0 = reference (original), input 1 = distorted (processed).

    Note: libvmaf on Windows only writes reliably to the current working
    directory, so we use a fixed local filename (cleaned up after).
    """
    import json

    tmp = Path("_vmaf_tmp.json")
    try:
        lavfi = (
            f"[0:v]fps={source_fps},setpts=PTS-STARTPTS[ref];"
            f"[1:v]fps={source_fps},setpts=PTS-STARTPTS[dist];"
            f"[ref][dist]libvmaf=log_path=_vmaf_tmp.json:log_fmt=json"
        )
        cmd = [
            "ffmpeg", "-y",
            "-t", str(PREVIEW_DURATION), "-i", str(reference),
            "-t", str(PREVIEW_DURATION), "-i", str(path),
            "-lavfi", lavfi,
            "-f", "null", "-",
        ]
        subprocess.run(cmd, capture_output=True, timeout=300)
        if not tmp.exists():
            return None
        data = json.loads(tmp.read_text(encoding="utf-8"))
        return float(data["pooled_metrics"]["vmaf"]["mean"])
    except Exception:
        return None
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ── Sweep steps ────────────────────────────────────────────────────────────────

def step_sweep() -> None:
    """Render all SWEEP_CANDIDATES on the preview clip and score each output.

    Outputs go to outputs/sweep/. Already-rendered files are skipped (delete
    to force a re-render). Results are ranked and saved to
    outputs/sweep/sweep_results.csv. Run 'compare' afterwards.
    """
    _check_transforms()
    _require_dupcheck_passed()
    results: list[dict] = []

    for i, candidate in enumerate(SWEEP_CANDIDATES, 1):
        label = candidate["label"]
        out = SWEEP_DIR / f"{label}.mp4"
        vf = build_pipeline(
            smoothing=candidate["smoothing"],
            tmix=candidate.get("tmix"),
            minterpolate=candidate.get("minterpolate"),
        )

        print(f"\n[{i}/{len(SWEEP_CANDIDATES)}] {label}")
        if out.exists():
            print("  Already rendered — scoring existing file. Delete to re-render.")
        else:
            run(
                ["ffmpeg", "-y", "-i", str(INPUT),
                 "-t", str(PREVIEW_DURATION),
                 "-vf", vf,
                 *ENCODE, str(out)],
                f"Sweep render: {label}",
            )

        print(f"  Scoring...", end=" ", flush=True)
        scores = score_video(out, source_fps=TARGET_UNIQUE_FPS, reference=INPUT)
        jm = scores["jitter_mean"]
        vm = scores["vmaf_mean"]
        parts = [f"jitter={jm:.4f}"] if jm is not None else ["(cv2 unavailable)"]
        if vm is not None:
            parts.append(f"vmaf={vm:.1f}")
        print("  ".join(parts))

        results.append({
            "label":             label,
            "smoothing":         candidate["smoothing"],
            "tmix":              candidate.get("tmix") or "",
            "minterpolate":      candidate.get("minterpolate") or "",
            "jitter_mean":       scores["jitter_mean"],
            "jitter_p95":        scores["jitter_p95"],
            "flicker_std":       scores["flicker_std"],
            "flicker_hf_energy": scores["flicker_hf_energy"],
            "sharpness_median":  scores["sharpness_median"],
            "sharpness_p10":     scores["sharpness_p10"],
            "artifact_score":    scores["artifact_score"],
            "brisque_mean":      scores["brisque_mean"],
            "ssim_mean":         scores["ssim_mean"],
            "psnr_mean":         scores["psnr_mean"],
            "vmaf_mean":         scores["vmaf_mean"],
            "file":              str(out),
        })

    _save_and_print_results(results, SWEEP_DIR / "sweep_results.csv")


def step_score() -> None:
    """Re-score all .mp4 files in outputs/sweep/ without re-rendering.

    Useful after adding new candidates or if you want fresh scores.
    Updates sweep_results.csv.
    """
    sweep_files = sorted(SWEEP_DIR.glob("*.mp4")) if SWEEP_DIR.exists() else []
    if not sweep_files:
        print("No files in outputs/sweep/. Run 'sweep' first.")
        return

    results: list[dict] = []
    for path in sweep_files:
        print(f"  Scoring {path.name}...", end=" ", flush=True)
        scores = score_video(path, source_fps=TARGET_UNIQUE_FPS, reference=INPUT)
        jm = scores["jitter_mean"]
        vm = scores["vmaf_mean"]
        parts = [f"jitter={jm:.4f}"] if jm is not None else ["(cv2 unavailable)"]
        if vm is not None:
            parts.append(f"vmaf={vm:.1f}")
        print("  ".join(parts))
        results.append({"label": path.stem, **scores, "file": str(path)})

    _save_and_print_results(results, SWEEP_DIR / "sweep_results.csv")


def step_compare() -> None:
    """Generate a side-by-side video: original clip vs best sweep candidate.

    Reads sweep_results.csv to find the best candidate — ranked by vmaf_mean
    (descending) when available, otherwise by jitter_mean (ascending).
    Both clips are trimmed to PREVIEW_DURATION seconds.
    Output: outputs/tests/compare_orig_vs_<label>.mp4
    """
    csv_path = SWEEP_DIR / "sweep_results.csv"
    if not csv_path.exists():
        print("No sweep_results.csv found. Run 'sweep' first.")
        return

    with csv_path.open(newline="") as f:
        rows = list(csv.DictReader(f))

    def _nonempty(r: dict, key: str) -> bool:
        v = r.get(key, "")
        return v not in ("", "None", None)

    vmaf_rows = [r for r in rows if _nonempty(r, "vmaf_mean")]
    if vmaf_rows:
        vmaf_rows.sort(key=lambda r: float(r["vmaf_mean"]), reverse=True)
        best     = vmaf_rows[0]
        rank_key = f"vmaf_mean={float(best['vmaf_mean']):.2f}"
    else:
        jitter_rows = [r for r in rows if _nonempty(r, "jitter_mean")]
        if not jitter_rows:
            print("No scored results in sweep_results.csv. Run 'score' first.")
            return
        jitter_rows.sort(key=lambda r: float(r["jitter_mean"]))
        best     = jitter_rows[0]
        rank_key = f"jitter_mean={float(best['jitter_mean']):.4f}"

    best_path = Path(best["file"])
    if not best_path.exists():
        print(f"Best candidate file not found: {best_path}")
        return

    # Original clip — simple copy, no filters, same duration for fair comparison
    orig_clip = TESTS_DIR / "original_preview.mp4"
    if not orig_clip.exists():
        run(
            ["ffmpeg", "-y", "-i", str(INPUT),
             "-t", str(PREVIEW_DURATION),
             *ENCODE, str(orig_clip)],
            "Extract original preview clip (no filters)",
        )

    out = TESTS_DIR / f"compare_orig_vs_{best['label']}.mp4"
    run(
        ["ffmpeg", "-y",
         "-i", str(orig_clip),
         "-i", str(best_path),
         "-filter_complex", "hstack",
         *ENCODE, str(out)],
        f"Side-by-side: original vs {best['label']}",
    )
    print(f"\n  Output: {out}")
    print(f"  Best candidate: {best['label']}  {rank_key}")


def _save_and_print_results(results: list[dict], csv_path: Path) -> None:
    """Write results to CSV and print a ranked summary table."""
    fieldnames = [
        "label", "smoothing", "tmix", "minterpolate",
        "jitter_mean", "jitter_p95",
        "flicker_std", "flicker_hf_energy",
        "sharpness_median", "sharpness_p10",
        "artifact_score",
        "brisque_mean",
        "ssim_mean", "psnr_mean",
        "vmaf_mean",
        "file",
    ]
    with csv_path.open("w", newline="") as f:
        present = [k for k in fieldnames if k in (results[0] if results else {})] or fieldnames
        writer = csv.DictWriter(f, fieldnames=present, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)

    scored = [r for r in results if r.get("jitter_mean") is not None]

    # Rank by VMAF (higher = better) when available, else jitter_mean (lower = better)
    have_vmaf   = any(r.get("vmaf_mean")   is not None for r in scored)
    have_brisque = any(r.get("brisque_mean") is not None for r in scored)
    have_ssim   = any(r.get("ssim_mean")   is not None for r in scored)

    if have_vmaf:
        scored.sort(key=lambda r: float(r["vmaf_mean"] or 0), reverse=True)
        rank_note = "vmaf_mean — higher = better"
    else:
        scored.sort(key=lambda r: float(r["jitter_mean"]))
        rank_note = "jitter_mean — lower = smoother"

    print(f"\n── Sweep results (ranked by {rank_note})")
    hdr = f"  {'Rank':<5} {'Label':<26} {'jitter_mn':<11} {'sharpness':<11} {'artifact':<10} {'flicker':<9}"
    if have_brisque:
        hdr += f" {'brisque':<9}"
    if have_ssim:
        hdr += f" {'ssim':<7} {'psnr_dB':<9}"
    if have_vmaf:
        hdr += f" {'vmaf'}"
    print(hdr)

    for rank, r in enumerate(scored, 1):
        flag = "  ← best" if rank == 1 else ""
        jm  = float(r.get("jitter_mean") or 0)
        sh  = float(r.get("sharpness_median") or 0)
        art = float(r.get("artifact_score") or 0)
        fs  = float(r.get("flicker_std") or 0)
        row = f"  {rank:<5} {r['label']:<26} {jm:<11.4f} {sh:<11.1f} {art:<10.2f} {fs:<9.4f}"
        if have_brisque:
            bq = r.get("brisque_mean")
            row += f" {float(bq):<9.1f}" if bq not in (None, "", "None") else f" {'n/a':<9}"
        if have_ssim:
            ss = r.get("ssim_mean")
            pn = r.get("psnr_mean")
            if ss not in (None, "", "None"):
                row += f" {float(ss):<7.4f} {float(pn):<9.2f}"
            else:
                row += f" {'n/a':<7} {'n/a':<9}"
        if have_vmaf:
            vm = r.get("vmaf_mean")
            row += f" {float(vm):<6.2f}" if vm not in (None, "", "None") else " n/a"
        row += flag
        print(row)

    print(f"\n  CSV saved: {csv_path}")
    if scored:
        print(f"  Run 'compare' to generate original vs {scored[0]['label']} side-by-side.")


# ── FFT flicker-frequency analysis ───────────────────────────────────────────

# Short moving-average window used to detrend the luma signal before FFT.
# This removes slow scene-level drift (pans, fades) so rapid frame-level
# flicker is not buried.  Increase if your scenes change very slowly.
FFT_DETREND_WINDOW = 31   # frames  (must be odd; ~2 s at 16 fps)

def step_fftcheck() -> None:
    """FFT flicker analysis — four signals to reveal frame-level flicker.

    Why mean-luma alone is insufficient
    ------------------------------------
    Raw mean-luma is dominated by slow scene drift (panning, fades, lighting
    changes) that produces huge low-frequency peaks and buries flicker.
    The fix is to analyse signals that isolate the RAPID frame-to-frame
    component:

      Signal A  raw mean luma — DC-subtracted only (baseline / reference)
      Signal B  detrended mean luma — subtract a moving-average to remove
                slow drift; only the rapid frame-level variation remains
      Signal C  frame-to-frame absolute difference (MAD) — directly measures
                how much each frame differs from the previous; the most
                sensitive signal for detecting periodic flicker
      Signal D  detrended frame variance — Laplacian variance of each frame
                measures apparent sharpness/grain; periodic variation here
                indicates exposure or focus oscillation

    For 8mm film flicker, expect peaks in signals B/C/D around:
      fps/2  if every other frame is brighter (alternating pattern)
      fps    if each frame flickers independently at the frame rate
      lower  if flicker is irregular / partially periodic

    Outputs (in outputs/tests/)
    ---------------------------
    fftcheck.png  — 4-panel plot (one panel per signal)
    """
    if not INPUT.exists():
        print(f"ERROR: input file not found: {INPUT}")
        sys.exit(1)

    try:
        import cv2          # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        print("ERROR: opencv-python / numpy required.  pip install opencv-python numpy")
        sys.exit(1)

    # ── Read all frames once ───────────────────────────────────────────────
    print(f"\n── FFT flicker analysis")
    print(f"  Input: {INPUT}")
    print("  Reading frames...", end=" ", flush=True)

    cap = cv2.VideoCapture(str(INPUT))
    if not cap.isOpened():
        print(f"ERROR: cannot open {INPUT}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    mean_luma: list[float] = []
    frame_mad:  list[float] = []
    lap_var:    list[float] = []
    prev_gray = None

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean_luma.append(float(np.mean(gray)))
        lap_var.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
        if prev_gray is not None:
            mad = float(np.mean(np.abs(gray.astype(np.int16) - prev_gray.astype(np.int16))))
        else:
            mad = 0.0
        frame_mad.append(mad)
        prev_gray = gray
    cap.release()

    n = len(mean_luma)
    print(f"{n} frames  (fps={fps:.3f})")

    if n < 64:
        print("ERROR: too few frames for meaningful FFT.")
        sys.exit(1)

    # ── Build the four signals ─────────────────────────────────────────────
    def detrend(arr: np.ndarray, window: int) -> np.ndarray:
        """Subtract a centred moving average to remove slow drift."""
        w = max(3, window | 1)   # ensure odd
        kernel = np.ones(w) / w
        trend  = np.convolve(arr, kernel, mode="same")
        return arr - trend

    sig_a = np.array(mean_luma);   sig_a -= sig_a.mean()        # raw luma, DC removed
    sig_b = detrend(np.array(mean_luma), FFT_DETREND_WINDOW)    # detrended luma
    sig_c = np.array(frame_mad);   sig_c -= sig_c.mean()        # frame MAD, DC removed
    sig_d = detrend(np.array(lap_var),   FFT_DETREND_WINDOW)    # detrended sharpness

    signals = [
        (sig_a, "A: raw mean luma (DC removed)",       "steelblue"),
        (sig_b, f"B: detrended mean luma (w={FFT_DETREND_WINDOW})", "darkorange"),
        (sig_c, "C: frame-to-frame MAD (DC removed)",  "green"),
        (sig_d, f"D: detrended Laplacian variance (w={FFT_DETREND_WINDOW})", "purple"),
    ]

    # ── Compute FFT for each signal ────────────────────────────────────────
    def fft_of(sig: np.ndarray):
        vals = np.fft.rfft(sig)
        mag  = np.abs(vals)
        frq  = np.fft.rfftfreq(len(sig), d=1.0 / fps)
        return frq, mag

    print("\n  Top-5 dominant frequencies per signal:")
    results = []
    for sig, label, _ in signals:
        frq, mag = fft_of(sig)
        results.append((frq, mag))
        top = np.argsort(mag)[-5:][::-1]
        print(f"\n  [{label}]")
        for i in top:
            mark = "  <-- flicker candidate" if 1.0 < frq[i] < (fps * 0.6) else ""
            print(f"    {frq[i]:>7.3f} Hz   mag={mag[i]:.2f}{mark}")

    # ── Plot ──────────────────────────────────────────────────────────────
    try:
        import matplotlib        # type: ignore
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # type: ignore
    except ImportError:
        print("  (matplotlib not installed — skipping plot.  pip install matplotlib)")
        return

    max_freq = min(fps * 0.55, 30.0)   # Nyquist-ish cap; no point showing alias zone
    marker_hz = [(fps / 2, f"{fps/2:.1f} Hz (fps/2)"),
                 (fps,     f"{fps:.1f} Hz (fps)")]

    fig, axes = plt.subplots(4, 1, figsize=(14, 14), tight_layout=True)
    fig.suptitle(
        f"Flicker FFT  —  {n} frames @ {fps:.2f} fps  —  {INPUT.name}\n"
        "Signals B and C are most sensitive to frame-level flicker",
        fontsize=10,
    )

    for ax, (frq, mag), (_, label, color) in zip(axes, results, signals):
        mask  = frq <= max_freq
        pfreq = frq[mask]
        pmag  = mag[mask]
        ax.plot(pfreq, pmag, linewidth=0.9, color=color)
        peak  = float(pmag.max()) if len(pmag) else 1.0
        for hz, hlabel in marker_hz:
            if hz <= max_freq:
                ax.axvline(hz, linestyle="--", linewidth=1.0, color="red", alpha=0.7)
                ax.text(hz + max_freq * 0.01, peak * 0.80, hlabel,
                        fontsize=7, color="red", rotation=90, va="top")
        ax.set_xlim(0, max_freq)
        ax.set_ylim(bottom=0)
        ax.set_xlabel("Frequency (Hz)", fontsize=8)
        ax.set_ylabel("Magnitude", fontsize=8)
        ax.set_title(label, fontsize=9)
        ax.grid(True, linewidth=0.3, alpha=0.4)

    ensure_dirs()
    plot_path = TESTS_DIR / "fftcheck.png"
    fig.savefig(str(plot_path), dpi=150)
    plt.close(fig)
    print(f"\n  Plot saved: {plot_path}")


# ── Entry point ────────────────────────────────────────────────────────────────

STEPS = {
    "dupcheck": step_dupcheck,
    "tempseg":  step_tempseg,
    "fftcheck": step_fftcheck,
    "analyze":  step_analyze,
    "preview":  step_preview,
    "master":   step_master,
    "delivery": step_delivery,
    "sweep":    step_sweep,
    "score":    step_score,
    "compare":  step_compare,
}


def main() -> None:
    ensure_dirs()

    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    for name in sys.argv[1:]:
        if name not in STEPS:
            print(f"Unknown step '{name}'. Available: {', '.join(STEPS)}")
            sys.exit(1)
        STEPS[name]()


if __name__ == "__main__":
    main()
