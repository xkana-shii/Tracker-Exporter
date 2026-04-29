import os
import re
import shutil
import time
from datetime import datetime
import gzip

import requests
from selenium import webdriver
from selenium.common.exceptions import NoAlertPresentException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

from myanimelist.config.mal_config import (
    BASE_URL,
    EXPORTS_DIR,
    MAX_EXPORTS,
    MAX_RETRIES,
    PASSWORD,
    RETRY_DELAY,
    USERNAME,
    setup_logging,
)

log = setup_logging()


# ── Driver ──────────────────────────────────────────────────────────────[...]


def _build_driver(download_dir: str) -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--window-size=1200x800")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--headless=new")
    opts.add_experimental_option(
        "prefs",
        {
            "download.default_directory": os.path.abspath(download_dir),
            "download.prompt_for_download": False,
            "profile.default_content_settings.popups": 0,
            "directory_upgrade": True,
            "safebrowsing.enabled": True,
            "safebrowsing.disable_download_protection": True,
        },
    )

    return webdriver.Chrome(options=opts)


# ── Auth ──────────────────────────────────────────────────────────────[...]


def _dismiss_privacy_popup(driver: webdriver.Chrome) -> None:
    selectors = [
        (By.XPATH, "//button[normalize-space()='AGREE']"),
        (By.CSS_SELECTOR, "button[mode='primary']"),
        (By.CSS_SELECTOR, "button[aria-label='Agree']"),
        (By.CSS_SELECTOR, "button#onetrust-accept-btn-handler"),
    ]
    for how, what in selectors:
        for btn in driver.find_elements(how, what):
            try:
                btn.click()
                log.info("Privacy overlay dismissed")
                WebDriverWait(driver, 2).until(EC.staleness_of(btn))
                return
            except Exception:
                pass


def login(driver: webdriver.Chrome) -> None:
    if not USERNAME or not PASSWORD:
        log.error("MAL_USERNAME and MAL_PASSWORD not set in .env")
        raise SystemExit(1)

    log.info("Logging in as '%s'…", USERNAME)
    driver.get(f"{BASE_URL}/login.php")
    _dismiss_privacy_popup(driver)

    wait = WebDriverWait(driver, 30)
    try:
        user_input = wait.until(EC.element_to_be_clickable((By.ID, "loginUserName")))
        pass_input = wait.until(EC.element_to_be_clickable((By.ID, "login-password")))
        user_input.clear()
        user_input.send_keys(USERNAME)
        pass_input.clear()
        pass_input.send_keys(PASSWORD)
        pass_input.send_keys(Keys.RETURN)
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "a.header-right")))
        log.info("Login successful")
    except Exception as e:
        log.error("Login failed: %s", e)
        raise SystemExit(1)


# ── Session ─────────────────────────────────────────────────────────────[...]


def _build_session(driver: webdriver.Chrome) -> tuple[str, str, requests.Session]:
    """Extract CSRF token, user ID and a requests.Session from the driver cookies."""
    driver.get(f"{BASE_URL}/panel.php?go=export")
    WebDriverWait(driver, 20).until(
        EC.presence_of_element_located(
            (By.XPATH, "//form[@action='/panel.php?go=export2']")
        )
    )
    page = driver.page_source

    csrf_match = re.search(r'<meta name="csrf_token" content="([0-9a-f]+)"', page)
    if not csrf_match:
        raise RuntimeError("Could not extract CSRF token from export page")
    csrf_token = csrf_match.group(1)

    uid_match = re.search(r"'userId'\s*:\s*'(\d+)'", page)
    user_id = uid_match.group(1) if uid_match else ""

    session = _refresh_session(driver)
    return csrf_token, user_id, session


def _refresh_session(driver: webdriver.Chrome) -> requests.Session:
    """Rebuild a requests.Session from the current driver cookies."""
    session = requests.Session()
    for c in driver.get_cookies():
        session.cookies.set(
            c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/")
        )
    return session


# ── Export ─────────────────────────────────────────────────────────────�[...]

EXPORTS = [
    ("anime", "1"),
    ("manga", "2"),
]


