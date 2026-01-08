from __future__ import annotations
from typing import Optional
from django.db import transaction
from games.services.entry_step_result import EntryStepResult
from games.models import Game, Move
import games.services.apply_roll as apply_roll


class GameEntryManager:
    """Серии шестерок + обычные ходы.
    Поддержаны:
      - нейтральные клетки (ничего не делают);
      - змеи/стрелы (resolve_chain);
      - ДОП. поля boards.json: snake_to / ladder_to (и синонимы);
      - финиш: выход через 68 ИЛИ точный финиш на 72;
      - при переборе на верхнем ряду (69–71) — стоим и просим переброс;
      - верхний ряд (62–72), частные случаи — пошагово.
    """

    # ---------- main ----------
    @transaction.atomic
    def apply_roll(self, game: Game, rolled: int, player_id: Optional[int] = None) -> EntryStepResult:
        game = Game.objects.select_for_update().get(pk=game.pk)
        current_cell = int(getattr(game, "current_cell", 0) or 0)
        six_count = int(getattr(game, "current_six_number", 0) or 0)
        has_moves_any = Move.objects.filter(game=game).exists()
        has_non_hold = Move.objects.filter(game=game, on_hold=False).exists()

        series_active = six_count > 0
        # consider we're still at start as long as there are no non-hold moves
        at_start = not has_non_hold

        # --- НОВОЕ: строгая логика верхнего ряда (после 68) ---
        # Если уже прошли 68 (т.е. стоим на 69..71),
        # и бросок больше оставшегося количества клеток до 72 — стоим и просим переброс.
        remaining = EntryStepResult.BOARD_MAX - current_cell  # 72 - позиция
        if current_cell > EntryStepResult.EXIT_CELL or current_cell + rolled > EntryStepResult.BOARD_MAX:
            if rolled > remaining:
                return EntryStepResult(
                    status="ignored",
                    message=f"Випало {rolled}, але до фінішу лишилось лише {remaining}. Бросьте кубик ще раз 🎲",
                    six_count=six_count,
                    moves=[],
                )

        # --- START OF GAME: handle 6-combos exactly as in the rules ---
        if at_start:
            return apply_roll.at_first_start(rolled=rolled, game=Game, six_count=six_count, player_id=player_id)

        # --- /START OF GAME --- (ниже — обычная логика, когда мы уже не в начальном состоянии)

        # A) старт: нужна 6 (если всё ещё at_start, но без серии)
        if at_start and not series_active:
            return apply_roll.at_start_no_series_active(rolled=rolled, game=game, current_cell=current_cell,
                                                        player_id=player_id)

        # B) серия активна и снова 6 — копим
        if series_active and rolled == 6:
            return apply_roll.series_active_rolled_six(game=game, current_cell=current_cell, player_id=player_id,
                                                       six_count=six_count, on_start=at_start)

        # C) в игре выпала 6 — старт серии
        if (not series_active) and (rolled == 6) and (has_non_hold or current_cell > 0 or has_moves_any):
            return apply_roll.no_active_series_rolled_six(game=game, current_cell=current_cell, player_id=player_id)

        # D) серия активна и НЕ 6 — финал серии: снимаем on_hold и отдаём все ходы
        if series_active and rolled != 6:
            return apply_roll.series_active_rolled_not_six(game=game, current_cell=current_cell, player_id=player_id,
                                                       six_count=six_count, on_start=at_start, rolled=rolled)

        # E) одиночный ход (без серии)
        if (not series_active) and (rolled != 6):
            return apply_roll.no_active_series_rolled_not_six(game=game, current_cell=current_cell, player_id=player_id,
                                                              rolled=rolled)

        # fallback
        return EntryStepResult(
            status="ignored",
            message="Стан не потребує дій.",
            six_count=int(getattr(game, "current_six_number", 0) or 0),
            moves=[],
        )
