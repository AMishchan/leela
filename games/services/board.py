import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

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

def _to_int(v: Any) -> Optional[int]:
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None

def get_jump_target(n: int) -> Optional[int]:
    """
    Возвращает конечную клетку перехода для клетки n, если на ней есть змея/лестница/стрелка.
    Если перехода нет — возвращает None.
    НИЧЕГО в формате board.json не меняет, использует get_cell()/get_board().
    """
    cell = get_cell(n)

    # 1) Если структура клеток объектная — попробуем вытащить переход из самой клетки:
    if isinstance(cell, dict):
        # самые частые прямые ключи
        for key in ("to", "goto", "go_to", "target", "end", "next", "to_cell", "dest", "destination"):
            v = _to_int(cell.get(key))
            if v is not None and v != n:
                return v

        # вложенные варианты: arrow/ladder/snake/portal/warp и т.п.
        for nk in ("arrow", "ladder", "snake", "portal", "warp"):
            sub = cell.get(nk)
            if isinstance(sub, dict):
                for k in ("to", "target", "end", "goto", "jump_to"):
                    v = _to_int(sub.get(k))
                    if v is not None and v != n:
                        return v

        # fallback: вдруг переход лежит во вложенном объекте под произвольным ключом
        for v in cell.values():
            if isinstance(v, dict):
                cand = _to_int(v.get("to") or v.get("target") or v.get("end"))
                if cand is not None and cand != n:
                    return cand

    # 2) Если борда — мапа {"16": 6} (или завернута {"board": {...}}) — прочитаем напрямую
    board = get_board()
    mapping = board.get("board") if isinstance(board, dict) and "board" in board else board
    if isinstance(mapping, dict):
        v = _to_int(mapping.get(str(n)))
        if v is not None and v != n:
            return v

    return None

def resolve_chain(start: int, max_steps: int = 100) -> Tuple[int, List[Tuple[int, int]]]:
    """
    Пройти по цепочке переходов, начиная с клетки start.
    Возвращает (final_cell, [(from, to), ...]).
    """
    cur = start
    via: List[Tuple[int, int]] = []
    for _ in range(max_steps):
        nxt = get_jump_target(cur)
        if nxt is None or nxt == cur:
            break
        via.append((cur, nxt))
        cur = nxt
    return cur, via

from typing import Optional

def get_cell_image_name(n: int) -> Optional[str]:
    cell = get_cell(n)
    if not isinstance(cell, dict):
        return None
    for k in ("image", "image_url", "img", "image_file", "filename", "file", "path", "url"):
        v = cell.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for nk in ("media", "asset", "picture", "card"):
        sub = cell.get(nk)
        if isinstance(sub, dict):
            for k in ("image", "image_url", "img", "image_file", "filename", "file", "path", "url"):
                v = sub.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return None

