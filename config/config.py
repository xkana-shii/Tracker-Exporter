import os
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv

# Load environment variables from project root .env
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Credentials
USERNAME = os.getenv("MU_USERNAME", "")
PASSWORD = os.getenv("MU_PASSWORD", "")

# Paths
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# Ensure directories exist
os.makedirs(EXPORTS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# API
API_BASE_URL = "https://api.mangaupdates.com/v1"

# Export settings
MAX_EXPORTS = 3          # Number of dated export folders to keep
ITEMS_PER_PAGE = 100     # Items per API page request

# Logging setup
LOG_FILE = os.path.join(LOGS_DIR, "mangaupdates_export.log")


def setup_logging():
    logger = logging.getLogger("mu_export")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    # Rotating file handler (5 MB, 3 backups)
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    # Suppress noisy libraries
    for name in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)

    return logger
