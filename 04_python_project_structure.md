# Suggested Python orchestration structure

A good Python project structure is:

- `analyze.py`
    - stabilization analysis
    - optional scene splitting
- `preview.py`
    - generate short test renders for chosen time ranges
- `render.py`
    - apply approved pipeline to whole clips
- `profiles/`
    - JSON or YAML settings for different source types
- `outputs/`
    - `master_native/`
    - `delivery_upscaled/`
    - `tests/`

## Sensible profile presets

### light_cleanup
- stabilize
- deflicker
- mild contrast/color

### standard_cleanup
- stabilize
- deflicker
- mild denoise
- mild sharpen
- color grade

### heavy_cleanup
- stabilize
- deflicker
- stronger denoise
- manual review
- optional AI restore
- upscale

## Review method

For each reel:
1. render 10 to 20 second samples from difficult sections
2. compare original vs restored side by side
3. back off the strongest operations first
