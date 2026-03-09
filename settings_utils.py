from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping


def load_local_settings() -> dict[str, Any]:
    try:
        from local_settings import SETTINGS as local_settings  # type: ignore
    except Exception:
        return {}
    if isinstance(local_settings, dict):
        return local_settings
    return {}


def read_setting(name: str, local_settings: Mapping[str, Any]) -> Any:
    value = os.getenv(name)
    if value is not None and value != "":
        return value
    return local_settings.get(name)


def read_str(name: str, default: str, local_settings: Mapping[str, Any]) -> str:
    value = read_setting(name, local_settings)
    if value is None:
        return default
    return str(value).strip()


def read_bool(name: str, default: bool, local_settings: Mapping[str, Any]) -> bool:
    value = read_setting(name, local_settings)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def read_int(name: str, default: int, local_settings: Mapping[str, Any]) -> int:
    value = read_setting(name, local_settings)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except ValueError:
        return default


def read_float(name: str, default: float, local_settings: Mapping[str, Any]) -> float:
    value = read_setting(name, local_settings)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def resolve_path(raw_path: str, project_dir: Path) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    return project_dir / path
