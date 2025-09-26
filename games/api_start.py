from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponseBadRequest
import json
import os
from django.utils.timezone import now
from games.models import Game, Player
from games.services.tg_send import send_dice
from django.conf import settings
BOT_TOKEN = getattr(settings, "TELEGRAM_BOT_TOKEN", None)

@csrf_exempt
def start_game_endpoint(request):
    """Старт игры без какой-либо доп. аутентификации (как у вебхука)."""
    if request.method != "POST":
        return HttpResponseBadRequest("POST only")

    # читаем payload
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except Exception:
        payload = request.POST.dict()

    chat_id = payload.get("chat_id") or payload.get("telegram_id") or payload.get("user_id")
    email   = payload.get("email")
    if not chat_id:
        return HttpResponseBadRequest("chat_id or telegram_id required")

    if not BOT_TOKEN:
        return JsonResponse({"ok": False, "error": "bot_token_not_set"}, status=500)

    # находим/создаём игрока
    player = None
    if email:
        player = Player.objects.filter(email=email).first()
    if not player:
        player = Player.objects.filter(telegram_id=str(chat_id)).first()
    if not player:
        player = Player.objects.create(telegram_id=str(chat_id), email=email or "")

    # берём активную игру или создаём новую
    game = Game.objects.filter(player=player, is_active=True, status=Game.Status.ACTIVE).first()
    if not game:
        game = Game.objects.create(
            player=player,
            status=Game.Status.ACTIVE,
            is_active=True,
            current_cell=0,
            current_six_number=0,
            game_type="default",
            game_name=f"Game {now():%Y-%m-%d %H:%M:%S}",
        )

    # сразу кидаем первый кубик от бота
    tg_resp = send_dice(BOT_TOKEN, chat_id)

    # если Telegram не принял (обычно 403 — пользователь не нажал Start у бота)
    if not (isinstance(tg_resp, dict) and tg_resp.get("ok")):
        return JsonResponse({
            "ok": False,
            "error": "telegram_send_failed",
            "hint": "User must start the bot first (open https://t.me/<BOT_USERNAME>?start=startgame)",
            "telegram_response": tg_resp,
        }, status=400)

    return JsonResponse({
        "ok": True,
        "game_id": str(game.id),
        "telegram_response": tg_resp.get("result", {}),
    })
