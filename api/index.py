import sys
from pathlib import Path

# Vercel runs this module from the `api` directory, so expose the repository root.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.main import app

handler = app
