# Live Subtitles Development Notes

## Overview

This repository now includes a complete live subtitle sidecar for external video and live streams.

Current default flow:

1. Pull audio directly from `SUBTITLE_SOURCE_URL` with `ffmpeg`
2. Segment the stream into short WAV chunks
3. Run persistent in-process ASR with `faster-whisper`
4. Translate Japanese subtitles to Simplified Chinese asynchronously
5. Write OBS-friendly subtitle files and serve a browser overlay

The current implementation is tuned for low-latency Japanese-to-Chinese subtitles on Windows.

## Main Components

- `main.py`
  - Autostarts the subtitle sidecar and overlay server when enabled in `local_settings.py`
- `live_subtitles.py`
  - Capture, ASR, dedupe, translation, file output, auto-restart
- `subtitle_overlay_server.py`
  - Local HTTP server for browser-source subtitle rendering
- `subtitle_overlay.html`
  - Overlay UI for OBS/browser source
- `subtitle_audio_relay.py`
  - Older fallback path for loopback routing; no longer the default

## Runtime Architecture

```text
SUBTITLE_SOURCE_URL
  -> ffmpeg segment capture
  -> tmp_subtitles/chunks/chunk_xxxxxx.wav
  -> audio preparation / normalization
  -> persistent_faster_whisper
  -> origin subtitle entry
  -> async zh translation
  -> live_subtitle*.txt
  -> subtitle_overlay_server.py
  -> OBS browser source / text source
```

## Current Default Strategy

### Capture

- Backend: `ffmpeg_source_url`
- Why:
  - Avoids Windows loopback instability on this machine
  - Does not require audio to pass through system playback
  - Works directly for YouTube/live URLs

### ASR

- Backend: `persistent_faster_whisper`
- Model: local `faster-whisper-large-v2`
- Device: `cuda`
- Compute type: `float16`
- Why:
  - Per-invocation CLI transcription was too slow and too fragile
  - Persistent model removes process startup overhead
  - GPU inference keeps up with live playback much better

### Segmentation

- Chunk size: `2s`
- Recognition window: `2-4s`
- Holdback: `0.8s`
- Why:
  - `1s` was too fragmented
  - `3s` direct single-window mode was unstable in practice
  - `2s + 4s` is currently the best tradeoff between density and latency

### Translation

- Mode: asynchronous
- Workers: `3`
- Backlog limit: `3`
- Provider order: `deepseek -> glm -> openai`
- Fast path:
  - short interjections are translated locally
  - repeated lines use an in-memory cache
- Failure handling:
  - stale pending entries are dropped first
  - provider cooldown avoids repeatedly hitting a bad provider

## Current Local Settings

`local_settings.py` is intentionally ignored by Git. The current local defaults are:

```python
"SUBTITLE_ASR_BACKEND": "persistent_faster_whisper",
"SUBTITLE_CAPTURE_BACKEND": "ffmpeg_source_url",
"SUBTITLE_WHISPER_DEVICE": "cuda",
"SUBTITLE_WHISPER_COMPUTE_TYPE": "float16",
"SUBTITLE_SEGMENT_SECONDS": 2,
"SUBTITLE_RECOGNITION_WINDOW_SECONDS": 4,
"SUBTITLE_RECOGNITION_MIN_WINDOW_SECONDS": 2,
"SUBTITLE_RECOGNITION_HOLDBACK_SECONDS": 0.8,
"SUBTITLE_CAPTURE_STALL_SECONDS": 12,
"SUBTITLE_CAPTURE_RESTART_COOLDOWN_SECONDS": 3,
"SUBTITLE_HISTORY_LINES": 4,
"SUBTITLE_TRANSLATE_TO_ZH": True,
"SUBTITLE_TRANSLATION_PROVIDER_ORDER": "deepseek,glm,openai",
"SUBTITLE_TRANSLATION_TIMEOUT_SECONDS": 3.2,
"SUBTITLE_TRANSCRIBE_TIMEOUT_SECONDS": 20.0,
"SUBTITLE_TRANSLATION_MAX_TOKENS": 32,
"SUBTITLE_TRANSLATION_PENDING_LIMIT": 3,
"SUBTITLE_TRANSLATION_WORKERS": 3,
"SUBTITLE_SOURCE_URL": "https://www.youtube.com/watch?v=p39l_mrM7Pk",
```

