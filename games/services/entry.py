# games/services/entry.py
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from games.services.board import resolve_chain, get_cell_image_name
from games.services.images import normalize_image_relpath
from django.db import transaction
from django.db.models import Max

from games.models import Game, Move
from games.services.board import resolve_chain
# имя картинки берём прямо из board.json
try:
    from games.services.board import get_cell_image_name  # -> str | None
except Exception:
    def get_cell_image_name(_: int) -> Optional[str]:
        return None

# из имени/пути строим URL (публичный или защищённый)
try:
    from games.services.images import image_url_from_board_name  # (image_name, player_id=None, game_id=None) -> str | None
except Exception:
    def image_url_from_board_name(_: Optional[str], *, player_id: Optional[int] = None, game_id: Optional[int] = None) -> Optional[str]:
        return None


@dataclass
class EntryStepResult:
    status: str            # "ignored" | "continue" | "completed" | "single"
    message: str
    six_count: int
    moves: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def final_cell(self) -> Optional[int]:  # для обратной совместимости со старым кодом
        if not self.moves:
            return None
        try:
            return int(self.moves[-1].get("to_cell"))
        except Exception:
            return None


class GameEntryManager:
    """Серии шестерок + обычные ходы. Считывает/пишет current_six_number и ВСЕГДА отдаёт массив moves."""

    # ---------- utils ----------
    def _next_move_number(self, game: Game) -> int:
        last_no = getattr(game, "last_move_number", None)
        if last_no is None:
            agg = Move.objects.filter(game=game).aggregate(Max("move_number"))
            last_no = agg.get("move_number__max") or 0
        return int(last_no) + 1

    def _serialize_move(self, mv: Move, player_id: Optional[int] = None) -> dict:
        img_name = get_cell_image_name(int(mv.to_cell or 0))
        img_url = image_url_from_board_name(img_name, player_id=player_id, game_id=mv.game_id)
        return {
            "id": mv.id,
            "move_number": mv.move_number,
            "rolled": mv.rolled,
            "from_cell": mv.from_cell,
            "to_cell": mv.to_cell,
            "note": mv.note,
            "event_type": str(getattr(mv, "event_type", "")),
            "applied_rules": (mv.state_snapshot or {}).get("applied_rules", []),
            "on_hold": getattr(mv, "on_hold", False),
            "image_url": img_url,
        }

    def _serialize_moves(self, qs, player_id: Optional[int] = None) -> List[Dict[str, Any]]:
        return [self._serialize_move(mv, player_id) for mv in qs]

    # ---------- main ----------
    @transaction.atomic
    def apply_roll(self, game: Game, rolled: int, player_id: Optional[int] = None) -> EntryStepResult:
        game = Game.objects.select_for_update().get(pk=game.pk)

        current_cell = int(getattr(game, "current_cell", 0) or 0)
        six_count = int(getattr(game, "current_six_number", 0) or 0)
        has_moves_any = Move.objects.filter(game=game).exists()
        has_non_hold = Move.objects.filter(game=game, on_hold=False).exists()

        series_active = six_count > 0
        at_start = (not has_non_hold) and (current_cell == 0)

        # A) старт: нужна 6

        if at_start and not series_active:
            if rolled != 6:
                return EntryStepResult(status="ignored", message="Для входа нужна шестерка.", six_count=0, moves=[])
            move_no = self._next_move_number(game)
            final_cell, chain = resolve_chain(6)
            img_rel = normalize_image_relpath(get_cell_image_name(6))
            Move.objects.create(
                game=game, move_number=move_no, rolled=6,
                from_cell=current_cell, to_cell=final_cell,
                event_type=getattr(getattr(Move, "EventType", object), "NORMAL", "NORMAL"),
                note="entry: first six",
                state_snapshot={"applied_rules": [{"from": a, "to": b} for a, b in chain]},
                image_url=img_rel,
                on_hold=True,
            )
            game.current_cell = final_cell
            game.current_six_number = 1
            if hasattr(game, "last_move_number"):
                game.last_move_number = move_no
                game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])
            else:
                game.save(update_fields=["current_cell", "current_six_number"])
            return EntryStepResult(status="continue", message="Шестерка! Бросьте кубик ещё раз.", six_count=game.current_six_number, moves=[])

        # B) серия активна и снова 6 — копим
        if series_active and rolled == 6:
            move_no = self._next_move_number(game)
            target = (6 * (six_count + 1)) if at_start else (current_cell + 6)
            final_cell, chain = resolve_chain(target)
            img_rel = normalize_image_relpath(get_cell_image_name(final_cell))
            Move.objects.create(
                game=game, move_number=move_no, rolled=6,
                from_cell=current_cell, to_cell=final_cell,
                event_type=getattr(getattr(Move, "EventType", object), "NORMAL", "NORMAL"),
                note=("entry: six #{}".format(six_count + 1) if at_start else "series: six"),
                state_snapshot={"applied_rules": [{"from": a, "to": b} for a, b in chain]},
                image_url=img_rel,
                on_hold=True,
            )
            game.current_cell = final_cell
            game.current_six_number = six_count + 1
            if hasattr(game, "last_move_number"):
                game.last_move_number = move_no
                game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])
            else:
                game.save(update_fields=["current_cell", "current_six_number"])
            return EntryStepResult(status="continue", message="Шестерка! Бросьте кубик ещё раз.", six_count=game.current_six_number, moves=[])

        # C) в игре выпала 6 — старт серии
        if (not series_active) and (rolled == 6) and (has_non_hold or current_cell > 0 or has_moves_any):
            move_no = self._next_move_number(game)
            target = current_cell + 6
            final_cell, chain = resolve_chain(target)
            img_rel = normalize_image_relpath(get_cell_image_name(final_cell))
            Move.objects.create(
                game=game, move_number=move_no, rolled=6,
                from_cell=current_cell, to_cell=final_cell,
                event_type=getattr(getattr(Move, "EventType", object), "NORMAL", "NORMAL"),
                note="series: first six",
                state_snapshot={"applied_rules": [{"from": a, "to": b} for a, b in chain]},
                image_url=img_rel,
                on_hold=True,
            )
            game.current_cell = final_cell
            game.current_six_number = 1
            if hasattr(game, "last_move_number"):
                game.last_move_number = move_no
                game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])
            else:
                game.save(update_fields=["current_cell", "current_six_number"])
            return EntryStepResult(status="continue", message="Шестерка! Бросьте кубик ещё раз.", six_count=game.current_six_number, moves=[])

        # D) серия активна и НЕ 6 — финализируем: снимаем on_hold и отдаём все ходы
        if series_active and rolled != 6:
            move_no = self._next_move_number(game)
            target = (int(rolled) if at_start else current_cell + int(rolled))
            final_cell, chain = resolve_chain(target)
            img_rel = normalize_image_relpath(get_cell_image_name(final_cell))
            Move.objects.create(
                game=game, move_number=move_no, rolled=int(rolled),
                from_cell=current_cell, to_cell=final_cell,
                event_type=getattr(getattr(Move, "EventType", object), "NORMAL", "NORMAL"),
                note=("entry: final non-six" if at_start else "series: final non-six"),
                state_snapshot={"applied_rules": [{"from": a, "to": b} for a, b in chain]},
                image_url=img_rel,
                on_hold=True,
            )
            game.current_cell = final_cell
            game.current_six_number = 0
            if hasattr(game, "last_move_number"):
                game.last_move_number = move_no
                game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])
            else:
                game.save(update_fields=["current_cell", "current_six_number"])

            qs = Move.objects.select_for_update().filter(game=game, on_hold=True).order_by("move_number")
            released_list = list(qs)
            qs.update(on_hold=False)

            return EntryStepResult(
                status="completed",
                message="Серия завершена. Отдаём все накопленные ходы.",
                six_count=0,
                moves=self._serialize_moves(released_list, player_id=player_id),
            )

        # E) одиночный ход (без серии)
        if (not series_active) and (rolled != 6):
            move_no = self._next_move_number(game)
            target = current_cell + int(rolled)
            final_cell, chain = resolve_chain(target)
            img_rel = normalize_image_relpath(get_cell_image_name(final_cell))
            mv = Move.objects.create(
                game=game, move_number=move_no, rolled=int(rolled),
                from_cell=current_cell, to_cell=final_cell,
                event_type=getattr(getattr(Move, "EventType", object), "NORMAL", "NORMAL"),
                note="single move",
                state_snapshot={"applied_rules": [{"from": a, "to": b} for a, b in chain]},
                image_url=img_rel,
                on_hold=False,
            )
            game.current_cell = final_cell
            if hasattr(game, "last_move_number"):
                game.last_move_number = move_no
                game.save(update_fields=["current_cell", "last_move_number"])
            else:
                game.save(update_fields=["current_cell"])

            return EntryStepResult(
                status="single",
                message="Ход выполнен.",
                six_count=0,
                moves=[self._serialize_move(mv, player_id=player_id)],
            )

        # fallback
        return EntryStepResult(
            status="ignored",
            message="Состояние не требует действий.",
            six_count=int(getattr(game, "current_six_number", 0) or 0),
            moves=[],
        )

    # обратная совместимость
    def apply_entry_roll(self, game: Game, rolled: int, player_id: Optional[int] = None) -> EntryStepResult:
        return self.apply_roll(game, rolled, player_id=player_id)
