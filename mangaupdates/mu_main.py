import os
import json
import re
import shutil
import time
from datetime import datetime

import httpx

from mangaupdates.config.mu_config import (
    USERNAME, PASSWORD, API_BASE_URL, EXPORTS_DIR,
    MAX_EXPORTS, ITEMS_PER_PAGE, MAX_RETRIES, RETRY_DELAY,
    setup_logging,
)

log = setup_logging()


# ── HTTP ──────────────────────────────────────────────────────────────[...]

def _api_request(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """Make a request with automatic retry on transient errors."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = getattr(client, method)(url, **kwargs)
            if resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"Server error {resp.status_code}",
                    request=resp.request, response=resp,
                )
            return resp
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as exc:
            if attempt < MAX_RETRIES:
                log.warning("Attempt %d/%d failed: %s – retrying in %ds…", attempt, MAX_RETRIES, exc, RETRY_DELAY)
                time.sleep(RETRY_DELAY)
            else:
                log.error("All %d attempts failed: %s", MAX_RETRIES, exc)
                raise


# ── Auth ──────────────────────────────────────────────────────────────[...]

def login(client: httpx.Client) -> None:
    if not USERNAME or not PASSWORD:
        log.error("MU_USERNAME or MU_PASSWORD not set in .env")
        raise SystemExit(1)

    log.info("Logging in as '%s'…", USERNAME)
    resp = _api_request(client, "put", f"{API_BASE_URL}/account/login", json={
        "username": USERNAME,
        "password": PASSWORD,
    })

    if resp.status_code == 401:
        log.error("Login failed – invalid credentials")
        raise SystemExit(1)
    resp.raise_for_status()

    token = resp.json().get("context", {}).get("session_token")
    if not token:
        log.error("Login failed – no session token in response")
        raise SystemExit(1)

    client.headers["Authorization"] = f"Bearer {token}"
    log.info("Login successful")


def logout(client: httpx.Client) -> None:
    try:
        client.post(f"{API_BASE_URL}/account/logout")
        log.info("Logged out")
    except Exception as exc:
        log.warning("Logout failed: %s", exc)


# ── Lists ─────────────────────────────────────────────────────────────��[...]

def fetch_lists(client: httpx.Client) -> list[dict]:
    log.info("Fetching user lists…")
    resp = _api_request(client, "get", f"{API_BASE_URL}/lists")
    resp.raise_for_status()
    lists = resp.json()
    log.info("Found %d list(s): %s", len(lists), ", ".join(lst["title"] for lst in lists))
    return lists


def fetch_list_items(client: httpx.Client, list_id: int, title: str) -> list[dict]:
    """Paginate through a single list and return all items."""
    all_items: list[dict] = []
    page = 1
    max_pages = 500

    while page <= max_pages:
        resp = _api_request(client, "post", f"{API_BASE_URL}/lists/{list_id}/search", json={
            "page": page,
            "perpage": ITEMS_PER_PAGE,
        })
        resp.raise_for_status()

        data = resp.json()
        results = data.get("results", [])
        total = data.get("total_hits", 0)
        all_items.extend(results)

        if len(all_items) >= total or not results:
            break
        page += 1
    else:
        log.warning("%s: hit page limit (%d) – list may be incomplete", title, max_pages)

    log.info("  %s: %d item(s)", title, len(all_items))
    return all_items


# ── Save ──────────────────────────────────────────────────────────────[...]

def _sanitize_filename(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*]', "_", name).strip().strip(".")
    return safe if safe else "Unnamed_List"


def save_exports(exports: dict[str, list[dict]], folder: str) -> None:
    used_names: set[str] = set()
    for title, items in exports.items():
        safe_title = _sanitize_filename(title)
        unique_title = safe_title
        counter = 2
        while unique_title in used_names:
            unique_title = f"{safe_title}_{counter}"
            counter += 1
        used_names.add(unique_title)

        path = os.path.join(folder, f"{unique_title}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        log.info("  Saved %s (%d items)", path, len(items))


# ── Compare ─────────────────────────────────────────────────────────────[...]

def _parse_folder_date(name: str) -> datetime:
    try:
        return datetime.strptime(name, "%d.%m.%Y_%H-%M-%S")
    except ValueError:
        return datetime.min


def _find_previous_export(current_folder: str) -> str | None:
    if not os.path.isdir(EXPORTS_DIR):
        return None
    current_dt = _parse_folder_date(os.path.basename(current_folder))
    folders = sorted(
        (d for d in os.listdir(EXPORTS_DIR)
         if os.path.isdir(os.path.join(EXPORTS_DIR, d)) and _parse_folder_date(d) < current_dt),
        key=_parse_folder_date,
    )
    return os.path.join(EXPORTS_DIR, folders[-1]) if folders else None


def _get_series_ids(items: list[dict]) -> dict[int, str]:
    result: dict[int, str] = {}
    for item in items:
        series = item.get("record", {}).get("series", {})
        sid = series.get("id")
        if sid is not None:
            result[sid] = series.get("title", "Unknown")
    return result


def _load_previous_list(prev_folder: str, title: str) -> dict[int, str]:
    path = os.path.join(prev_folder, f"{_sanitize_filename(title)}.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _get_series_ids(json.load(f))
    except (json.JSONDecodeError, OSError):
        return {}


def compare_exports(current_folder: str, exports: dict[str, list[dict]]) -> None:
    prev_folder = _find_previous_export(current_folder)
    if not prev_folder:
        log.info("No previous export found – skipping comparison")
        return

    log.info("")
    log.info("=" * 50)
    log.info("Changes since last export (%s)", os.path.basename(prev_folder))
    log.info("=" * 50)

    # Build global sid→list maps for movement detection
    prev_sid_to_list: dict[int, str] = {}
    for title in exports:
        for sid in _load_previous_list(prev_folder, title):
            prev_sid_to_list[sid] = title

    cur_sid_to_list: dict[int, str] = {}
    for title, items in exports.items():
        for sid in _get_series_ids(items):
            cur_sid_to_list[sid] = title

    # Detect movements
    moved: dict[int, tuple[str, str, str]] = {}
    for sid, new_list in cur_sid_to_list.items():
        old_list = prev_sid_to_list.get(sid)
        if old_list and old_list != new_list:
            for items in exports.values():
                name_map = _get_series_ids(items)
                if sid in name_map:
                    moved[sid] = (name_map[sid], old_list, new_list)
                    break

    has_changes = False

    if moved:
        has_changes = True
        log.info("  Moved series:")
        for sid, (name, old_list, new_list) in moved.items():
            log.info("    ~ %s: %s -> %s", name, old_list, new_list)
        log.info("")

    moved_sids = set(moved)

    for title, current_items in exports.items():
        prev_ids = _load_previous_list(prev_folder, title)
        if not prev_ids and not os.path.isfile(
            os.path.join(prev_folder, f"{_sanitize_filename(title)}.json")
        ):
            log.info("  [%s] NEW LIST – %d item(s)", title, len(current_items))
            has_changes = True
            continue

        current_ids = _get_series_ids(current_items)
        added   = set(current_ids) - set(prev_ids) - moved_sids
        removed = set(prev_ids) - set(current_ids) - moved_sids
        diff    = len(current_items) - len(prev_ids)
        sign    = "+" if diff >= 0 else ""

        if not added and not removed:
            if diff == 0:
                log.info("  [%s] No changes (%d items)", title, len(current_items))
            else:
                has_changes = True
                log.info("  [%s] %d -> %d (%s%d) (movements only)",
                         title, len(prev_ids), len(current_items), sign, diff)
            continue

        has_changes = True
        log.info("  [%s] %d -> %d (%s%d)", title, len(prev_ids), len(current_items), sign, diff)
        for sid in added:
            log.info("    + %s", current_ids[sid])
        for sid in removed:
            log.info("    - %s", prev_ids[sid])

    current_safe = {_sanitize_filename(t) for t in exports}
    for f in os.listdir(prev_folder):
        if f.endswith(".json") and f[:-5] not in current_safe:
            has_changes = True
            log.info("  [%s] LIST REMOVED", f[:-5])

    if not has_changes:
        log.info("  No changes detected across all lists")


def rotate_exports() -> None:
    if not os.path.isdir(EXPORTS_DIR):
        return
    folders = sorted(
        [d for d in os.listdir(EXPORTS_DIR) if os.path.isdir(os.path.join(EXPORTS_DIR, d))],
        key=_parse_folder_date,
    )
    while len(folders) > MAX_EXPORTS:
        oldest = folders.pop(0)
        path = os.path.join(EXPORTS_DIR, oldest)
        try:
            shutil.rmtree(path)
            log.info("Deleted old export: %s", oldest)
        except OSError as exc:
            log.warning("Could not delete %s: %s", oldest, exc)


def main() -> None:
    log.info("=" * 50)
    log.info("MangaUpdates List Exporter")
    log.info("=" * 50)

    start = time.time()
    folder = os.path.join(EXPORTS_DIR, datetime.now().strftime("%d.%m.%Y_%H-%M-%S"))
    os.makedirs(folder, exist_ok=True)

    with httpx.Client(timeout=30) as client:
        login(client)
        try:
            lists = fetch_lists(client)
            if not lists:
                log.warning("No lists found for this account")
                return

            log.info("Exporting lists…")
            exports = {
                lst["title"]: fetch_list_items(client, lst["list_id"], lst["title"])
                for lst in lists
            }

            log.info("Saving exports…")
            save_exports(exports, folder)
            log.info("Exports saved to: %s", folder)

            compare_exports(folder, exports)
            rotate_exports()

            elapsed = time.time() - start
            total_items = sum(len(v) for v in exports.values())
            log.info("")
            log.info("=" * 50)
            log.info("Summary: %d list(s), %d total item(s) in %.1fs",
                     len(exports), total_items, elapsed)
            log.info("=" * 50)

        finally:
            logout(client)

    log.info("Done!")


if __name__ == "__main__":
    main()
