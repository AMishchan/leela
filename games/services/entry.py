from __future__ import annotations
from dataclasses import dataclass, field
from time import sleep
from typing import List, Optional, Dict, Any
from django.utils import timezone

from django.db import transaction
from django.db.models import Max

from games.models import Game, Move
from games.services.board import resolve_chain, get_cell_image_name
from games.services.images import normalize_image_relpath, image_url_from_board_name

@dataclass
class EntryStepResult:
    status: str            # "ignored" | "continue" | "completed" | "single" | "finished"
    message: str
    six_count: int
    moves: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def final_cell(self) -> Optional[int]:
        if not self.moves:
            return None
        try:
            return int(self.moves[-1].get("to_cell"))
        except Exception:
            return None


class GameEntryManager:
    """Серии шестерок + обычные ходы.
    Поддержаны:
      - нейтральные клетки (ничего не делают);
      - змеи/стрелы (resolve_chain);
      - ДОП. поля boards.json: snake_to / ladder_to (и синонимы);
      - финиш только через 68; при недолетах идём до 72, падаем и продолжаем остаток;
      - верхний ряд (62–72), частные случаи — пошагово.
    """

    EVENT_NORMAL = getattr(getattr(Move, "EventType", object), "NORMAL", "NORMAL")

    EXIT_CELL = 68
    BOARD_MAX = 72

    # ленивый кэш для alt-правил
    _ALT_MAP: Optional[Dict[int, int]] = None

    # --- поддержка разных ключей в boards.json ---
    ALT_KEYS_PRIORITY = (
        ("snake_to", "ladder_to"),
        ("snake2", "ladder2"),
        ("snake", "ladder"),
        ("snakeTo", "ladderTo"),
    )

    # -------------------------------
    def _six_continue_text(self, six_count: int) -> str:
        # синоним на русский вариант (чтобы не падало, если где-то зовётся по старому имени)
        sleep(3.0)
        return self._six_continue_text_ru(six_count)


    def _six_continue_text_ru(self, six_count: int) -> str:
        def ru_plural(n: int, one: str, few: str, many: str) -> str:
            n = abs(n)
            if 11 <= (n % 100) <= 14:
                return many
            last = n % 10
            if last == 1:
                return one
            if 2 <= last <= 4:
                return few
            return many

        form = ru_plural(six_count, "шестёрку", "шестёрки", "шестёрок")
        return (
            f"Отлично! Вы накопили {six_count} {form}. "
            "Бросайте кубик ещё раз. "
            "Как только выпадет число, отличное от 6, "
            "я отправлю все накопленные ходы по порядку."
        )

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

    def _extract_alt_to(self, meta: dict) -> Optional[int]:
        if not isinstance(meta, dict):
            return None
        # Приоритет: snake > ladder
        for snake_key, ladder_key in self.ALT_KEYS_PRIORITY:
            val = meta.get(snake_key)
            if val not in (None, ""):
                try:
                    return int(val)
                except Exception:
                    pass
            val = meta.get(ladder_key)
            if val not in (None, ""):
                try:
                    return int(val)
                except Exception:
                    pass
        return None

    def _get_alt_map(self) -> Dict[int, int]:
        """Строим {cell: to_cell} по snake*_to/ladder*_to (и синонимам) из boards.json, кэшируем."""
        if self._ALT_MAP is not None:
            return self._ALT_MAP

        mapping: Dict[int, int] = {}
        try:
            import importlib
            board_mod = importlib.import_module("games.services.board")

            # 1) Функции доступа к клетке
            getter = None
            for name in ("get_cell", "get_cell_props", "get_cell_data", "cell"):
                if hasattr(board_mod, name):
                    cand = getattr(board_mod, name)
                    if callable(cand):
                        getter = cand
                        break

            if getter:
                for i in range(1, self.BOARD_MAX + 1):
                    try:
                        meta = getter(i) or {}
                        to = self._extract_alt_to(meta)
                        if to is not None:
                            mapping[i] = int(to)
                    except Exception:
                        continue

            # 2) Прямые структуры
            if not mapping:
                for name in ("BOARD", "BOARD_CELLS", "CELLS", "BOARD_MAP"):
                    if not hasattr(board_mod, name):
                        continue
                    raw = getattr(board_mod, name)
                    if isinstance(raw, dict):
                        for k, v in raw.items():
                            try:
                                cell = int(k)
                                to = self._extract_alt_to(v or {})
                                if to is not None:
                                    mapping[cell] = int(to)
                            except Exception:
                                continue
                    elif isinstance(raw, list):
                        for idx, v in enumerate(raw):
                            cell = idx + 1  # 1-базная нумерация
                            try:
                                to = self._extract_alt_to(v or {})
                                if to is not None:
                                    mapping[cell] = int(to)
                            except Exception:
                                continue
                    if mapping:
                        break
        except Exception:
            mapping = {}

        self._ALT_MAP = mapping
        return mapping

    def _resolve_full(self, cell: int):
        """
        1) resolve_chain (базовые змеи/стрелы),
        2) alt: snake_to/ladder_to (и синонимы),
        цикл до стабилизации (max 10 итераций).
        Возврат: (final_cell, chain_pairs)
        """
        pos = int(cell)
        applied: List[List[int]] = []
        alt_map = self._get_alt_map()

        for _ in range(10):
            # База
            base_final, base_chain = resolve_chain(pos)
            if base_chain:
                applied.extend([[a, b] for a, b in base_chain])
                pos = int(base_final)
                continue

            # Alt
            to = alt_map.get(pos)
            if to is not None and int(to) != pos:
                applied.append([pos, int(to)])
                pos = int(to)
                continue

            break

        return pos, applied

    def _walk_n_steps(self, start_cell: int, steps: int):
        """
        Двигаемся на 'steps' клеток:
          - НЕ применяем змей/лестниц на промежуточных клетках (только считаем шаги).
          - Исключение: если по пути попали ровно на 72 — сразу применяем её правило и продолжаем остаток.
          - По завершении шагов применяем правила ТОЛЬКО для конечной клетки (остановки): _resolve_full(...).
          - Если по пути попали ровно на 68 — немедленный выход.
        Возвращает: (final_cell, chain_list, hit_exit)
        """
        pos = int(start_cell)
        total_chain: List[List[int]] = []
        hit_exit = False

        for _ in range(int(steps)):
            pos += 1

            # мгновенный выход, если достигли 68 внутри хода
            if pos == self.EXIT_CELL:
                hit_exit = True
                return pos, total_chain, hit_exit

            # спец-правило 72: сразу применяем и продолжаем
            if pos == self.BOARD_MAX:
                pos_after_72, chain72 = self._resolve_full(pos)
                if chain72:
                    total_chain.extend(chain72)
                pos = int(pos_after_72)

        # применяем правила на клетке остановки
        final_pos, end_chain = self._resolve_full(pos)
        if end_chain:
            total_chain.extend(end_chain)

        if int(final_pos) == self.EXIT_CELL:
            hit_exit = True

        return int(final_pos), total_chain, hit_exit

    # ——— завершение игры (единый хелпер) ———
    def _mark_finished_nonactive(self, game: Game):
        game.current_six_number = 0
        game.status = getattr(Game.Status, "FINISHED", "finished")
        if hasattr(game, "is_active"):
            game.is_active = False
            game.save(update_fields=["current_six_number", "status", "is_active"])
        else:
            game.save(update_fields=["current_six_number", "status"])

    def _finish_game_and_release(self, game: Game, player_id: Optional[int] = None) -> EntryStepResult:
        qs = Move.objects.select_for_update().filter(game=game, on_hold=True).order_by("move_number")
        released_list = list(qs)
        qs.update(on_hold=False)

        # NEW: зафиксировать завершение в БД (с полным списком финальных ходов)
        self._persist_finished_record(game, moves=released_list, reason="exit_68", player_id=player_id)

        self._mark_finished_nonactive(game)

        return EntryStepResult(
            status="finished",
            message="Вихід через 68. Гра завершена.",
            six_count=0,
            moves=self._serialize_moves(released_list, player_id=player_id),
        )

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
            final_cell, chain, hit_exit = self._walk_n_steps(0, 6)
            img_rel = normalize_image_relpath(get_cell_image_name(final_cell))


            Move.objects.create(
                game=game, move_number=move_no, rolled=6,
                from_cell=current_cell, to_cell=final_cell,
                event_type=self._event_from_chain(chain),
                note="entry: first six",
                state_snapshot={"applied_rules": self._rules_payload(chain)},
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

            if hit_exit:
                return self._finish_game_and_release(game, player_id=player_id)

            return EntryStepResult(
                status="continue",
                message=self._six_continue_text(game.current_six_number),
                six_count=game.current_six_number,
                moves=[],
            )

        # B) серия активна и снова 6 — копим
        if series_active and rolled == 6:
            move_no = self._next_move_number(game)
            final_cell, chain, hit_exit = self._walk_n_steps(current_cell, 6)
            img_rel = normalize_image_relpath(get_cell_image_name(final_cell))

            Move.objects.create(
                game=game, move_number=move_no, rolled=6,
                from_cell=current_cell, to_cell=final_cell,
                event_type=self._event_from_chain(chain),
                note=("entry: six #{}".format(six_count + 1) if at_start else "series: six"),
                state_snapshot={"applied_rules": self._rules_payload(chain)},
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

            if hit_exit:
                return self._finish_game_and_release(game, player_id=player_id)

            return EntryStepResult(
                status="continue",
                message=self._six_continue_text(game.current_six_number),
                six_count=game.current_six_number,
                moves=[],
            )

        # C) в игре выпала 6 — старт серии
        if (not series_active) and (rolled == 6) and (has_non_hold or current_cell > 0 or has_moves_any):
            move_no = self._next_move_number(game)
            final_cell, chain, hit_exit = self._walk_n_steps(current_cell, 6)
            img_rel = normalize_image_relpath(get_cell_image_name(final_cell))

            Move.objects.create(
                game=game, move_number=move_no, rolled=6,
                from_cell=current_cell, to_cell=final_cell,
                event_type=self._event_from_chain(chain),
                note="series: first six",
                state_snapshot={"applied_rules": self._rules_payload(chain)},
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

            if hit_exit:
                return self._finish_game_and_release(game, player_id=player_id)

            return EntryStepResult(
                status="continue",
                message=self._six_continue_text(game.current_six_number),
                six_count=game.current_six_number,
                moves=[],
            )

        # D) серия активна и НЕ 6 — финал серии: снимаем on_hold и отдаём все ходы
        if series_active and rolled != 6:
            move_no = self._next_move_number(game)
            final_cell, chain, hit_exit = self._walk_n_steps(current_cell, int(rolled))
            img_rel = normalize_image_relpath(get_cell_image_name(final_cell))

            Move.objects.create(
                game=game, move_number=move_no, rolled=int(rolled),
                from_cell=current_cell, to_cell=final_cell,
                event_type=self._event_from_chain(chain),
                note=("entry: final non-six" if at_start else "series: final non-six"),
                state_snapshot={"applied_rules": self._rules_payload(chain)},
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

            if final_cell == self.EXIT_CELL or hit_exit:
                # NEW: снапшот завершающей серии
                self._persist_finished_record(game, moves=released_list, reason="exit_68", player_id=player_id)
                self._mark_finished_nonactive(game)
                return EntryStepResult(
                    status="finished",
                    message="Вихід через 68. Гра завершена.",
                    six_count=0,
                    moves=self._serialize_moves(released_list, player_id=player_id),
                )

            return EntryStepResult(
                status="completed",
                message="Серия завершена. Отдаём все накопленные ходы.",
                six_count=0,
                moves=self._serialize_moves(released_list, player_id=player_id),
            )

        # E) одиночный ход (без серии)
        if (not series_active) and (rolled != 6):
            move_no = self._next_move_number(game)
            final_cell, chain, hit_exit = self._walk_n_steps(current_cell, int(rolled))
            img_rel = normalize_image_relpath(get_cell_image_name(final_cell))

            mv = Move.objects.create(
                game=game, move_number=move_no, rolled=int(rolled),
                from_cell=current_cell, to_cell=final_cell,
                event_type=self._event_from_chain(chain),
                note="single move",
                state_snapshot={"applied_rules": self._rules_payload(chain)},
                image_url=img_rel,
                on_hold=False,
            )
            game.current_cell = final_cell
            if hasattr(game, "last_move_number"):
                game.last_move_number = move_no
                game.save(update_fields=["current_cell", "last_move_number"])
            else:
                game.save(update_fields=["current_cell"])

            if final_cell == self.EXIT_CELL or hit_exit:
                # NEW: снапшот одиночного финишного хода
                self._persist_finished_record(game, moves=[mv], reason="exit_68", player_id=player_id)
                self._mark_finished_nonactive(game)
                return EntryStepResult(
                    status="finished",
                    message="Вихід через 68. Гра завершена.",
                    six_count=0,
                    moves=[self._serialize_move(mv, player_id=player_id)],
                )

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

    # --- внутри класса GameEntryManager ---

    def _build_finish_payload(self, game: Game, moves: list[Move], *, reason: str, player_id: Optional[int]) -> dict:
        """Готовим консистентный снапшот завершения партии."""
        try:
            total_moves = Move.objects.filter(game=game, on_hold=False).count()
        except Exception:
            total_moves = len(moves)

        return {
            "game_id": getattr(game, "id", None),
            "player_id": player_id,
            "finished_at": timezone.now().isoformat(),
            "finished_reason": reason,  # например: "exit_68"
            "final_cell": int(getattr(game, "current_cell", 0) or 0),
            "total_moves": int(total_moves),
            "moves": [
                {
                    "id": mv.id,
                    "move_number": mv.move_number,
                    "rolled": mv.rolled,
                    "from_cell": mv.from_cell,
                    "to_cell": mv.to_cell,
                    "note": mv.note,
                    "event_type": str(getattr(mv, "event_type", "")),
                    "on_hold": getattr(mv, "on_hold", False),
                } for mv in moves
            ],
        }

    def _persist_finished_record(self, game: Game, *, moves: list[Move], reason: str,
                                 player_id: Optional[int] = None) -> None:
        """
        Пишем факт завершения партии в БД.
        1) Если есть модель CompletedGame — создаём запись там (best effort).
        2) Иначе положим снапшот в JSON-поле игры, если найдём подходящее.
        3) Дополнительно проставим finished_at / finished_reason, если такие поля у Game существуют.
        """
        payload = self._build_finish_payload(game, moves, reason=reason, player_id=player_id)

        # 1) Пытаемся создать запись в CompletedGame (если модель есть)
        try:
            from games.models import CompletedGame  # type: ignore
            try:
                CompletedGame.objects.create(
                    game=game if "game" in {f.name for f in CompletedGame._meta.fields} else None,
                    game_id=getattr(game, "id", None),
                    player_id=player_id,
                    finished_at=timezone.now(),
                    finished_reason=reason,
                    payload=payload if "payload" in {f.name for f in CompletedGame._meta.fields} else None,
                )
            except Exception:
                # Если поля отличаются — попробуем минимальный набор
                CompletedGame.objects.create(
                    game_id=getattr(game, "id", None),
                    finished_at=timezone.now(),
                    finished_reason=reason,
                )
        except Exception:
            # 2) Нет модели — попробуем сохранить снапшот в самом Game
            updated_fields = []
            for json_field_name in ("result_payload", "final_payload", "results"):
                if hasattr(game, json_field_name):
                    setattr(game, json_field_name, payload)
                    updated_fields.append(json_field_name)

            # 3) Отдельные поля на самой игре, если они есть
            if hasattr(game, "finished_at"):
                game.finished_at = timezone.now()
                updated_fields.append("finished_at")
            if hasattr(game, "finished_reason"):
                game.finished_reason = reason
                updated_fields.append("finished_reason")

            if updated_fields:
                try:
                    game.save(update_fields=list(set(updated_fields)))
                except Exception:
                    # крайний случай — просто не падаем
                    pass

    # --- event helpers ---
    def _et(self, name: str):
        """Безопасно вернуть константу из Move.EventType, иначе — строку."""
        ET = getattr(Move, "EventType", None)
        return getattr(ET, name, name) if ET else name

    def _event_from_chain(self, chain: list[list[int]] | list[tuple[int,int]] | None):
        """
        По последнему срабатыванию определяем тип: LADDER (вверх) или SNAKE (вниз).
        Если срабатываний нет — NORMAL.
        """
        if not chain:
            return self.EVENT_NORMAL
        a, b = map(int, chain[-1])  # последнее правило
        if b > a:
            return self._et("LADDER")
        if b < a:
            return self._et("SNAKE")
        return self.EVENT_NORMAL

    def _rules_payload(self, chain: list[list[int]] | list[tuple[int,int]] | None):
        """Сериализация применённых правил в state_snapshot.applied_rules."""
        if not chain:
            return []
        out = []
        for a, b in chain:
            a = int(a); b = int(b)
            out.append({
                "from": a,
                "to": b,
                "type": "ladder" if b > a else ("snake" if b < a else "neutral"),
            })
        return out