def _trigger_export(
    driver: webdriver.Chrome, list_type: str, select_value: str
) -> None:
    """Click the export button for a given list type via Selenium."""
    driver.get(f"{BASE_URL}/panel.php?go=export")
    form = WebDriverWait(driver, 20).until(
        EC.presence_of_element_located(
            (By.XPATH, "//form[@action='/panel.php?go=export2']")
        )
    )
    Select(form.find_element(By.NAME, "type")).select_by_value(select_value)
    form.find_element(By.CSS_SELECTOR, 'input[type="submit"][name="subexport"]').click()

    try:
        WebDriverWait(driver, 5).until(EC.alert_is_present())
        alert = driver.switch_to.alert
        log.info("Accepting alert: %s", alert.text)
        alert.accept()
    except NoAlertPresentException:
        pass

    log.info("Triggered %s export", list_type)


def _maybe_extract_gz(gz_path: str) -> str | None:
    """
    If gz_path ends with .gz, attempt to decompress it to the same path without .gz.
    Returns the path to the extracted file on success, or None on failure/no-op.

    After successful extraction, the original .gz archive is removed.
    """
    if not gz_path or not gz_path.lower().endswith(".gz"):
        return None
    extracted_path = gz_path[:-3]
    try:
        with gzip.open(gz_path, "rb") as gz_f, open(extracted_path, "wb") as out_f:
            shutil.copyfileobj(gz_f, out_f)
        log.info(
            "Extracted %s -> %s (%d bytes)",
            gz_path,
            extracted_path,
            os.path.getsize(extracted_path),
        )
        # Remove the original .gz archive now that extraction succeeded
        try:
            os.remove(gz_path)
            log.info("Removed archive %s", gz_path)
        except Exception as exc_rm:
            log.warning("Could not remove archive %s: %s", gz_path, exc_rm)
        return extracted_path
    except Exception as exc:
        log.warning("Failed to extract %s: %s", gz_path, exc)
        return None


def _try_direct_download(
    session: requests.Session, list_type: str, csrf_token: str, out_path: str
) -> bool:
    """
    POST directly to export2 and save if the response is a real export file.
    Returns True on success.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(
                f"{BASE_URL}/panel.php?go=export2",
                data={
                    "type": "1" if list_type == "anime" else "2",
                    "subexport": "Export My List",
                    "csrf_token": csrf_token,
                },
                allow_redirects=True,
            )
            content_type = resp.headers.get("Content-Type", "")
            if ("gzip" in content_type or "octet-stream" in content_type) and len(
                resp.content
            ) > 1024:
                with open(out_path, "wb") as f:
                    f.write(resp.content)
                log.info("Saved %s (%d bytes)", out_path, len(resp.content))
                # Attempt to extract .gz to .xml
                _maybe_extract_gz(out_path)
                return True
            return False
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                log.warning(
                    "Direct download attempt %d/%d failed: %s – retrying in %ds…",
                    attempt,
                    MAX_RETRIES,
                    exc,
                    RETRY_DELAY,
                )
                time.sleep(RETRY_DELAY)
            else:
                log.error(
                    "Direct download failed after %d attempts: %s", MAX_RETRIES, exc
                )
                return False
    return False


def _download_from_panel(
    session: requests.Session, list_type: str, out_path: str
) -> None:
    """Scrape the export panel for the download link and fetch the file."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(f"{BASE_URL}/panel.php?go=export")
            resp.raise_for_status()
            pattern = (
                r'href="(https://myanimelist\.net/[^"]*{}list[^"]*\.xml\.gz)"'.format(
                    list_type
                )
            )
            m = re.search(pattern, resp.text)
            if not m:
                raise RuntimeError(
                    f"{list_type.capitalize()} export link not found in panel"
                )
            link = m.group(1)
            log.info("Found export link: %s", link)

            dl = session.get(link)
            dl.raise_for_status()
            with open(out_path, "wb") as f:
                f.write(dl.content)
            log.info("Saved %s (%d bytes)", out_path, len(dl.content))
            # Attempt to extract .gz to .xml
            _maybe_extract_gz(out_path)
            return
        except Exception as exc:
            if attempt < MAX_RETRIES:
                log.warning(
                    "Panel download attempt %d/%d failed: %s – retrying in %ds…",
                    attempt,
                    MAX_RETRIES,
                    exc,
                    RETRY_DELAY,
                )
                time.sleep(RETRY_DELAY)
            else:
                log.error(
                    "Panel download failed after %d attempts: %s", MAX_RETRIES, exc
                )
                raise


