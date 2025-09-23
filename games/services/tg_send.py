# games/services/tg_send.py
from __future__ import annotations
import time
import requests
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin
from django.conf import settings
from games.services.board import get_cell
from games.services.images import build_abs_image_url

# Если в settings.BOARD_CELL_IMAGE_URL относительный ("/media/..."),
# нужен SITE_BASE_URL (например, "https://your.domain").
SITE_BASE_URL = getattr(settings, "SITE_BASE_URL", "").rstrip("/")

def _abs_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if not SITE_BASE_URL:
        return None
    return urljoin(SITE_BASE_URL + "/", u.lstrip("/"))

def render_move_text(mv: Dict[str, Any]) -> str:
    """
    Строим текст сообщения по одному ходу.
    Опираться на возможные поля board.json: title/name, meaning/text/desc.
    """
    to_cell = int(mv.get("to_cell") or 0)
    from_cell = int(mv.get("from_cell") or 0)
    rolled = mv.get("rolled")
    cell = get_cell(to_cell) or {}

    title = cell.get("title") or cell.get("name") or f"Клетка {to_cell}"
    meaning = cell.get("meaning") or cell.get("text") or cell.get("desc") or ""
    rules = mv.get("applied_rules") or []
    if rules:
        chain = "\n".join([f"→ {r.get('from')} → {r.get('to')}" for r in rules])
        rules_block = f"\nПереходы:\n{chain}"
    else:
        rules_block = ""

    return (
        f"Бросок: {rolled}\n"
        f"{from_cell} → {to_cell}\n"
        f"{title}\n"
        f"{meaning}{rules_block}"
    ).strip()

def send_moves_sequentially(bot_token: str, chat_id: int, moves: List[Dict[str, Any]], per_message_delay: float = 0.6) -> int:
    base = f"https://api.telegram.org/bot{bot_token}"
    sent = 0
    for mv in moves:
        text = render_move_text(mv)
        # mv["image_url"] — относительный путь; собираем абсолютный
        img = build_abs_image_url(mv.get("image_url"))

        try:
            if img:
                res =requests.post(f"{base}/sendPhoto", json={
                    "chat_id": chat_id,
                    "photo": img,
                    "caption": text,
                }, timeout=8)
            else:
                requests.post(f"{base}/sendMessage", json={
                    "chat_id": chat_id,
                    "text": text,
                }, timeout=8)
            sent += 1
            time.sleep(per_message_delay)
        except Exception:
            continue
    return sent
