import json
from datetime import datetime, timezone as dt_tz
from pathlib import Path
from threading import Thread
from games.services.tg_send import send_moves_sequentially
from players.models import Player
from games.services.entry import GameEntryManager
from games.services.tg_send import send_dice  # импорт
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponseNotAllowed
from games.models import Move, Game
from games.services.tg_send import send_quiz
from django.utils import timezone
from django.conf import settings
from django.db import transaction
from games.services.board import get_cell_image_name
from games.services.images import image_url_from_board_name

# Where to dump webhook payloads
DUMP_DIR = Path(getattr(settings, "WEBHOOK_DUMP_DIR",
                        Path(settings.BASE_DIR) / "var" / "webhooks"))
DUMP_DIR.mkdir(parents=True, exist_ok=True)

def _send_one_move_and_quiz(bot_token: str, chat_id: int | str, move_dict: dict, *, delay: float = 0.6):
    """
    Отправляет ОДНУ карточку хода, затем ForceReply по этому же ходу,
    сохраняет answer_prompt_msg_id в Move.
    """
    try:
        # 1) карточка
        send_moves_sequentially(bot_token, chat_id, [move_dict], per_message_delay=delay)
    finally:
        # 2) запрос ответа (ForceReply)
        try:
            move_id = move_dict.get("id")
            to_cell = move_dict.get("to_cell")
            rolled = move_dict.get("rolled")
            prompt = f"Ваш ответ по ходу #{move_dict.get('move_number')} (бросок {rolled}, клетка {to_cell}). Напишите, что вы почувствовали/поняли."

            resp = send_quiz(bot_token, chat_id, prompt_text=prompt)
            msg_id = (resp.get("result") or {}).get("message_id")
            if msg_id and move_id:
                mv = Move.objects.filter(id=move_id).first()
                if mv:
                    mv.answer_prompt_msg_id = int(msg_id)
                    mv.save(update_fields=["answer_prompt_msg_id"])
        except Exception:
            pass

def _send_moves_then_quiz(bot_token: str, chat_id: int | str, moves: list[dict], *, per_message_delay: float = 0.6):
    """
    1) Отправляем все карточки ходов.
    2) Для последнего хода просим ответ (ForceReply).
    3) Сохраняем message_id запроса в Move.answer_prompt_msg_id.
    """
    try:
        send_moves_sequentially(bot_token, chat_id, moves, per_message_delay=per_message_delay)
    finally:
        try:
            if not moves:
                return
            last = moves[-1]
            move_id = last.get("id")
            # текст-подсказка можно сформировать из клетки/события
            to_cell = last.get("to_cell")
            rolled = last.get("rolled")
            prompt = f"Ваш ответ по ходу #{last.get('move_number')} (бросок {rolled}, клетка {to_cell}). Напишите, что вы почувствовали/поняли."

            resp = send_quiz(bot_token, chat_id, prompt_text=prompt)
            msg_id = (resp.get("result") or {}).get("message_id")
            if msg_id and move_id:
                from games.models import Move
                try:
                    mv = Move.objects.get(id=move_id)
                    mv.answer_prompt_msg_id = int(msg_id)
                    mv.save(update_fields=["answer_prompt_msg_id"])
                except Exception:
                    pass
        except Exception:
            pass

def _extract_telegram_meta(payload: dict):
    """
    Возвращает словарь:
      {
        "update_id": int|None,
        "from_id": int|None,
        "username": str|None,
        "message_date": datetime|None (UTC),
        "dice_value": int|None,
        "message_id": int|None,
      }
    Работает как с вложенностью {data: {message: ...}}, так и с плоским {message: ...}.
    """
    d = payload.get("data") if isinstance(payload, dict) else None
    root = payload
    if isinstance(d, dict) and "message" in d:
        root = d

    message = (root or {}).get("message") or {}
    frm = message.get("from") or {}

    ts = message.get("date")
    msg_dt = None
    if isinstance(ts, (int, float)):
        msg_dt = datetime.fromtimestamp(ts, tz=dt_tz.utc)

    dice_value = None
    dice = message.get("dice")
    if isinstance(dice, dict):
        dice_value = dice.get("value")

    message = (root or {}).get("message") or {}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")  # NEW

    frm = message.get("from") or {}
    # ... остальное без изменений ...

    return {
        "update_id": payload.get("update_id"),
        "from_id": frm.get("id"),
        "username": frm.get("username"),
        "message_date": msg_dt,
        "dice_value": dice_value,
        "message_id": message.get("message_id"),
        "chat_id": chat_id,  # NEW
    }


