"""
Copy this file to `local_settings.py` and adjust the values for your machine.

Notes:
- API keys are recommended via environment variables:
  - OPENAI_API_KEY
  - DEEPSEEK_API_KEY or DEEPSEEK_KEY
  - GLM_API_KEY
- `local_settings.py` is ignored by Git on purpose.
- This template focuses on the current live-subtitle pipeline and the basic bot runtime.
"""

SETTINGS = {
    # Basic TTS
    "TTS_PROVIDER": "edge",
    "EDGE_TTS_VOICE": "zh-CN-XiaoyiNeural",
    "EDGE_TTS_RATE": "+2%",
    "EDGE_TTS_PITCH": "-2Hz",
    "EDGE_TTS_VOLUME": "+0%",
    "EDGE_TTS_TIMEOUT_SECONDS": 25,

    # Player / misc
    "MPV_AUDIO_DEVICE": "",
    "AUDIO_QUEUE_MAX_SIZE": 24,
    "MAX_KNOWN_USERS": 4000,
    "USER_CACHE_TTL_SECONDS": 7200,
    "MAX_RESOLVED_USER_NAMES": 4000,
    "MAX_FAILED_RESOLUTIONS": 1000,
    "SIDECAR_LOG_MAX_BYTES": 10485760,
    "SIDECAR_LOG_BACKUPS": 3,
    "HEARTBEAT_ENABLED": True,
    "HEARTBEAT_INTERVAL_SECONDS": 180,

    # Live subtitles: binaries and model paths
    "SUBTITLE_FFMPEG_PATH": r"E:\path\to\ffmpeg.exe",
    "SUBTITLE_WHISPER_PATH": r"E:\path\to\faster-whisper-xxl.exe",
    "SUBTITLE_WHISPER_MODEL_DIR": r"E:\path\to\models",

    # Live subtitles: capture and ASR
    "SUBTITLE_ASR_BACKEND": "persistent_faster_whisper",  # or "whisper_cli"
    "SUBTITLE_CAPTURE_BACKEND": "ffmpeg_source_url",      # or "soundcard_loopback" / "ffmpeg_dshow"
    "SUBTITLE_LOOPBACK_SPEAKER": "default",
    "SUBTITLE_SAMPLE_RATE": 48000,
    "SUBTITLE_AUDIO_DEVICE": "",
    "SUBTITLE_SOURCE_LANGUAGE": "ja",
    "SUBTITLE_WHISPER_MODEL": "large-v2",
    "SUBTITLE_WHISPER_DEVICE": "cuda",
    "SUBTITLE_WHISPER_COMPUTE_TYPE": "float16",

    # Live subtitles: timing
    "SUBTITLE_SEGMENT_SECONDS": 2,
    "SUBTITLE_RECOGNITION_WINDOW_SECONDS": 4,
    "SUBTITLE_RECOGNITION_MIN_WINDOW_SECONDS": 2,
    "SUBTITLE_RECOGNITION_HOLDBACK_SECONDS": 0.8,
    "SUBTITLE_CAPTURE_STALL_SECONDS": 12,
    "SUBTITLE_CAPTURE_RESTART_COOLDOWN_SECONDS": 3,
    "SUBTITLE_HISTORY_LINES": 4,

    # Live subtitles: translation
    "SUBTITLE_TRANSLATE_TO_ZH": True,
    "SUBTITLE_TRANSLATION_PROVIDER_ORDER": "deepseek,glm,openai",
    "SUBTITLE_TRANSLATION_TIMEOUT_SECONDS": 3.2,
    "SUBTITLE_TRANSCRIBE_TIMEOUT_SECONDS": 20.0,
    "SUBTITLE_TRANSLATION_MAX_TOKENS": 32,
    "SUBTITLE_TRANSLATION_PENDING_LIMIT": 3,
    "SUBTITLE_TRANSLATION_WORKERS": 3,

    # Live subtitles: runtime / output
    "SUBTITLE_AUTOSTART": True,
    "SUBTITLE_LOG_PATH": "live_subtitles.log",
    "SUBTITLE_OVERLAY_AUTOSTART": True,
    "SUBTITLE_OVERLAY_SCRIPT_PATH": "subtitle_overlay_server.py",
    "SUBTITLE_OVERLAY_LOG_PATH": "subtitle_overlay.log",
    "SUBTITLE_OVERLAY_HOST": "127.0.0.1",
    "SUBTITLE_OVERLAY_PORT": 18082,
    # Preferred: put your URL in this text file (first non-empty line).
    # The sidecar can hot-switch when this file changes.
    "SUBTITLE_SOURCE_URL_FILE": "subtitle_source_url.txt",
    "SUBTITLE_SOURCE_URL_RELOAD_SECONDS": 2.0,
    # Fallback URL when SUBTITLE_SOURCE_URL_FILE is missing/empty.
    "SUBTITLE_SOURCE_URL": "https://www.youtube.com/watch?v=YOUR_SOURCE_ID",
    # For source-url capture: prefer low bitrate formats with audio.
    "SUBTITLE_SOURCE_FORMAT": "worst[acodec!=none]/worst",
    "SUBTITLE_RELAY_AUDIO_DEVICE": "",
    "SUBTITLE_RELAY_YTDLP_PATH": r"E:\path\to\yt-dlp.exe",
    "SUBTITLE_OUTPUT_PATH": "live_subtitle.txt",
    "SUBTITLE_ORIGIN_OUTPUT_PATH": "live_subtitle_origin.txt",
    "SUBTITLE_TRANSLATED_OUTPUT_PATH": "live_subtitle_translated.txt",
}
