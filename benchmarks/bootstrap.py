from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "services" / "sidecar" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))