@csrf_exempt
def telegram_dice_webhook(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    raw_body = request.body or b""
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        payload = {"_parse_error": True}

    meta = _extract_telegram_meta(payload)
    tg_from_id = meta["from_id"]
    tg_username = meta["username"]
    tg_dt = meta["message_date"]
    dice_value = meta["dice_value"]
    update_id = meta["update_id"]

    # --- Находим/создаём игрока ---
    player = _upsert_player_from_telegram(tg_from_id, tg_username)

    # --- Пытаемся возобновить активную игру ДО проверки dice_value ---
    game = Game.resume_last(player=player)

    # Если активной игры нет — создаём новую и кидаем ПЕРВЫЙ кубик от бота
    if not game:
        game = Game.start_new(player=player, game_type="telegram_dice", game_name="Лила (TG)")
        bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        if bot_token:
            Thread(
                target=send_dice,   # импорт из games.services.send
                args=(bot_token, tg_from_id),
                kwargs={"emoji": "🎲"},
                daemon=True,
            ).start()
        return JsonResponse({
            "ok": True,
            "status": "new_game_started",
            "message": "Создана новая игра. Бросаем первый кубик.",
            "game_id": str(game.id),
            "dice_sent": bool(bot_token),
        })

    # --- ЕСЛИ ЭТО НЕ КУБИК: сначала проверяем, не ждём ли мы ответа по какому-то ходу ---
    if dice_value is None:
        bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        pending = Move.objects.filter(
            game=game, on_hold=False, player_answer__isnull=True
        ).order_by("move_number").first()  # ВАЖНО: самый ранний незакрытый ход

        if pending:
            # Переотправим форму (ForceReply), если нет активного запроса
            if bot_token and not pending.answer_prompt_msg_id:
                prompt = (f"Требуется ответ по ходу #{pending.move_number} "
                          f"(клетка {pending.to_cell}). Напишите, что вы почувствовали/поняли.")
                resp = send_quiz(bot_token, tg_from_id, prompt_text=prompt)
                msg_id = (resp.get("result") or {}).get("message_id")
                if msg_id:
                    pending.answer_prompt_msg_id = int(msg_id)
                    pending.save(update_fields=["answer_prompt_msg_id"])

            return JsonResponse({
                "ok": True,
                "status": "awaiting_answer",
                "message": "Пожалуйста, ответьте на предыдущий ход перед следующим броском.",
                "pending_move_id": pending.id,
            })

        # Ничего не ждём — просто подтверждаем приём не-кубика
        return JsonResponse({"ok": True, "captured": True, "dice_value": None})

    # --- ПРИШЁЛ КУБИК: перед выполнением хода убедимся, что нет незакрытых ответов ---
    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    pending = Move.objects.filter(
        game=game, on_hold=False, player_answer__isnull=True
    ).order_by("move_number").first()  # ВАЖНО: самый ранний

    if pending:
        if bot_token and not pending.answer_prompt_msg_id:
            prompt = (f"Требуется ответ по ходу #{pending.move_number} "
                      f"(клетка {pending.to_cell}). Напишите, что вы почувствовали/поняли.")
            resp = send_quiz(bot_token, tg_from_id, prompt_text=prompt)
            msg_id = (resp.get("result") or {}).get("message_id")
            if msg_id:
                pending.answer_prompt_msg_id = int(msg_id)
                pending.save(update_fields=["answer_prompt_msg_id"])

        return JsonResponse({
            "ok": True,
            "status": "awaiting_answer",
            "message": "Пожалуйста, ответьте на предыдущий ход перед следующим броском.",
            "pending_move_id": pending.id,
        })

    # --- Есть активная игра и пришёл кубик — играем ход ---
    manager = GameEntryManager()
    res = manager.apply_roll(game, rolled=int(dice_value), player_id=player.id)

    if res.status == "continue":
        # НЕ шлём sendDice автоматически внутри активной игры
        return JsonResponse({
            "ok": True,
            "status": "continue",
            "message": res.message,
            "six_count": res.six_count,
        })

    if res.status == "completed":
        # ВНИМАНИЕ: если хотите слать карточки серии ПО ОДНОЙ + ForceReply после каждой —
        # замените на отправку только первой карточки своим _send_one_move_and_quiz(...)
        if bot_token and res.moves:
            Thread(
                target=_send_moves_then_quiz,   # если уже используете поминутно — оставьте так;
                args=(bot_token, tg_from_id, res.moves),
                kwargs={"per_message_delay": 0.6},
                daemon=True,
            ).start()
        return JsonResponse({
            "ok": True,
            "status": "completed",
            "message": "Серия завершена, отправляем ходы пользователю.",
            "six_count": res.six_count,
            "moves_count": len(res.moves),
        })

    if res.status == "single":
        if bot_token and res.moves:
            Thread(
                target=_send_moves_then_quiz,
                args=(bot_token, tg_from_id, res.moves),
                kwargs={"per_message_delay": 0.6},
                daemon=True,
            ).start()
        return JsonResponse({
            "ok": True,
            "status": "single",
            "message": res.message,
            "moves_count": len(res.moves),
        })

    if res.status == "ignored":
        return JsonResponse({
            "ok": True,
            "status": "ignored",
            "message": res.message,
            "six_count": res.six_count,
        })

    # finished и прочие — без доп. действий
    return JsonResponse({
        "ok": True,
        "status": res.status,
        "message": res.message,
        "six_count": res.six_count,
    })


def _player_defaults_from_meta(tg_id: int | None, tg_username: str | None) -> dict:
    """Собираем безопасные defaults для Player.get_or_create."""
    email_local = str(tg_id or tg_username or "unknown")
    defaults = {
        "email": f"tg_{email_local}@example.local",
        "telegram_username": (tg_username or "").strip(),
    }
    # Если в модели есть choice-поля — подставим безопасные значения:
    try:
        if hasattr(Player, "MainStatus"):
            defaults["main_status"] = Player.MainStatus.ACTIVE
        if hasattr(Player, "PlayerType"):
            defaults["player_type"] = Player.PlayerType.FREE
        if hasattr(Player, "PaymentsStatus"):
            defaults["payment_status"] = Player.PaymentsStatus.NONE
    except Exception:
        pass
    return defaults


def _upsert_player_from_telegram(tg_id: int | None, tg_username: str | None) -> Player:
    """Находит/создаёт Player по telegram_id или telegram_username, аккуратно обновляет username."""
    defaults = _player_defaults_from_meta(tg_id, tg_username)
    with transaction.atomic():
        # 1) пробуем по telegram_id
        if tg_id:
            player, created = Player.objects.get_or_create(
                telegram_id=tg_id,
                defaults=defaults,
            )
            # обновим username, если поменялся
            new_un = (tg_username or "").strip()
            if not created and new_un and player.telegram_username != new_un:
                player.telegram_username = new_un
                # если есть updated_at — он сам проставится auto_now=True; иначе просто сохраним это поле
                player.save(update_fields=["telegram_username"])
            return player

        # 2) иначе — по username (если он есть)
        if tg_username:
            player, _ = Player.objects.get_or_create(
                telegram_username__iexact=tg_username.strip(),
                defaults=defaults,
            )
            return player

        # 3) крайний случай — ни id, ни username (технич. запись)
        return Player.objects.create(**defaults)


def _send_moves_then_dice(bot_token: str, chat_id: int | str,
                          moves: list[dict], *, per_message_delay: float = 0.6,
                          emoji: str = "🎲"):
    try:
        send_moves_sequentially(bot_token, chat_id, moves, per_message_delay=per_message_delay)
    finally:
        try:
            send_dice(bot_token, chat_id, emoji=emoji)
        except Exception:
            pass

def _extract_text_reply(payload: dict):
    """
    Возвращает {chat_id, from_id, text, reply_to_message_id} или None.
    """
    msg = (payload.get("message") or
           (payload.get("data") or {}).get("message") or
           {})
    text = msg.get("text")
    if not isinstance(text, str):
        return None
    reply = msg.get("reply_to_message") or {}
    return {
        "chat_id": (msg.get("chat") or {}).get("id"),
        "from_id": (msg.get("from") or {}).get("id"),
        "text": text.strip(),
        "reply_to_message_id": reply.get("message_id"),
    }

@csrf_exempt
def telegram_answer_webhook(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    try:
        payload = json.loads((request.body or b"").decode("utf-8"))
    except Exception:
        return JsonResponse({"ok": False, "error": "bad_json"}, status=400)

    data = _extract_text_reply(payload)
    if not data or not data.get("reply_to_message_id"):
        # это не ответ на ForceReply — можете игнорить или вернуть ok
        return JsonResponse({"ok": True, "ignored": True})

    chat_id = data["chat_id"]
    from_id = data["from_id"]
    text = data["text"]
    reply_msg_id = int(data["reply_to_message_id"])

    # Находим ход по ответу на нашу подсказку
    mv = Move.objects.filter(answer_prompt_msg_id=reply_msg_id).select_related("game").first()
    if not mv:
        return JsonResponse({"ok": True, "ignored": True, "reason": "no_move_for_reply"})

    # Сохраняем ответ
    mv.player_answer = text
    mv.player_answer_at = timezone.now()
    mv.answer_prompt_msg_id = None  # больше не ждём
    mv.save(update_fields=["player_answer", "player_answer_at", "answer_prompt_msg_id"])

    # ищем следующий ход в этой же игре
    next_mv = Move.objects.filter(
        game=mv.game, on_hold=False, move_number__gt=mv.move_number
    ).order_by("move_number").first()

    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)

    if next_mv and bot_token:
        # защитимся: нет ли незакрытых ответов перед next_mv (на всякий случай)
        has_earlier_pending = Move.objects.filter(
            game=mv.game, on_hold=False, move_number__lt=next_mv.move_number,
            player_answer__isnull=True
        ).exists()
        if not has_earlier_pending:
            # соберём move_dict как в EntryManager._serialize_move
            try:
                mgr = GameEntryManager()
                move_dict = mgr._serialize_move(next_mv, player_id=getattr(mv.game, "player_id", None))
            except Exception:
                # минимально необходимое: id, номера, клетки, картинка
                img_name = get_cell_image_name(int(next_mv.to_cell or 0))
                img_url = image_url_from_board_name(img_name, player_id=getattr(mv.game, "player_id", None),
                                                    game_id=next_mv.game_id)
                move_dict = {
                    "id": next_mv.id,
                    "move_number": next_mv.move_number,
                    "rolled": next_mv.rolled,
                    "from_cell": next_mv.from_cell,
                    "to_cell": next_mv.to_cell,
                    "note": next_mv.note,
                    "event_type": str(getattr(next_mv, "event_type", "")),
                    "applied_rules": (next_mv.state_snapshot or {}).get("applied_rules", []),
                    "on_hold": getattr(next_mv, "on_hold", False),
                    "image_url": img_url,
                }

            # отправляем следующую карточку + ForceReply
            Thread(
                target=_send_one_move_and_quiz,
                args=(bot_token, chat_id, move_dict),
                kwargs={"delay": 0.6},
                daemon=True,
            ).start()

            return JsonResponse({"ok": True, "saved": True, "move_id": mv.id, "next_move_id": next_mv.id})

    # Ответ игроку — можно бросать кубик
    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    if bot_token:
        try:
            import requests
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": "Спасибо! Теперь можете бросать кубик ещё раз 🎲",
                },
                timeout=8,
            )
        except Exception:
            pass

    return JsonResponse({"ok": True, "saved": True, "move_id": mv.id})