import os
import logging
from logging.handlers import RotatingFileHandler

# Import central repo-root configuration helpers
from config.config import (
    tracker_exports_dir,
    tracker_logs_dir,
    get_tracker_credentials,
)

# Credentials
USERNAME, PASSWORD = get_tracker_credentials("myanimelist")

# Paths
EXPORTS_DIR = tracker_exports_dir("myanimelist")
LOGS_DIR = tracker_logs_dir("myanimelist")

# Ensure directories exist
os.makedirs(EXPORTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# API
BASE_URL = "https://myanimelist.net"

# Export settings
MAX_EXPORTS = 10    # Number of dated export folders to keep

# Retry settings
MAX_RETRIES = 3     # Number of retry attempts for requests
RETRY_DELAY = 5     # Seconds between retries

# Logging setup
LOG_FILE = os.path.join(LOGS_DIR, "myanimelist_export.log")


def setup_logging():
    logger = logging.getLogger("mal_export")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    for name in ("httpx", "httpcore", "urllib3", "selenium"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return logger
