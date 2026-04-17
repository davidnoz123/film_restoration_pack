# Player Subproject

Synchronized side-by-side video comparison tool for evaluating sweep candidates
in a browser. Driven by `sweep_results.csv`; launched via `film_restore.py serve`.

---

## Purpose

After running `sweep`, the candidate `.mp4` files sit in `outputs/sweep/` alongside
`sweep_results.csv` with all quality metrics. Inspecting them one-by-one in a media
player is slow. This tool lets you:

- Pick any two candidates from dropdowns
- Watch them synchronized, side by side
- See all metric values beneath each player
- Loop a region, scrub, and switch pairs instantly

---

## Architecture

| Layer | Technology | Notes |
|-------|-----------|-------|
| Server | Flask (Python) | Byte-range video serving so seeking works in browser |
| UI | Single HTML page | Vanilla JS + CSS, no framework |
| Data | `sweep_results.csv` | Flask reads this at startup and injects into the page |
| Launch | `python film_restore.py serve` | Starts Flask on `localhost:5000`, opens browser |

New dependency: `pip install flask`

---

## File Layout

```
film_restoration_pack/
├── film_restore.py          ← add step_serve() here
└── player/
    └── index.html           ← self-contained UI (JS inline, no build step)
```

Flask serves:
- `GET /`                    → `player/index.html`
- `GET /videos/<filename>`   → byte-range file from `outputs/sweep/`
- `GET /api/candidates`      → JSON array of candidates + metrics from CSV

---

## UI Layout

```
┌──────────────────────────────────────────────────────────────────┐
│  Film Restoration — Candidate Comparison                         │
├─────────────────────────────┬────────────────────────────────────┤
│  [ s40_mi50_mci        ▾ ]  │  [ s30_mi50_blend         ▾ ]     │
│  ┌───────────────────────┐  │  ┌───────────────────────┐        │
│  │                       │  │  │                       │        │
│  │       video A         │  │  │       video B         │        │
│  │                       │  │  │                       │        │
│  └───────────────────────┘  │  └───────────────────────┘        │
│  vmaf 2.0  sharp 44.5       │  vmaf 0.8  sharp 49.6             │
│  brisque 53.9  jitter 3.10  │  brisque 52.2  jitter 3.41        │
│  ssim 0.480  psnr 12.34 dB  │  ssim 0.494  psnr 12.86 dB       │
├─────────────────────────────┴────────────────────────────────────┤
│  [▶ Play]  [⏸ Pause]  [⟳ Loop]                                   │
│  ──────────●───────────────────────────────────  0:08 / 0:20    │
└──────────────────────────────────────────────────────────────────┘
```

---

## Sync Behaviour

- Left player is **master**: drives `timeupdate` events
- Right player is **follower**: if `|currentTime_B - currentTime_A| > 80ms`, snap to master time
- Play/pause/seek on either player propagates to both
- Loop toggle: when master reaches end, both seek to 0 and continue playing
- Keyboard shortcuts: `Space` = play/pause, `←/→` = ±5s, `L` = toggle loop

---

## Metric Display

All metrics pulled from `sweep_results.csv` at `/api/candidates`.
Displayed beneath each player with colour coding:

| Metric | Better when | Highlight colour |
|--------|-------------|-----------------|
| vmaf_mean | higher | green if > 5, amber if 1–5, red if < 1 |
| sharpness_median | higher | green if > 45, amber if 25–45, red if < 25 |
| brisque_mean | lower | green if < 50, amber if 50–60, red if > 60 |
| jitter_mean | lower | green if < 1, amber if 1–3, red if > 3 |
| ssim_mean | higher | neutral (low for all restoration outputs) |
| psnr_mean | higher | neutral (low for all restoration outputs) |

---

## Flask Routes

```python
GET /
    Returns player/index.html

GET /videos/<filename>
    Serves outputs/sweep/<filename> with byte-range support
    Only allows .mp4 files in the sweep directory (no path traversal)

GET /api/candidates
    Returns JSON:
    [
      {
        "label": "s40_mi50_mci",
        "file": "s40_mi50_mci.mp4",
        "vmaf_mean": 2.04,
        "sharpness_median": 44.5,
        "brisque_mean": 53.9,
        "jitter_mean": 3.10,
        "ssim_mean": 0.480,
        "psnr_mean": 12.34
      },
      ...
    ]
```

---

## Security Notes

- `GET /videos/<filename>`: validate that `filename` contains no path separators
  and ends in `.mp4` before constructing the file path — prevents directory traversal.
- Bind to `127.0.0.1` only (not `0.0.0.0`) — local tool, not a networked service.
- No user input is written to disk.

---

## Implementation Steps

1. `pip install flask` (add to requirements note in `film_restore.py` header)
2. Create `player/index.html` — single file, all JS/CSS inline
3. Add `step_serve()` to `film_restore.py`:
   - Read `sweep_results.csv`
   - Register Flask routes
   - Call `webbrowser.open("http://127.0.0.1:5000")`
   - Call `app.run(host="127.0.0.1", port=5000)`
4. Register `"serve"` in the `STEPS` dispatch dict
5. Update the usage block in the module docstring

---

## Open Questions

- Should `serve` also expose `outputs/tests/` files (previews, compare clips)?
- Loop region markers (in/out points) — useful for focusing on a specific pan shot?
- Export: button to copy the selected candidate label to clipboard for use in `master`?