## Reliability Features

### Capture Recovery

- Detects chunk stalls and restarts capture automatically
- Clears stale chunk/prepared files on restart
- Kills stale worker processes on startup

### ASR Fallback

- Preferred backend: `persistent_faster_whisper`
- Fallback backend: `whisper_cli`
- If persistent ASR fails to initialize, runtime falls back automatically

### Subtitle File Writes

- Atomic temp-file replacement first
- Retry loop on file-lock contention
- Final fallback to direct write
- This fixes intermittent Windows `Access denied` errors when OBS/browser briefly locks files

### Noise Control

- Silent chunks are skipped
- Duplicate entries are skipped
- Repeated word and phrase runs are collapsed before translation

## Subtitle Outputs

The sidecar writes three files:

- `live_subtitle.txt`
  - bilingual display
  - Chinese first, Japanese second
- `live_subtitle_origin.txt`
  - Japanese only
- `live_subtitle_translated.txt`
  - Chinese only

Each block includes a timestamp:

```text
[03-08 15:42:27]
喂，你不在吗？这家伙。
おいない?こいつ
```

## Overlay

Overlay server:

- `http://127.0.0.1:18082/subtitle_overlay.html`
- `http://127.0.0.1:18082/api/subtitle`

Recommended OBS browser-source URLs:

- bilingual:
  - `http://127.0.0.1:18082/subtitle_overlay.html?mode=bilingual&count=2&interval=400`
- translated only:
  - `http://127.0.0.1:18082/subtitle_overlay.html?mode=translated&count=2&interval=400`
- origin only:
  - `http://127.0.0.1:18082/subtitle_overlay.html?mode=origin&count=2&interval=400`

For live streaming, `mode=translated` usually gives the best viewer experience.

## Running the System

### Full bot with subtitle autostart

```powershell
python .\main.py
```

### Subtitle sidecar only

```powershell
python -u .\live_subtitles.py
```

### Overlay only

```powershell
python -u .\subtitle_overlay_server.py
```

### Device listing

```powershell
python .\live_subtitles.py --list-devices
```

## Diagnostics

### Main Logs

- `live_subtitles_stdout.log`
- `live_subtitles_stderr.log`
- `subtitle_overlay.log`

### Useful Log Patterns

- `[subtitle][chunk] ...`
  - capture is alive
- `[subtitle][skip] ... reason=transcribe_failed`
  - ASR could not decode this chunk/window
- `[subtitle][skip] ... reason=no_incremental_text`
  - ASR produced nothing new
- `[subtitle][zh] ...`
  - Chinese translation has landed
- `[subtitle][capture] restarting: ...`
  - capture was auto-restarted after a stall

### Quick Checks

```powershell
Get-Content .\live_subtitles_stdout.log -Tail 80
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:18082/api/subtitle | Select-Object -ExpandProperty Content
```

## Known Tradeoffs

- Japanese ASR is now faster than Chinese translation
  - current mitigation: translation workers, short-sentence fast path, cache, queue pruning
- Some short or noisy chunks still fail ASR
  - current mitigation: 2-4s windowing, duplicate suppression, automatic restart
- Translation quality is intentionally biased toward speed
  - current mitigation: low token cap and fast provider ordering

## Why This Is the Current Baseline

This is the first version that satisfies all of these at once:

- works on the current Windows machine
- does not depend on fragile loopback capture
- survives source interruptions
- keeps subtitle output and overlay alive after stalls
- gives near-real-time Japanese output
- keeps Chinese close enough for live viewing

It is the recommended baseline for future work on other games or other live/video sources.