# ── Rotation ────────────────────────────────────────────────────────────��[...]


def _parse_folder_date(name: str) -> datetime:
    try:
        return datetime.strptime(name, "%d.%m.%Y_%H-%M-%S")
    except ValueError:
        return datetime.min


def rotate_exports() -> None:
    if not os.path.isdir(EXPORTS_DIR):
        return
    folders = sorted(
        [
            d
            for d in os.listdir(EXPORTS_DIR)
            if os.path.isdir(os.path.join(EXPORTS_DIR, d))
        ],
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


# ── Main ──────────────────────────────────────────────────────────────[...]


def main() -> None:
    log.info("=" * 50)
    log.info("MyAnimeList Exporter")
    log.info("=" * 50)

    start = time.time()
    now = datetime.now()
    folder = os.path.join(EXPORTS_DIR, now.strftime("%d.%m.%Y_%H-%M-%S"))
    os.makedirs(folder, exist_ok=True)

    driver = _build_driver(folder)
    try:
        login(driver)
        csrf_token, user_id, session = _build_session(driver)

        for list_type, select_value in EXPORTS:
            log.info("Exporting %s list…", list_type)
            # Record existing files in the download folder so we can detect new browser downloads
            before_files = set(os.listdir(folder))

            _trigger_export(driver, list_type, select_value)

            # Refresh session cookies after each trigger
            session = _refresh_session(driver)

            # Human-readable timestamp for filename
            human_ts = datetime.now().strftime("%d.%m.%Y_%H-%M-%S")
            out_path = os.path.join(
                folder, f"mal_library-{list_type}-{human_ts}.xml.gz"
            )

            # Wait a short while for the browser to auto-download (if it does). If we detect
            # a new .xml.gz file in the download folder, treat that as the export and rename/move it.
            waited = 0
            found_browser_file = None
            while waited < 15:  # seconds
                current = set(os.listdir(folder))
                new = current - before_files
                gz_files = [f for f in new if f.lower().endswith(".xml.gz")]
                if gz_files:
                    # pick the newest (by mtime) among the new gz files
                    gz_paths = [os.path.join(folder, f) for f in gz_files]
                    newest = max(gz_paths, key=lambda p: os.path.getmtime(p))
                    found_browser_file = newest
                    break
                time.sleep(1)
                waited += 1

            if found_browser_file:
                # Move/rename the browser-downloaded file to our canonical filename if needed
                try:
                    if os.path.abspath(found_browser_file) != os.path.abspath(out_path):
                        shutil.move(found_browser_file, out_path)
                    log.info("Using browser download for %s -> %s", list_type, out_path)
                    # Attempt to extract the moved .gz into .xml
                    _maybe_extract_gz(out_path)
                    continue  # skip manual download attempts
                except Exception as e:
                    log.warning(
                        "Could not move browser download %s -> %s: %s",
                        found_browser_file,
                        out_path,
                        e,
                    )
                    # fall through to manual download attempts

            # Try direct POST download method
            if _try_direct_download(session, list_type, csrf_token, out_path):
                continue

            log.info("Direct download not ready – waiting 5s before scraping panel…")
            time.sleep(5)
            try:
                _download_from_panel(session, list_type, out_path)
            except Exception as e:
                log.error("%s export failed: %s", list_type.capitalize(), e)

    finally:
        driver.quit()

    rotate_exports()

    elapsed = time.time() - start
    log.info("=" * 50)
    log.info("Exports saved to: %s (%.1fs)", folder, elapsed)
    log.info("=" * 50)


if __name__ == "__main__":
    main()
