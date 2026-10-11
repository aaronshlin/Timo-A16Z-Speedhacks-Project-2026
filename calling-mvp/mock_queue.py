"""Twelve fictional Timo sessions. Every dispatcher action is simulated."""
import json
from pathlib import Path

MOCK_QUEUE = json.loads(Path(__file__).with_name('demo_sessions.json').read_text())
