from django.utils.deprecation import MiddlewareMixin
from rest_framework.authentication import BaseAuthentication
from rest_framework import exceptions
from .models import ApiKey
from django.urls import path
from django.http import JsonResponse


def ping(request):
    return JsonResponse({"ok": True, "service": "api", "v": 1})


class ApiKeyAuthentication(BaseAuthentication):
    keyword = "Bearer"  # чтобы работало с Authorization: Bearer <ключ>

    def authenticate(self, request):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith(f"{self.keyword} "):
            return None  # DRF попробует другие схемы, если есть

        token = auth.split(" ", 1)[1].strip()
        try:
            key = ApiKey.objects.get(key=token, is_active=True)
        except ApiKey.DoesNotExist:
            raise exceptions.AuthenticationFailed("Invalid API key")

        # опциональная проверка IP
        if key.allowed_ips:
            ip = request.META.get("REMOTE_ADDR", "")
            allowed = [x.strip() for x in key.allowed_ips.split(",") if x.strip()]
            if ip not in allowed:
                raise exceptions.AuthenticationFailed("IP not allowed")

        # В DRF нужно вернуть (user, auth). Пользователя у нас нет — используем Anonymous.
        from django.contrib.auth.models import AnonymousUser
        return (AnonymousUser(), key)



import random
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status

from players.models import Player
from games.models import Game
from games.services.board import get_cell

MAX_CELL = 72

def _apply_board_events(cell_obj, to_cell):
    """
    Логика «змейки/стрелы»: ladders eat snakes → приоритет у лестницы.
    cell_obj — объект клетки, на которую мы пришли до событий.
    """
    if not cell_obj:
        return to_cell, "normal"
    # Поля могут называться ladder_to/snake_to или event:{type,to}
    ladder_to = cell_obj.get("ladder_to")
    snake_to  = cell_obj.get("snake_to")
    event     = cell_obj.get("event")

    if event and isinstance(event, dict):
        et = event.get("type")
        if et == "ladder":
            return int(event.get("to")), "ladder"
        if et == "snake":
            return int(event.get("to")), "snake"

    # Приоритет лестницы
    if ladder_to:
        return int(ladder_to), "ladder"
    if snake_to:
        return int(snake_to), "snake"
    return to_cell, "normal"

@api_view(["POST"])
def roll_dice(request):
    """
    Тело запроса (JSON):
      {
        "telegram_id": 123456789,
        "game_type": "leela",        # опц.
        "game_name": "Лила #1"       # опц.
      }

    Заголовок:
      Authorization: Token <DRF_TOKEN>
    """
    tg_id = request.data.get("telegram_id")
    game_type = request.data.get("game_type") or "leela"
    game_name = request.data.get("game_name") or ""

    if not tg_id:
        return Response({"error": "telegram_id is required"}, status=status.HTTP_400_BAD_REQUEST)

    try:
        player = Player.objects.get(telegram_id=tg_id)
    except Player.DoesNotExist:
        return Response({"error": "player not found"}, status=status.HTTP_404_NOT_FOUND)

    # Берём последнюю актуальную игру; если нет — создаём новую
    game = Game.resume_last(player, game_type=game_type) or Game.start_new(player, game_type=game_type, game_name=game_name)

    rolled = random.randint(1, 6)
    from_cell = game.current_cell or 0
    tentative = from_cell + rolled
    if tentative > MAX_CELL:
        tentative = MAX_CELL

    # Смотрим описание клетки, на которую пришли (до событий)
    base_cell_obj = get_cell(tentative)
    to_cell, event_type = _apply_board_events(base_cell_obj, tentative)

    # Описание финальной клетки (после событий)
    final_cell_obj = get_cell(to_cell)

    # Собираем state_after (что хочешь — минимум финальная клетка)
    state_after = {
        "rolled": rolled,
        "from_cell": from_cell,
        "to_cell": to_cell,
        "event_type": event_type,
    }

    # Пишем ход и обновляем игру
    game.add_move(
        rolled=rolled,
        from_cell=from_cell,
        to_cell=to_cell,
        event_type=event_type,
        note="API roll",
        state_after=state_after,
    )

    # Если дошли до финала — закроем игру
    if to_cell >= MAX_CELL:
        game.finish()

    # Что отдать наружу
    def pack(cell_obj):
        if not cell_obj:
            return None
        return {
            "n": int(cell_obj.get("n") or cell_obj.get("cell")),
            "title": cell_obj.get("title"),
            "meaning": cell_obj.get("meaning") or cell_obj.get("description"),
            "prompt": cell_obj.get("prompt"),
            "ladder_to": cell_obj.get("ladder_to"),
            "snake_to": cell_obj.get("snake_to"),
            "rule": cell_obj.get("rule"),
        }

    return Response({
        "ok": True,
        "game_id": str(game.id),
        "status": game.status,
        "is_active": game.is_active,
        "rolled": rolled,
        "from_cell": from_cell,
        "to_cell": to_cell,
        "event_type": event_type,
        "base_cell": pack(base_cell_obj),   # куда встали до применения событий
        "final_cell": pack(final_cell_obj), # где оказались в итоге
        "last_move_number": game.last_move_number,
    })


from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework import status
from .serializers import PlayerCreateSerializer

@api_view(["POST"])
def create_player(request):
    """
    Создаёт игрока.
    Тело JSON:
    {
      "email": "user@example.com",            # обязательное (у тебя unique=True)
      "telegram_id": 123456789,               # обязательное (у тебя unique=True)
      "telegram_username": "nickname",        # опционально
      "bot_token": "xxx",                     # опционально
      "main_status": "active|inactive|banned",
      "payment_status": "none|pending|paid|refunded",
      "player_type": "free|trial|premium|admin",
      "game_type": "leela",                   # опционально (пока просто поле у Player)
      "game_name": "Лила #1"                  # опционально
    }
    """
    ser = PlayerCreateSerializer(data=request.data)
    if not ser.is_valid():
        return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)

    try:
        player = ser.save()
    except Exception as e:
        # Например, нарушение уникальности email/telegram_id
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    # что отдаём наружу
    data = {
        "id": player.id,
        "email": player.email,
        "telegram_id": player.telegram_id,
        "telegram_username": player.telegram_username,
        "main_status": player.main_status,
        "payment_status": player.payment_status,
        "player_type": player.player_type,
        "game_type": player.game_type,
        "game_name": player.game_name,
        "registered_at": player.registered_at,
    }
    return Response(data, status=status.HTTP_201_CREATED)
