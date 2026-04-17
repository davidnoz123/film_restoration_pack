# Accepted approaches by cleanup operation

## 1. Stabilization

### What problem this solves
Camera shake, gate weave, jitter, sideways drift, rotation wobble.

### Accepted approaches
- Global 2D stabilization from frame-to-frame motion analysis.
- Two-pass stabilization using a motion-analysis file, then a transform pass.
- In difficult material, selective or shot-by-shot stabilization rather than one setting for a whole reel.
- Cropping or modest zoom after stabilization to hide border exposure.

### Good tools
- FFmpeg `vidstabdetect` + `vidstabtransform`
- NLE stabilizers such as DaVinci Resolve
- AI/video tools such as Topaz Video AI for difficult consumer footage

### Notes
- Start here. If you sharpen or denoise first, the instability often becomes harder to treat cleanly.
- For old film transfers, moderate settings usually look better than aggressive lock-off.

## 2. Deflicker / exposure stabilization

### What problem this solves
Brightness pumping, frame-to-frame exposure shifts, lamp instability, transfer flicker.

### Accepted approaches
- Temporal luminance smoothing across neighboring frames.
- Scene-aware treatment so real lighting changes are preserved.
- Separate luminance stabilization from color balancing where possible.

### Good tools
- FFmpeg `deflicker`
- Dedicated restoration tools
- Manual grading for difficult shots

### Notes
- Deflicker is one of the highest-value steps on old 8 mm.
- Heavy settings can flatten intended flashes or rapid lighting changes.

## 3. Dust, dirt, scratch, and blotch cleanup

### What problem this solves
White specks, black specks, dirt spots, small scratches, transfer debris.

### Accepted approaches
- Temporal defect detection: compare neighboring frames and replace isolated defects.
- Manual paint/patch work for severe or persistent defects.
- Conservative cleanup first, then manual cleanup only where needed.

### Good tools
- Dedicated restoration tools
- Manual clone/paint tools in NLE/compositor
- Some archival workflows use frame-by-frame repair for hero shots

### Notes
- There is no single FFmpeg filter that fully replaces a real dirt/scratch restoration tool.
- Automated dirt removal works best on transient defects, not long moving scratches.

## 4. Noise reduction / grain reduction

### What problem this solves
Scanner noise, transfer compression noise, sensor noise, coarse chroma noise.

### Accepted approaches
- Temporal noise reduction first, because real picture detail is coherent across frames.
- Mild spatial noise reduction after temporal, if still needed.
- Separate treatment for luma vs chroma when possible.
- Preserve some grain/texture so the image does not look plastic.

### Good tools
- DaVinci Resolve Studio temporal/spatial NR
- FFmpeg `hqdn3d` for a simple non-AI baseline
- Neat Video or Topaz Video AI for heavier restoration workflows

### Notes
- For film-originated footage, not all “noise” is bad. Some is natural grain.
- Remove digital ugliness; do not erase all texture.

## 5. Deblur / sharpen / crispness recovery

### What problem this solves
Soft transfers, mild focus loss, muddy edges, low-acutance scans.

### Accepted approaches
- Mild sharpening after denoise, not before.
- Local-contrast enhancement or edge enhancement rather than extreme sharpening.
- Deblur only when softness is modest; severe blur is rarely fully recoverable.
- AI restoration only on copies, with comparison against the source.

### Good tools
- FFmpeg `unsharp` as a baseline
- Resolve sharpening / midtone detail / contrast tools
- Topaz Video AI restore/sharpen models for harder cases

### Notes
- “Crisper” is usually best achieved by a combination of stabilization, deflicker, denoise, careful contrast, and mild sharpening.
- Over-sharpening creates halos and ugly edge chatter.

## 6. Color restoration

### What problem this solves
Faded dyes, color cast, low saturation, poor white balance, weak contrast.

### Accepted approaches
- Set black point, white point, and midtone balance first.
- Correct major cast before adding saturation.
- Grade shot by shot when different reels or scenes have different fading.
- Use reference memory carefully; avoid fake modern colors.

### Good tools
- DaVinci Resolve color page
- FFmpeg `eq`, `curves`, `colorbalance` as a baseline
- AI auto-enhancement only as a starting point, not the final authority

### Notes
- With old 8 mm, conservative color work often looks more believable than vivid modern color.

## 7. Contrast / tonal recovery

### What problem this solves
Flat transfer, weak blacks, clipped whites, muddy mids.

### Accepted approaches
- Curves or lift/gamma/gain style correction.
- Shot-level tuning.
- Separate luminance and color decisions where possible.

### Notes
- Often more valuable than sharpening.
- A better tonal range can make the image look much crisper without any aggressive edge enhancement.

## 8. Frame interpolation / motion smoothing

### What problem this solves
Judder from low frame rate or awkward transfer cadence.

### Accepted approaches
- Use only when there is a clear display reason.
- Keep an untouched native-frame-rate version as well.

### Notes
- This is not a cleanup necessity. It is a presentation choice.
- Interpolation can create artifacts and can change the historic feel of old film.

## 9. Upscaling

### What problem this solves
Low display resolution, weak apparent detail on modern screens.

### Accepted approaches
- Upscale after stabilization, flicker control, and primary cleanup.
- Compare classic resampling against AI upscale.
- Keep the native-resolution restored master too.

### Good tools
- FFmpeg `scale` with Lanczos for a clean traditional baseline
- Topaz Video AI or similar for AI upscale when used carefully

### Notes
- Upscaling does not truly recover missing detail, though AI can improve perceived detail.
- It is often better as the last major creative step.

## 10. Manual shot repair

### What problem this solves
Bad splices, isolated severe damage, missing frames, giant scratches, warped shots.

### Accepted approaches
- Manual intervention shot by shot.
- Freeze/repeat/patch neighboring frames when necessary.
- Separate hero-shot repair from bulk pipeline cleanup.

### Notes
- Some problems do not respond well to global automation.
- The accepted professional approach is often a mostly-automatic pipeline plus manual exceptions.
