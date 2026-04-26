import os
import shutil
import time
from datetime import datetime, timezone

import httpx

from mangabaka.config.mb_config import (
    BASE_URL,
    EMAIL,
    EXPORTS_DIR,
    MAX_EXPORTS,
    MAX_RETRIES,
    PASSWORD,
    RETRY_DELAY,
    setup_logging,
)

log = setup_logging()


# ── HTTP ──────────────────────────────────────────────────────────────[...]


def _api_request(
    client: httpx.Client, method: str, url: str, **kwargs
) -> httpx.Response:
    """Make a request with automatic retry on transient errors."""
    kwargs.setdefault("follow_redirects", True)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = getattr(client, method)(url, **kwargs)

            # Treat a redirect to /auth as an authentication failure
            if resp.history and "/auth" in str(resp.url):
                log.error("Redirected to auth page – session cookie was not accepted")
                raise SystemExit(1)

            if resp.status_code >= 500:
                raise httpx.HTTPStatusError(
                    f"Server error {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )
            return resp
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.HTTPStatusError,
        ) as exc:
            if attempt < MAX_RETRIES:
                log.warning(
                    "Attempt %d/%d failed: %s – retrying in %ds…",
                    attempt,
                    MAX_RETRIES,
                    exc,
                    RETRY_DELAY,
                )
                time.sleep(RETRY_DELAY)
            else:
                log.error("All %d attempts failed: %s", MAX_RETRIES, exc)
                raise


# ── Auth ──────────────────────────────────────────────────────────────[...]


def login(client: httpx.Client) -> None:
    if not EMAIL or not PASSWORD:
        log.error("MB_EMAIL or MB_PASSWORD not set in .env")
        raise SystemExit(1)

    log.info("Logging in as '%s'…", EMAIL)

    # Do NOT follow redirects on login so we can inspect the raw response cookies
    resp = client.post(
        f"{BASE_URL}/auth/sign-in/email",
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "callbackURL": "/my",
        },
        headers={
            "Referer": f"{BASE_URL}/auth",
            "Origin": BASE_URL,
            "User-Agent": "Mozilla/5.0",
        },
        follow_redirects=False,
    )

    if resp.status_code == 401:
        log.error("Login failed – invalid credentials")
        raise SystemExit(1)

    # Debug: show exactly what cookies and headers the server sent back
    log.info("Login response status: %s", resp.status_code)
    log.info("Login response cookies: %s", dict(resp.cookies))
    log.info("Login Set-Cookie headers: %s", resp.headers.get("set-cookie", "(none)"))

    token = resp.json().get("token") if resp.content else None
    log.info("Login response token field: %s", token[:12] + "…" if token else "(none)")

    # Strategy 1: cookies were set directly on the response (most likely)
    if resp.cookies:
        log.info("Using cookies from login response directly")
        for name, value in resp.cookies.items():
            client.cookies.set(name, value, domain="mangabaka.org")

    # Strategy 2: token returned in JSON body, set it manually
    elif token:
        log.info("Setting cookie manually from token in response body")
        client.cookies.set(
            "__Secure-better-auth.session_token", token, domain="mangabaka.org"
        )

    else:
        log.error("Login failed – no cookies or token in response")
        raise SystemExit(1)

    log.info("Cookies now on client: %s", dict(client.cookies))
    log.info("Login successful")


# ── Export ─────────────────────────────────────────────────────────────�[...]

EXPORTS = [
    ("mangabaka", f"{BASE_URL}/my/library/export/mangabaka", "json"),
    ("mal", f"{BASE_URL}/my/library/export/mal", "xml"),
]


def _iso_filename(label: str, ext: str, dt: datetime) -> str:
    """
    Return filenames like:
      - mb_library-26.04.2026_14-32-16.json       (for label == "mangabaka")
      - mb_library_mal-26.04.2026_14-32-16.xml   (for label == "mal")
    """
    ts = dt.strftime("%d.%m.%Y_%H-%M-%S")
    if label == "mangabaka":
        base = "mb_library"
    elif label == "mal":
        base = "mb_library_mal"
    else:
        base = f"mb_{label}"
    return f"{base}-{ts}.{ext}"


def download_export(
    client: httpx.Client, label: str, url: str, ext: str, folder: str, dt: datetime
) -> None:
    log.info("Downloading %s export…", label)
    resp = _api_request(client, "get", url)
    resp.raise_for_status()

    filename = _iso_filename(label, ext, dt)
    path = os.path.join(folder, filename)
    with open(path, "wb") as f:
        f.write(resp.content)
    log.info("Saved %s (%d bytes)", path, len(resp.content))


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
    log.info("MangaBaka Library Exporter")
    log.info("=" * 50)

    start = time.time()
    dt = datetime.now(timezone.utc)
    folder = os.path.join(EXPORTS_DIR, dt.strftime("%d.%m.%Y_%H-%M-%S"))
    os.makedirs(folder, exist_ok=True)

    with httpx.Client(timeout=30) as client:
        login(client)
        for label, url, ext in EXPORTS:
            download_export(client, label, url, ext, folder, dt)

    rotate_exports()

    elapsed = time.time() - start
    log.info("=" * 50)
    log.info("Exports saved to: %s (%.1fs)", folder, elapsed)
    log.info("=" * 50)


if __name__ == "__main__":
    main()
