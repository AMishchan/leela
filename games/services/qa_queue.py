# games/services/qa_queue.py
from threading import Thread
from django.conf import settings
from games.models import Game, Move
from games.services.entry import GameEntryManager
from games.services.board import get_cell_image_name
from games.services.images import image_url_from_board_name


def on_turn_finished_with_series(game: Game, series_moves):
    """
    Серия завершилась: отправляем ПЕРВУЮ карточку серии с ForceReply.
    series_moves — список Move или список dict с {"id": ...}.
    """
    if not series_moves:
        return

    # Нормализуем к списку Move
    moves: list[Move] = []
    for mv in series_moves:
        if isinstance(mv, Move):
            moves.append(mv)
        elif isinstance(mv, dict) and "id" in mv:
            try:
                moves.append(Move.objects.get(id=mv["id"]))
            except Move.DoesNotExist:
                pass

    if not moves:
        return

    # Берём первую по номеру хода
    first_move = sorted(moves, key=lambda m: (m.move_number or 0))[0]

    # Сериализация move → dict
    try:
        mgr = GameEntryManager()
        move_dict = mgr._serialize_move(first_move, player_id=game.player_id)
    except Exception:
        img_name = get_cell_image_name(int(first_move.to_cell or 0))
        img_url = image_url_from_board_name(img_name, player_id=game.player_id, game_id=game.id)
        move_dict = {
            "id": first_move.id,
            "move_number": first_move.move_number,
            "rolled": first_move.rolled,
            "from_cell": first_move.from_cell,
            "to_cell": first_move.to_cell,
            "note": first_move.note,
            "event_type": str(getattr(first_move, "event_type", "")),
            "applied_rules": (first_move.state_snapshot or {}).get("applied_rules", []),
            "on_hold": getattr(first_move, "on_hold", False),
            "image_url": img_url,
        }

    # Отправляем карточку + вопрос (ForceReply)
    from webhooks.views import _send_one_move_and_quiz  # путь корректный для твоей структуры
    bot_token = getattr(settings, "TELEGRAM_BOT_TOKEN", None)
    chat_id = getattr(game.player, "telegram_id", None)
    if bot_token and chat_id:
        Thread(
            target=_send_one_move_and_quiz,
            args=(bot_token, chat_id, move_dict),
            kwargs={"delay": 0.6},
            daemon=True,
        ).start()
