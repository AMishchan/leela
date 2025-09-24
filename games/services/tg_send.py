from __future__ import annotations

import os
import time
import requests
from typing import Dict, Any, List, Optional
from django.conf import settings
from games.services.board import get_cell

SITE_BASE_URL = getattr(settings, "SITE_BASE_URL", "").rstrip("/")

# Где лежат файлы картинок (относительные пути начнутся с "cards/...")
MEDIA_ROOT = getattr(settings, "PROTECTED_MEDIA_ROOT", "")


# ---------- Утилиты ----------

def _abs_path_from_rel(rel_path: Optional[str]) -> Optional[str]:
    """Построить абсолютный путь к файлу из MEDIA_ROOT и относительного пути (напр., 'cards/22-....jpg')."""
    if not rel_path or not MEDIA_ROOT:
        return None
    # нормализуем
    rel_norm = rel_path.lstrip("/").replace("/", os.sep)
    return os.path.join(MEDIA_ROOT, rel_norm)

def _is_good_image_url(url: str, timeout: float = 8.0) -> bool:
    """Быстрая проверка, что по URL действительно лежит картинка, и она не пустая."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        if r.status_code != 200:
            return False
        ct = (r.headers.get("Content-Type") or "").lower()
        cl = int(r.headers.get("Content-Length") or 0)
        return ct.startswith("image/") and cl > 0
    except Exception:
        return False

def _truncate_caption(caption: Optional[str]) -> Optional[str]:
    """Подрезаем подпись под лимит Telegram ~1024 символа."""
    if caption and len(caption) > 1024:
        return caption[:1021] + "..."
    return caption


# ---------- Рендер текста хода ----------

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



# ---------- Основная функция ----------

def send_moves_sequentially(
    bot_token: str,
    chat_id: int,
    moves: List[Dict[str, Any]],
    per_message_delay: float = 0.6,
) -> int:
    """
    Отправляет ходы по очереди.
    Если есть картинка:
      1) пробуем отправить по URL (photo=<URL>) — только если URL реально отдает image/* и не пустой;
      2) если URL не валиден или TG вернул ошибку, отправляем как файл (multipart) из MEDIA_ROOT;
    Если картинки нет — отправляем текст.
    """
    sent = 0
    base = f"https://api.telegram.org/bot{bot_token}"

    for mv in moves:
        caption = _truncate_caption(render_move_text(mv))

        rel_img = mv.get("image_url") or mv.get("image")
        abs_path = _abs_path_from_rel(rel_img) if rel_img else None

        try:
            # --- 2) Фолбэк: отправка как файла (из приватного MEDIA_ROOT) ---
            if abs_path:
                with open(abs_path,
                          "rb") as f:
                    r=requests.post(
                        f"{base}/sendPhoto",
                        data={"chat_id": chat_id, "caption": ''},
                        files={"photo": f},
                        timeout=5
                    )
                if r.status_code == 200:
                    sent += 1
                    time.sleep(per_message_delay)
                    continue
                # если и файл не ушёл — отправим текст

            # --- 3) Нет картинки или всё упало — шлём текст ---
            requests.post(
                f"{base}/sendMessage",
                json={"chat_id": chat_id, "text": caption or ""},
                timeout=8,
            )
            sent += 1
            time.sleep(per_message_delay)

        except Exception:
            # Любая ошибка — хотя бы текст
            try:
                requests.post(
                    f"{base}/sendMessage",
                    json={"chat_id": chat_id, "text": caption or ""},
                    timeout=8,
                )
                sent += 1
                time.sleep(per_message_delay)
            except Exception:
                # совсем упало — пропускаем
                continue

    return sent
