# FFmpeg baseline recipes

These are conservative baselines, not final universal settings.

## 1. Analyze stabilization

```bash
ffmpeg -i input.mp4 -vf vidstabdetect=result=transforms.trf -f null -
```

## 2. Apply stabilization

```bash
ffmpeg -i input.mp4 -vf "vidstabtransform=input=transforms.trf:smoothing=20" -c:v libx264 -crf 18 -preset slow -c:a copy stabilized.mp4
```

## 3. Add deflicker

```bash
ffmpeg -i input.mp4 -vf "vidstabtransform=input=transforms.trf:smoothing=20,deflicker" -c:v libx264 -crf 18 -preset slow -c:a copy stabilized_deflickered.mp4
```

## 4. Mild denoise

```bash
ffmpeg -i input.mp4 -vf "hqdn3d=1.5:1.5:6:6" -c:v libx264 -crf 18 -preset slow -c:a copy denoised.mp4
```

## 5. Mild sharpen

```bash
ffmpeg -i input.mp4 -vf "unsharp=5:5:0.8:5:5:0.0" -c:v libx264 -crf 18 -preset slow -c:a copy sharpened.mp4
```

## 6. Mild color and contrast correction

```bash
ffmpeg -i input.mp4 -vf "eq=contrast=1.08:saturation=1.12:brightness=0.01" -c:v libx264 -crf 18 -preset slow -c:a copy graded.mp4
```

## 7. Traditional upscale

```bash
ffmpeg -i input.mp4 -vf "scale=2*iw:2*ih:flags=lanczos" -c:v libx264 -crf 18 -preset slow -c:a copy upscaled.mp4
```

## 8. Combined baseline pipeline

```bash
ffmpeg -i input.mp4 \
-vf "vidstabtransform=input=transforms.trf:smoothing=20,deflicker,hqdn3d=1.2:1.2:4.5:4.5,unsharp=5:5:0.6:5:5:0.0,eq=contrast=1.08:saturation=1.10" \
-c:v libx264 -crf 18 -preset slow -c:a copy restored_baseline.mp4
```

## Practical advice

- Tune one operation at a time.
- Export short test sections first.
- Keep a no-upscale master and an upscaled delivery version.
