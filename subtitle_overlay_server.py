from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    from local_settings import SETTINGS as LOCAL_SETTINGS
except Exception:
    LOCAL_SETTINGS: dict[str, Any] = {}


PROJECT_DIR = Path(__file__).resolve().parent
HTML_PATH = PROJECT_DIR / "subtitle_overlay.html"
TIMESTAMP_LINE_RE = re.compile(r"^\[(\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]$")


def _read_setting(name: str) -> Any:
    value = os.getenv(name)
    if value is not None and value != "":
        return value
    return LOCAL_SETTINGS.get(name)


def _read_str(name: str, default: str) -> str:
    value = _read_setting(name)
    if value is None:
        return default
    return str(value).strip()


def _read_int(name: str, default: int) -> int:
    value = _read_setting(name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return PROJECT_DIR / path


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _split_blocks(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []

    blocks: list[str] = []
    current: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.strip():
            current.append(line)
            continue
        if current:
            blocks.append("\n".join(current))
            current = []

    if current:
        blocks.append("\n".join(current))
    return blocks


def _split_timestamped_block(block: str) -> tuple[str, list[str]]:
    lines = [line.rstrip() for line in block.splitlines() if line.strip()]
    if not lines:
        return "", []

    match = TIMESTAMP_LINE_RE.fullmatch(lines[0].strip())
    if match is None:
        return "", lines
    return match.group(1), lines[1:]


def _parse_entries(display_text: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for block in _split_blocks(display_text):
        timestamp_text, raw_lines = _split_timestamped_block(block)
        lines = [line.strip() for line in raw_lines if line.strip()]
        if not lines:
            continue
        if timestamp_text:
            translated = lines[0] if len(lines) >= 2 else ""
            origin = "\n".join(lines[1:] if translated else lines).strip()
        else:
            origin = lines[0]
            translated = "\n".join(lines[1:]).strip()
        entries.append(
            {
                "origin": origin,
                "translated": translated,
                "combined": "\n".join(lines),
                "timestamp_text": timestamp_text,
            }
        )
    return entries


@dataclass(frozen=True)
class OverlayConfig:
    host: str
    port: int
    output_path: Path
    origin_output_path: Path
    translated_output_path: Path


def load_config() -> OverlayConfig:
    return OverlayConfig(
        host=_read_str("SUBTITLE_OVERLAY_HOST", "127.0.0.1"),
        port=max(_read_int("SUBTITLE_OVERLAY_PORT", 18082), 1),
        output_path=_resolve_path(_read_str("SUBTITLE_OUTPUT_PATH", "live_subtitle.txt")),
        origin_output_path=_resolve_path(
            _read_str("SUBTITLE_ORIGIN_OUTPUT_PATH", "live_subtitle_origin.txt")
        ),
        translated_output_path=_resolve_path(
            _read_str("SUBTITLE_TRANSLATED_OUTPUT_PATH", "live_subtitle_translated.txt")
        ),
    )


def build_payload(config: OverlayConfig) -> dict[str, Any]:
    display_text = _read_text(config.output_path)
    origin_text = _read_text(config.origin_output_path)
    translated_text = _read_text(config.translated_output_path)
    mtimes = [
        mtime
        for mtime in (
            _mtime(config.output_path),
            _mtime(config.origin_output_path),
            _mtime(config.translated_output_path),
        )
        if mtime is not None
    ]

    return {
        "display_text": display_text,
        "origin_text": origin_text,
        "translated_text": translated_text,
        "entries": _parse_entries(display_text),
        "origin_entries": _split_blocks(origin_text),
        "translated_entries": _split_blocks(translated_text),
        "updated_at": max(mtimes) if mtimes else None,
    }


class OverlayHandler(BaseHTTPRequestHandler):
    server_version = "SubtitleOverlay/1.0"

    @property
    def config(self) -> OverlayConfig:
        return self.server.config  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/subtitle_overlay.html"}:
            self._serve_html()
            return
        if parsed.path == "/api/subtitle":
            self._serve_json(build_payload(self.config))
            return
        if parsed.path == "/healthz":
            self._serve_json({"ok": True})
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _serve_html(self) -> None:
        if not HTML_PATH.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "Overlay HTML not found")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(HTML_PATH.read_bytes())

    def _serve_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def run_server(config: OverlayConfig) -> int:
    server = ThreadingHTTPServer((config.host, config.port), OverlayHandler)
    server.config = config  # type: ignore[attr-defined]
    print(
        f"[subtitle-overlay] listening on http://{config.host}:{config.port}/subtitle_overlay.html"
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[subtitle-overlay] stopped")
        return 0
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve a local subtitle overlay page.")
    parser.add_argument("--host", default=None, help="Host to bind. Defaults to config.")
    parser.add_argument("--port", type=int, default=None, help="Port to bind. Defaults to config.")
    args = parser.parse_args()

    config = load_config()
    if args.host:
        config = OverlayConfig(
            host=args.host,
            port=config.port,
            output_path=config.output_path,
            origin_output_path=config.origin_output_path,
            translated_output_path=config.translated_output_path,
        )
    if args.port:
        config = OverlayConfig(
            host=config.host,
            port=max(args.port, 1),
            output_path=config.output_path,
            origin_output_path=config.origin_output_path,
            translated_output_path=config.translated_output_path,
        )
    return run_server(config)


if __name__ == "__main__":
    raise SystemExit(main())
