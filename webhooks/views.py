import json
from datetime import datetime, timezone as dt_tz

from django.db import transaction
from django.http import JsonResponse, HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone

from players.models import Player
from .models import Game, Move

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
        # даже если парс не удался — зафиксируем пустой ход как "технический"
        payload = {"_parse_error": True}

    meta = _extract_telegram_meta(payload)
    tg_from_id = meta["from_id"]
    tg_username = meta["username"]
    tg_dt = meta["message_date"]
    dice_value = meta["dice_value"]
    update_id = meta["update_id"]

    # Если нет броска — просто подтверждаем, но payload сохраним в последующем ходе логики, если нужно.
    # Здесь считаем, что обрабатываем только сообщения с кубиком.
    if dice_value is None:
        return JsonResponse({"ok": True, "captured": True, "dice_value": None})

    # ---- Находим игрока ----
    # Предпочтительно по telegram_id; если нет — по username. Если игрок не найден — создаём "технического".
    player = None
    if tg_from_id is not None:
        player = Player.objects.filter(telegram_id=tg_from_id).first()
    if not player and tg_username:
        player = Player.objects.filter(username__iexact=tg_username).first()
    if not player:
        # Создай с нужными полями под свою модель Player
        player = Player.objects.create(
            telegram_id=tg_from_id,
            username=tg_username or "",
            email=f"tg_{tg_from_id or 'unknown'}@example.local",  # при необходимости замени
        )

    # ---- Берём текущую игру (или создаём новую) ----
    game = Game.resume_last(player=player)
    if not game:
        game = Game.start_new(player=player, game_type="telegram_dice", game_name="Лила (TG)")

    # Идемпотентность: если уже обрабатывали этот update — выходим
    if update_id is not None:
        already = Move.objects.filter(game=game, webhook_payload__update_id=update_id).exists()
        if already:
            return JsonResponse({"ok": True, "captured": True, "dice_value": dice_value, "dedup": True})

    # ---- Считаем переход клетки ----
    from_cell = game.current_cell or 0
    to_cell = from_cell + int(dice_value)

    # Здесь можно вставить твою бизнес-логику стрел/змей:
    event_type = Move.EventType.NORMAL
    note = ""
    state_after = {
        "update_id": update_id,
        "message_id": meta["message_id"],
        "username": tg_username,
        "applied_rules": [],  # сюда можешь класть, что сработало (змея/стрела/бонус)
    }