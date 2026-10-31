"""Start the local app with Python: python start.py [--port 8765]."""
from pathlib import Path
import os
import runpy
import sys


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    os.chdir(root)
    sys.path.insert(0, str(root))
    runpy.run_path(str(root / "app.py"), run_name="__main__")
