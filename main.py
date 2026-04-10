import os
import json
import re
import shutil
from datetime import datetime

import httpx

from config.config import (
    USERNAME, PASSWORD, API_BASE_URL, EXPORTS_DIR,
    MAX_EXPORTS, ITEMS_PER_PAGE, setup_logging,
)

log = setup_logging()


def login(client: httpx.Client) -> str:
    """Authenticate and return a session token."""
    if not USERNAME or not PASSWORD:
        log.error("MU_USERNAME or MU_PASSWORD not set in .env file")
        raise SystemExit(1)

    log.info("Logging in as '%s'...", USERNAME)
    resp = client.put(f"{API_BASE_URL}/account/login", json={
        "username": USERNAME,
        "password": PASSWORD,
    })

    if resp.status_code == 401:
        log.error("Login failed – invalid credentials")
        raise SystemExit(1)
    resp.raise_for_status()

    data = resp.json()
    token = data.get("context", {}).get("session_token")
    if not token:
        log.error("No session token in login response (status: %s)", data.get("status", "unknown"))
        raise SystemExit(1)

    log.info("Login successful")
    return token


def logout(client: httpx.Client) -> None:
    """End the API session."""
    try:
        client.post(f"{API_BASE_URL}/account/logout")
        log.info("Logged out")
    except Exception as exc:
        log.warning("Logout failed: %s", exc)


def fetch_lists(client: httpx.Client) -> list[dict]:
    """Get all user lists (built-in + custom)."""
    log.info("Fetching user lists...")
    resp = client.get(f"{API_BASE_URL}/lists")
    resp.raise_for_status()
    lists = resp.json()
    log.info("Found %d list(s): %s", len(lists), ", ".join(lst["title"] for lst in lists))
    return lists


