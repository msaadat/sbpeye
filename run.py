import uvicorn
import sys
from pathlib import Path

# Add src to python path so it can find sbpeye
sys.path.insert(0, str(Path(__file__).parent / "src"))

if __name__ == "__main__":
    uvicorn.run("sbpeye.main:app", host="0.0.0.0", port=8000, reload=True)
