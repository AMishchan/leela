import json
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "board.json"
_cache = {"data": None, "mtime": None}

def get_board():
    st = DATA_PATH.stat().st_mtime
    if _cache["data"] is None or _cache["mtime"] != st:
        with DATA_PATH.open("r", encoding="utf-8") as f:
            _cache["data"] = json.load(f)
            _cache["mtime"] = st
    return _cache["data"]

def get_cell(n: int):
    board = get_board()
    # board — либо массив объектов, либо { "board": [...] }
    cells = board["board"] if isinstance(board, dict) and "board" in board else board
    return next((c for c in cells if int(c.get("n") or c.get("cell")) == n), None)