def export_list(client: httpx.Client, list_id: int, title: str) -> list[dict]:
    """Paginate through a single list and return all items."""
    all_items = []
    page = 1
    max_pages = 500  # Safety limit to prevent infinite loops

    while page <= max_pages:
        resp = client.post(f"{API_BASE_URL}/lists/{list_id}/search", json={
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
        log.warning("  %s: hit page limit (%d) – list may be incomplete", title, max_pages)

    log.info("  %s: %d item(s)", title, len(all_items))
    return all_items


def sanitize_filename(name: str) -> str:
    """Remove characters unsafe for filenames."""
    safe = re.sub(r'[<>:"/\\|?*]', '_', name).strip().strip('.')
    return safe if safe else "Unnamed_List"


def save_exports(exports: dict[str, list[dict]]) -> str:
    """Save each list to a timestamped folder. Returns the folder path."""
    folder_name = datetime.now().strftime("%Y_%m_%d_%H-%M-%S")
    folder_path = os.path.join(EXPORTS_DIR, folder_name)
    os.makedirs(folder_path, exist_ok=True)

    used_names = set()
    for title, items in exports.items():
        safe_title = sanitize_filename(title)
        # Prevent duplicate filenames from different list titles
        unique_title = safe_title
        counter = 2
        while unique_title in used_names:
            unique_title = f"{safe_title}_{counter}"
            counter += 1
        used_names.add(unique_title)

        file_path = os.path.join(folder_path, f"{unique_title}.json")
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        log.info("  Saved %s (%d items)", file_path, len(items))

    return folder_path


def get_series_ids(items: list[dict]) -> dict[int, str]:
    """Extract {series_id: title} from a list export."""
    result = {}
    for item in items:
        record = item.get("record", {})
        series = record.get("series", {})
        sid = series.get("id")
        title = series.get("title", "Unknown")
        if sid is not None:
            result[sid] = title
    return result


def find_previous_export(current_folder: str) -> str | None:
    """Find the most recent export folder before current_folder."""
    if not os.path.isdir(EXPORTS_DIR):
        return None

    current_name = os.path.basename(current_folder)
    folders = sorted(
        d for d in os.listdir(EXPORTS_DIR)
        if os.path.isdir(os.path.join(EXPORTS_DIR, d)) and d < current_name
    )
    if folders:
        return os.path.join(EXPORTS_DIR, folders[-1])
    return None


def compare_exports(current_folder: str, exports: dict[str, list[dict]]) -> None:
    """Compare current export with the previous one and print changes."""
    prev_folder = find_previous_export(current_folder)
    if not prev_folder:
        log.info("No previous export found – skipping comparison")
        return

    prev_name = os.path.basename(prev_folder)
    log.info("")
    log.info("=" * 50)
    log.info("Changes since last export (%s)", prev_name)
    log.info("=" * 50)

    has_changes = False

    for title, current_items in exports.items():
        safe_title = sanitize_filename(title)
        prev_file = os.path.join(prev_folder, f"{safe_title}.json")
        if not os.path.isfile(prev_file):
            log.info("  [%s] NEW LIST (not in previous export) – %d item(s)", title, len(current_items))
            has_changes = True
            continue

        try:
            with open(prev_file, "r", encoding="utf-8") as f:
                prev_items = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("  [%s] Could not read previous export: %s", title, exc)
            continue

        current_ids = get_series_ids(current_items)
        prev_ids = get_series_ids(prev_items)

        added_ids = set(current_ids) - set(prev_ids)
        removed_ids = set(prev_ids) - set(current_ids)
        count_diff = len(current_items) - len(prev_items)

        if not added_ids and not removed_ids:
            log.info("  [%s] No changes (%d items)", title, len(current_items))
            continue

        has_changes = True
        sign = "+" if count_diff >= 0 else ""
        log.info("  [%s] %d -> %d (%s%d)", title, len(prev_items), len(current_items), sign, count_diff)

        for sid in added_ids:
            log.info("    + Added: %s", current_ids[sid])
        for sid in removed_ids:
            log.info("    - Removed: %s", prev_ids[sid])

    # Check for lists that existed before but are now gone
    current_safe_titles = {sanitize_filename(t) for t in exports}
    for prev_file in os.listdir(prev_folder):
        if prev_file.endswith(".json"):
            list_name = prev_file[:-5]
            if list_name not in current_safe_titles:
                has_changes = True
                log.info("  [%s] LIST REMOVED (no longer exists)", list_name)

    if not has_changes:
        log.info("  No changes detected across all lists")


def rotate_exports() -> None:
    """Keep only the newest MAX_EXPORTS folders, delete the rest."""
    if not os.path.isdir(EXPORTS_DIR):
        return

    folders = sorted(
        [d for d in os.listdir(EXPORTS_DIR)
         if os.path.isdir(os.path.join(EXPORTS_DIR, d))],
    )

    while len(folders) > MAX_EXPORTS:
        oldest = folders.pop(0)
        path = os.path.join(EXPORTS_DIR, oldest)
        try:
            shutil.rmtree(path)
            log.info("Deleted old export: %s", oldest)
        except OSError as exc:
            log.warning("Could not delete %s: %s", oldest, exc)


def main():
    log.info("=" * 50)
    log.info("MangaUpdates List Exporter")
    log.info("=" * 50)

    with httpx.Client(timeout=30) as client:
        # 1. Login
        token = login(client)
        client.headers["Authorization"] = f"Bearer {token}"

        try:
            # 2. Fetch all lists
            lists = fetch_lists(client)

            if not lists:
                log.warning("No lists found for this account")
                return

            # 3. Export each list
            log.info("Exporting lists...")
            exports = {}
            for lst in lists:
                list_id = lst["list_id"]
                title = lst["title"]
                exports[title] = export_list(client, list_id, title)

            # 4. Save to dated folder
            log.info("Saving exports...")
            folder = save_exports(exports)
            log.info("Exports saved to: %s", folder)

            # 5. Compare with previous export
            compare_exports(folder, exports)

            # 6. Rotate old exports
            rotate_exports()

        finally:
            # 7. Logout
            logout(client)

    log.info("Done!")


if __name__ == "__main__":
    main()
