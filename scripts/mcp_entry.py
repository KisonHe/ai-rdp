from pathlib import Path
import sys

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from ai_rdp.mcp_server import run

run(root / ".state")
