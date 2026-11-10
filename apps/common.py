import json
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "state/nodes.json"

with open(CONFIG_PATH, encoding="utf-8") as f:
    NODES = json.load(f)