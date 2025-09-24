import json
from datetime import datetime, timezone as dt_tz
from pathlib import Path
from threading import Thread
from games.services.tg_send import send_moves_sequentially
from django.http import JsonResponse, HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt
from games.models import Game, Move
from players.models import Player
from games.services.entry import GameEntryManager

from django.conf import settings

# Where to dump webhook payloads
DUMP_DIR = Path(getattr(settings, "WEBHOOK_DUMP_DIR",
                        Path(settings.BASE_DIR) / "var" / "webhooks"))
DUMP_DIR.mkdir(parents=True, exist_ok=True)


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

    return {
        "update_id": payload.get("update_id"),
        "from_id": frm.get("id"),
        "username": frm.get("username"),
        "message_date": msg_dt,
        "dice_value": dice_value,
        "message_id": message.get("message_id"),
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

    # Обрабатываем только сообщения с кубиком
    if dice_value is None:
        return JsonResponse({"ok": True, "captured": True, "dice_value": None})

    # --- Находим/создаём игрока (правильные поля: telegram_id / telegram_username) ---
    player = _upsert_player_from_telegram(tg_from_id, tg_username)

    # --- Берём текущую игру (или создаём новую) ---
    game = Game.resume_last(player=player)
    if not game:
        game = Game.start_new(player=player, game_type="telegram_dice", game_name="Лила (TG)")

    manager = GameEntryManager()
    res = manager.apply_roll(game, rolled=int(dice_value), player_id=player.id)

    # Если серия шестерок продолжается — просто подсказываем бросать дальше.
    if res.status == "continue":
        return JsonResponse({
            "ok": True,
            "status": "continue",
            "message": res.message,
            "six_count": res.six_count,
        })

    # Если серия завершилась — шлём ВСЕ ходы по очереди в TG (в фоне), а вебхуку быстро отвечаем.
    if res.status == "completed":
        bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        if bot_token and res.moves:
            Thread(
                target=send_moves_sequentially,
                args=(bot_token, tg_from_id, res.moves),
                kwargs={"per_message_delay": 0.6},
                daemon=True,
            ).start()

        return JsonResponse({
            "ok": True,
            "status": "completed",
            "message": "Серия завершена, отправляем ходы пользователю.",
            "six_count": res.six_count,  # 0
            # можно вернуть для диагностики количество готовых к отправке сообщений:
            "moves_count": len(res.moves),
        })

    # Обычный одиночный ход (без серии): отправим один месседж тоже в фоне.
    if res.status == "single":
        bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
        if bot_token and res.moves:
            Thread(
                target=send_moves_sequentially,
                args=(bot_token, tg_from_id, res.moves),
                kwargs={"per_message_delay": 0.0},  # один ход — задержка не нужна
                daemon=True,
            ).start()

        return JsonResponse({
            "ok": True,
            "status": "single",
            "message": res.message,
            "moves_count": len(res.moves),  # 1
        })

    # ignored
    return JsonResponse({
        "ok": True,
        "status": "ignored",
        "message": res.message,
        "six_count": res.six_count,
    })


# helpers внутри этого же файла (или вынеси в helpers/player_lookup.py)
from django.db import transaction


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
