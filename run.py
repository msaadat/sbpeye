import os
import sys
from pathlib import Path

import uvicorn

# Add src to python path so it can find sbpeye
sys.path.insert(0, str(Path(__file__).parent / "src"))

if __name__ == "__main__":
    # reload=True without reload_dirs watches the whole working directory,
    # including frontend/node_modules and sbpeye.db (rewritten on most
    # requests) — that thrashes the server with restarts on deployments
    # that don't opt into dev mode. Scope the watch to source only when enabled.
    dev_mode = "--dev" in sys.argv or os.environ.get("SBPEYE_DEV") == "1"
    uvicorn.run(
        "sbpeye.main:app",
        host="0.0.0.0",
        port=8000,
        reload=dev_mode,
        reload_dirs=[str(Path(__file__).parent / "src")] if dev_mode else None,
    )
