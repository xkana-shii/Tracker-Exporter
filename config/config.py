import os
from dotenv import load_dotenv, find_dotenv

# Load environment variables from repo-root .env once for the whole run.
load_dotenv(find_dotenv())

# Repository root
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Base directories for exports and logs (per-tracker subfolders will be created)
EXPORTS_BASE = os.path.join(REPO_ROOT, "exports")
LOGS_BASE = os.path.join(REPO_ROOT, "logs")

# Ensure the base directories exist
os.makedirs(EXPORTS_BASE, exist_ok=True)
os.makedirs(LOGS_BASE, exist_ok=True)

# Mapping of tracker -> (username_env, password_env)
_TRACKER_ENV_VARS = {
    "mangaupdates": ("MU_USERNAME", "MU_PASSWORD"),
    "myanimelist": ("MAL_USERNAME", "MAL_PASSWORD"),
    "mangabaka": ("MB_EMAIL", "MB_PASSWORD"),
}

# Mapping of tracker -> env var name used to enable/disable the tracker.
_TRACKER_ENABLED_ENV = {
    "mangaupdates": "MANGAUPDATES",
    "myanimelist": "MYANIMELIST",
    "mangabaka": "MANGABAKA",
}


def _is_placeholder(value: str) -> bool:
    """Return True if the env value is empty or looks like a placeholder."""
    if value is None:
        return True
    v = str(value).strip().lower()
    if v == "":
        return True
    indicators = ("your", "username", "password", "email")
    return any(ind in v for ind in indicators)


def _parse_bool_env(value: str | None, default: bool) -> bool:
    """
    Parse boolean environment variable strings.

    Only accepts "true" or "false" (case-insensitive). If the env var is absent
    or contains any other value, the provided default is returned.
    """
    if value is None:
        return default
    v = str(value).strip().lower()
    if v == "true":
        return True
    if v == "false":
        return False
    return default


def get_tracker_credentials(tracker: str) -> tuple[str, str]:
    """
    Return the (username_or_email, password) tuple for the given tracker by reading env vars.
    If the tracker is unknown, returns ("", "").
    Note: For MyAnimeList this reads MAL_USERNAME (no fallback).
    """
    tracker = tracker.lower()
    if tracker not in _TRACKER_ENV_VARS:
        return "", ""
    user_env, pass_env = _TRACKER_ENV_VARS[tracker]
    username = os.getenv(user_env, "")
    password = os.getenv(pass_env, "")
    return username or "", password or ""


def tracker_exports_dir(tracker: str) -> str:
    """Return the exports directory path for a tracker, e.g. <repo>/exports/mangaupdates"""
    path = os.path.join(EXPORTS_BASE, tracker)
    os.makedirs(path, exist_ok=True)
    return path


def tracker_logs_dir(tracker: str) -> str:
    """Return the logs directory path for a tracker, e.g. <repo>/logs/mangaupdates"""
    path = os.path.join(LOGS_BASE, tracker)
    os.makedirs(path, exist_ok=True)
    return path


def is_tracker_enabled(tracker: str, default: bool = True) -> tuple[bool, str]:
    """
    Return (enabled, reason). If the corresponding env flag exists, use it.
    If the env var is absent, return default (True by default).
    Note: flags must be exactly "true" or "false" (case-insensitive).
    """
    tracker = tracker.lower()
    env_name = _TRACKER_ENABLED_ENV.get(tracker)
    if not env_name:
        return default, ""
    raw = os.getenv(env_name)
    if raw is None:
        raw = os.getenv(env_name.lower())
    if raw is None:
        return default, ""
    enabled = _parse_bool_env(raw, default)
    reason = "" if enabled else f"disabled via {env_name}={raw}"
    return enabled, reason


def should_run_tracker(tracker: str) -> bool:
    """Return True if the tracker is enabled via env AND has credentials that appear valid."""
    enabled, _ = is_tracker_enabled(tracker)
    if not enabled:
        return False
    username, password = get_tracker_credentials(tracker)
    return not (_is_placeholder(username) or _is_placeholder(password))


def should_run_tracker_with_reason(tracker: str) -> tuple[bool, str]:
    """
    Return (should_run, reason). reason is non-empty when should_run is False.
    Rules:
      - If the enable flag exists and is "false" -> should_run=False.
      - If enabled and credentials missing/placeholder -> should_run=False with reason.
      - If enabled and credentials present -> should_run=True.
    """
    enabled, reason = is_tracker_enabled(tracker)
    if not enabled:
        return False, reason or "disabled via env flag"

    username, password = get_tracker_credentials(tracker)
    if username == "" and password == "":
        return False, "both credentials missing"
    if username == "":
        return False, "username/email missing"
    if password == "":
        return False, "password missing"
    if _is_placeholder(username):
        return False, f"username/email looks like placeholder: '{username}'"
    if _is_placeholder(password):
        return False, f"password looks like placeholder: '{password}'"
    return True, ""
