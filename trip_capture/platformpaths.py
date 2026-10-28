from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

_SETTING_KEYS = {"trip_date", "origin", "destination", "fuel", "sites", "output_dir", "browser", "hipass_id",
                 "trip_end_date", "department", "position", "traveler_name", "trip_purpose", "rail_origin", "rail_destination",
                 "rail_return_origin", "rail_return_destination", "transport_mode", "mode_sites"}


def app_data_dir() -> Path:
    directory = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "TripEvidenceCapture"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def load_settings() -> dict:
    try:
        settings = json.loads((app_data_dir() / "settings.json").read_text(encoding="utf-8"))
        if isinstance(settings, dict):
            return {key: value for key, value in settings.items() if key in _SETTING_KEYS}
    except (OSError, ValueError, TypeError):
        pass
    return {}


def save_settings(settings: dict) -> None:
    """Keep only explicitly allowed preferences; credentials never go in JSON."""
    destination = app_data_dir() / "settings.json"
    safe = {key: value for key, value in settings.items() if key in _SETTING_KEYS}
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent, suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump(safe, file, ensure_ascii=False, indent=2)
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
