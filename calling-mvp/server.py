"""Compatibility exports and launcher; the only FastAPI app is app.main:app."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.agent.engine import Engine, atomic_json, extract_decision, instruction, verify_signature

if __name__ == '__main__':
    from serve_calls import main
    main()
