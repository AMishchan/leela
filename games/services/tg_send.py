from __future__ import annotations

from typing import Dict, Any, List, Optional
from django.conf import settings
from games.services.board import get_cell
import os
from typing import Any, Dict, Optional
import time
import requests

SITE_BASE_URL = getattr(settings, "SITE_BASE_URL", "").rstrip("/")

# Где лежат файлы картинок (относительные пути начнутся с "cards/...")
MEDIA_ROOT = getattr(settings, "PROTECTED_MEDIA_ROOT", "")
TG_API = "https://api.telegram.org/bot{token}/{method}"
DEFAULT_TIMEOUT = 10
# Официально поддерживаемые эмодзи для sendDice:
ALLOWED_DICE_EMOJIS = {"🎲", "🎯", "🏀", "⚽", "🎳", "🎰"}


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
    # Клетка остановки ПЕРЕД правилом (то, что «пропадает» в Telegram):
    pre_rule_cell = None
    if rules:
        try:
            pre_rule_cell = int(rules[0].get("from"))
        except Exception:
            pre_rule_cell = None

    # Красиво опишем цепочку правил
    if rules:
        parts = []
        for r in rules:
            a = r.get("from")
            b = r.get("to")
            t = (r.get("type") or "").lower()
            label = "лестница" if t == "ladder" else ("змея" if t == "snake" else "правило")
            parts.append(f"{a} → {b} ({label})")
        rules_block = "Переходы: " + " ; ".join(parts)
    else:
        rules_block = ""

    # Итоговый текст
    lines = [
        f"Бросок: {rolled}",
        f"{from_cell} → {to_cell}",
        title,
    ]
    if meaning:
        lines.append(meaning)

    # 👇 Вот та самая «промежуточная» клетка (начало стрелы/лестницы)
    if pre_rule_cell is not None:
        lines.append(f"Остановились на {pre_rule_cell} — сработало правило.")
    if rules_block:
        lines.append(rules_block)

    return "\n".join(lines).strip()


# ---------- Основная функция ----------

def send_moves_sequentially(
        bot_token: str,
        chat_id: int,
        moves: List[Dict[str, Any]],
        per_message_delay: float = 3.6,
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

        time.sleep(3.0)
        # ... внутри цикла по ходам ...
        try:
            # --- 2) Фолбэк: отправка как файла (из приватного MEDIA_ROOT) ---
            if abs_path:
                with open(abs_path, "rb") as f:
                    r = requests.post(
                        f"{base}/sendPhoto",
                        data={"chat_id": chat_id, "caption": caption or ""},
                        files={"photo": f},
                        timeout=5
                    )
                if r.status_code == 200:
                    sent += 1
                    continue  # пауза будет в finally

            # --- 3) Нет картинки или всё упало — шлём текст ---
            r = requests.post(
                f"{base}/sendMessage",
                json={"chat_id": chat_id, "text": caption or ""},
                timeout=8,
            )
            if r.status_code == 200:
                sent += 1

        except Exception:
            # Любая ошибка — хотя бы текст
            try:
                r = requests.post(
                    f"{base}/sendMessage",
                    json={"chat_id": chat_id, "text": caption or ""},
                    timeout=8,
                )
                if r.status_code == 200:
                    sent += 1
            except Exception:
                # совсем упало — пропускаем
                pass

    return sent


def send_dice(
        token: Optional[str],
        chat_id: int | str,
        *,
        emoji: str = "🎲",
        disable_notification: bool = False,
        reply_to_message_id: Optional[int] = None,
        allow_sending_without_reply: bool = True,
        message_thread_id: Optional[int] = None,  # для тем в супергруппах
        timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Отправляет бросок кубика в чат. Возвращает JSON-ответ Telegram (dict).
    Если токен не передан, берёт TELEGRAM_BOT_TOKEN из окружения.
    """
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"ok": False, "error": "bot_token_not_set"}

    if emoji not in ALLOWED_DICE_EMOJIS:
        emoji = "🎲"

    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "emoji": emoji,
        "disable_notification": disable_notification,
        "allow_sending_without_reply": allow_sending_without_reply,
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id

    try:
        r = requests.post(
            TG_API.format(token=token, method="sendDice"),
            json=payload,
            timeout=timeout,
        )
        # Бывает, что Telegram вернёт HTML в ошибках — защитимся
        try:
            data = r.json()
        except Exception:
            return {"ok": False, "status_code": r.status_code, "text": r.text}
        return data
    except requests.RequestException as e:
        return {"ok": False, "error": "request_exception", "detail": str(e)}


def extract_dice_value(resp: Dict[str, Any]) -> Optional[int]:
    """Удобный хелпер: достаёт выпавшее значение 1..6 из ответа sendDice."""
    try:
        return (resp.get("result") or {}).get("dice", {}).get("value")
    except Exception:
        return None

def send_quiz(
    token: Optional[str],
    chat_id: int | str,
    *,
    prompt_text: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Просим игрока ответить: отправляем сообщение с ForceReply.
    Игрок отвечает прямо в чат, их next-message придёт как reply_to_message.
    """
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"ok": False, "error": "bot_token_not_set"}

    payload = {
        "chat_id": chat_id,
        "text": prompt_text,
        "reply_markup": {"force_reply": True, "input_field_placeholder": "Напишите ответ…"},
    }
    try:
        r = requests.post(TG_API.format(token=token, method="sendMessage"), json=payload, timeout=timeout)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "status_code": r.status_code, "text": r.text}
    except requests.RequestException as e:
        return {"ok": False, "error": "request_exception", "detail": str(e)}


def send_text_message(
    token: Optional[str],
    chat_id: int | str,
    text: str,
    *,
    parse_mode: Optional[str] = None,          # "Markdown", "HTML" — если нужно
    disable_notification: bool = False,
    reply_to_message_id: Optional[int] = None,
    allow_sending_without_reply: bool = True,
    message_thread_id: Optional[int] = None,   # для тем/топиков
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    Отправка простого текстового сообщения в чат.
    Удобный обёртка над sendMessage для любых сервисных текстов (оплата и т.п.).
    """
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return {"ok": False, "error": "bot_token_not_set"}

    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_notification": disable_notification,
        "allow_sending_without_reply": allow_sending_without_reply,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id

    try:
        r = requests.post(
            TG_API.format(token=token, method="sendMessage"),
            json=payload,
            timeout=timeout,
        )
        try:
            return r.json()
        except Exception:
            return {"ok": False, "status_code": r.status_code, "text": r.text}
    except requests.RequestException as e:
        return {"ok": False, "error": "request_exception", "detail": str(e)}
