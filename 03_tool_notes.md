# Tool notes

## FFmpeg
Best for repeatable scripted pipelines, testing, batch work, and baseline restoration passes.

Strengths:
- reproducible
- scriptable from Python
- excellent for stabilization, deflicker, denoise baselines, simple tone work

Weaknesses:
- limited for true dirt/scratch restoration
- less interactive for shot-by-shot judgement

## DaVinci Resolve
Best for manual judgement, grading, selective treatment, and high-quality noise reduction.

Strengths:
- strong color workflow
- temporal/spatial noise reduction in Studio
- good manual control shot by shot

Weaknesses:
- less convenient for bulk automation than FFmpeg
- some advanced tools require Studio

## Topaz Video AI
Best for AI-assisted denoise, restore, sharpen, stabilize, and upscale on difficult consumer footage.

Strengths:
- often strong on perceived detail and upscale
- useful for damaged or very soft transfers

Weaknesses:
- can hallucinate detail
- easy to over-process
- should be treated as an enhancement option, not the sole master workflow

## Manual restoration tools
Best for hero shots and severe damage.

Use when:
- there are large scratches
- there are missing or damaged frames
- automatic tools fail on a small number of important shots
