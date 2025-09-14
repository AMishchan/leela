import json, time
from pathlib import Path

# путь к файлу относительно этого файла
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "board.json"

_cache = {"data": None, "mtime": None}

def get_board(force_reload: bool = False):
    """
    Возвращает JSON как Python-объект.
    Кэширует в памяти, перезагружает при изменении файла.
    """
    try:
        mtime = DATA_PATH.stat().st_mtime
    except FileNotFoundError:
        raise RuntimeError(f"Board JSON not found: {DATA_PATH}")

    if force_reload or _cache["data"] is None or _cache["mtime"] != mtime:
        with DATA_PATH.open("r", encoding="utf-8") as f:
            _cache["data"] = json.load(f)
            _cache["mtime"] = mtime
    return _cache["data"]
