from __future__ import annotations
from dataclasses import dataclass, field
from time import sleep
from typing import List, Optional, Dict, Any
from django.utils import timezone
import random
from django.db import transaction
from django.db.models import Max

from games.models import Game, Move
from games.services.board import resolve_chain, get_cell_image_name
from games.services.images import normalize_image_relpath, image_url_from_board_name
from games.services.game_summary import collect_game_summary, render_summary_prompt
from games.services.openai_client import OpenAIClient


@dataclass
class EntryStepResult:
    status: str  # "ignored" | "continue" | "completed" | "single" | "finished"
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
      - финиш: выход через 68 ИЛИ точный финиш на 72;
      - при переборе на верхнем ряду (69–71) — стоим и просим переброс;
      - верхний ряд (62–72), частные случаи — пошагово.
    """

    # Messages shown while we wait for the very first 6
    START_WAIT_MESSAGES = [
        "Try again! We need a 6.",
        "Not a six yet — roll again 🎲",
        "Close, but not 6. One more time!",
        "Almost there. Throw the dice again!",
        "No 6 this time. Keep rolling!",
        "Ще не шістка — кидаймо ще!",
        "Потрібна шістка для старту. Спробуйте знову.",
    ]

    EVENT_NORMAL = getattr(getattr(Move, "EventType", object), "NORMAL", "NORMAL")

    EXIT_CELL = 68
    BOARD_MAX = 72
    FINISH_CELL = 72  # явная финишная клетка

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
    # Сообщение о финише (без рекурсии и переменных вне области видимости)
    def _finish_message(self, cell: int, analysis: str = "") -> str:
        if int(cell) == self.EXIT_CELL:
            base = "Вихід через 68. Гра завершена."
        else:
            base = "Гра завершена."
        return (f"{base} {analysis}").strip()

    # Ход без правил (обрезаем по BOARD_MAX, exit-флаг и для 68, и для 72)
    def _walk_pure_no_rules(self, start_cell: int, steps: int):
        final_pos = int(start_cell) + int(steps)
        if final_pos > self.BOARD_MAX:
            final_pos = self.BOARD_MAX
        hit_exit = (final_pos == self.EXIT_CELL or final_pos == self.FINISH_CELL)
        return final_pos, [], hit_exit

    def _wait_six_msg(self, rolled: int) -> str:
        """Pick a random 'waiting for first six' message."""
        msg = random.choice(self.START_WAIT_MESSAGES)
        return msg.replace("{rolled}", str(rolled))

    def _create_moves_with_chain(
        self,
        *,
        game: Game,
        start_move_no: int,
        from_cell: int,
        rolled: int,
        final_cell: int,
        chain: list[list[int]] | list[tuple[int, int]],
        on_hold: bool,
        at_start: bool,
    ) -> tuple[int, list[Move]]:
        """
        Persists:
          1) the STEP move: from_cell -> pre_rule_cell (rolled shown here)
          2) each RULE hop as its own move: a -> b (rolled = None)
        Returns: (last_move_no, list_of_created_moves)
        """
        created: list[Move] = []
        move_no = int(start_move_no)

        # 1) STEP: go to the *first* rule start (or final_cell if no rules)
        if chain:
            pre_rule = int(chain[0][0])
        else:
            pre_rule = int(final_cell)

        # only create the step move if it actually moves
        if chain and int(from_cell) != int(pre_rule):
            img_rel_step = normalize_image_relpath(get_cell_image_name(pre_rule))
            created.append(
                Move.objects.create(
                    game=game,
                    move_number=move_no,
                    rolled=int(rolled),
                    from_cell=int(from_cell),
                    to_cell=pre_rule,
                    event_type=self.EVENT_NORMAL,
                    note=(
                        "entry: first six"
                        if at_start and rolled == 6
                        else "series: six"
                        if rolled == 6
                        else "single step"
                        if not chain
                        else "step to rule start"
                    ),
                    state_snapshot={"applied_rules": []},
                    image_url=img_rel_step,
                    on_hold=on_hold,
                )
            )
            move_no += 1

        # 2) RULE HOPS: one Move per (a -> b)
        for a, b in chain:
            a, b = int(a), int(b)
            img_rel_rule = normalize_image_relpath(get_cell_image_name(b))
            created.append(
                Move.objects.create(
                    game=game,
                    move_number=move_no,
                    rolled=rolled,
                    from_cell=a,
                    to_cell=b,
                    event_type=self._et("LADDER")
                    if b > a
                    else self._et("SNAKE")
                    if b < a
                    else self.EVENT_NORMAL,
                    note=f"auto rule: {a}->{b}",
                    state_snapshot={"applied_rules": self._rules_payload([[a, b]])},
                    image_url=img_rel_rule,
                    on_hold=on_hold,
                )
            )
            move_no += 1

        # 3) If no chain, ensure we still have a single move to final_cell
        if not chain and int(from_cell) != int(final_cell):
            img_rel_final = normalize_image_relpath(get_cell_image_name(final_cell))
            created.append(
                Move.objects.create(
                    game=game,
                    move_number=move_no,
                    rolled=int(rolled),
                    from_cell=int(from_cell),
                    to_cell=int(final_cell),
                    event_type=self.EVENT_NORMAL,
                    note="single move",
                    state_snapshot={"applied_rules": []},
                    image_url=img_rel_final,
                    on_hold=on_hold,
                )
            )
            move_no += 1

        # If nothing had to be created (edge case), create a no-op move once:
        if not created:
            img_rel_final = normalize_image_relpath(get_cell_image_name(final_cell))
            created.append(
                Move.objects.create(
                    game=game,
                    move_number=move_no,
                    rolled=int(rolled),
                    from_cell=int(from_cell),
                    to_cell=int(final_cell),
                    event_type=self.EVENT_NORMAL,
                    note="noop",
                    state_snapshot={"applied_rules": []},
                    image_url=img_rel_final,
                    on_hold=on_hold,
                )
            )
            move_no += 1

        return move_no - 1, created

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

        form = ru_plural(six_count, "шістку", "шістки", "шісток")
        return (
            f"Чудово! Ви назбирали {six_count} {form}. "
            "Кидайте кубик ще раз. "
            "Як тільки випаде число, відмінне від 6, "
            "я надішлю всі накопичені ходи по черзі."
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

        applied_rules = (mv.state_snapshot or {}).get("applied_rules", []) or []

        # 1) Клетка остановки ДО применения правила
        pre_rule_cell = int(applied_rules[0]["from"]) if applied_rules else int(mv.to_cell or 0)

        # 2) Читаемый текст про правила
        def _pretty_rules(rules):
            if not rules:
                return ""
            parts = []
            for r in rules:
                a, b = int(r["from"]), int(r["to"])
                rtype = r.get("type")
                if rtype == "ladder":
                    parts.append(f"{a} → {b} (лестница)")
                elif rtype == "snake":
                    parts.append(f"{a} → {b} (змея)")
                else:
                    parts.append(f"{a} → {b}")
            return " ; ".join(parts)

        rules_txt = _pretty_rules(applied_rules)

        # 3) Готовые строки
        human_pre_rule = (
            f"Бросок: {mv.rolled}. Дошли до {pre_rule_cell} — сработало правило: {rules_txt}."
            if applied_rules
            else ""
        )
        human_final = f"Итог: {mv.from_cell} → {mv.to_cell}."

        return {
            "id": mv.id,
            "move_number": mv.move_number,
            "rolled": mv.rolled,
            "from_cell": mv.from_cell,
            "to_cell": mv.to_cell,
            "pre_rule_cell": pre_rule_cell,
            "note": mv.note,
            "event_type": str(getattr(mv, "event_type", "")),
            "applied_rules": applied_rules,
            "chain_pairs": [[r["from"], r["to"]] for r in applied_rules],
            "human_text_pre_rule": human_pre_rule,
            "human_text_final": human_final,
            "image_url": img_url,
            "on_hold": getattr(mv, "on_hold", False),
        }

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
          - Завершаем игру если итоговая клетка (после правил) == 68 ИЛИ == 72.
        Возвращает: (final_cell, chain_list, hit_exit)
        """
        pos = int(start_cell)
        total_chain: List[List[int]] = []
        hit_exit = False

        for _ in range(int(steps)):
            pos += 1

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

        # Завершение — на 68 или 72
        if int(final_pos) == self.EXIT_CELL or int(final_pos) == self.FINISH_CELL:
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
            "finished_reason": reason,  # например: "exit_68" / "finish_72"
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
                }
                for mv in moves
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
                # Если поля отличаются — минимальный набор
                CompletedGame.objects.create(
                    game_id=getattr(game, "id", None),
                    finished_at=timezone.now(),
                    finished_reason=reason,
                )
        except Exception:
            # 2) Нет модели — попытаемся сохранить снапшот в самом Game
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
                    pass  # крайний случай — не падаем

    def _finish_game_and_release(self, game: Game, player_id: Optional[int] = None) -> EntryStepResult:
        qs = Move.objects.select_for_update().filter(game=game, on_hold=True).order_by("move_number")
        released_list = list(qs)
        qs.update(on_hold=False)

        # Запишем завершение в БД (с полным списком финальных ходов)
        reason = "exit_68" if int(game.current_cell) == self.EXIT_CELL else "finish_72"
        self._persist_finished_record(game, moves=released_list, reason=reason, player_id=player_id)

        self._mark_finished_nonactive(game)
        try:
            summary = collect_game_summary(game)
            client = OpenAIClient()
            analysis = client.send_summary_json(summary)
            sleep(3.0)
        except Exception:
            analysis = ""

        return EntryStepResult(
            status="finished",
            message=self._finish_message(game.current_cell, analysis),
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
        # consider we're still at start as long as there are no non-hold moves
        at_start = not has_non_hold

        # --- НОВОЕ: строгая логика верхнего ряда (после 68) ---
        # Если уже прошли 68 (т.е. стоим на 69..71),
        # и бросок больше оставшегося количества клеток до 72 — стоим и просим переброс.
        remaining = self.BOARD_MAX - current_cell  # 72 - позиция
        if current_cell > self.EXIT_CELL or current_cell + rolled > self.BOARD_MAX:
            if rolled > remaining:
                return EntryStepResult(
                    status="ignored",
                    message=f"Випало {rolled}, але до фінішу лишилось лише {remaining}. Бросьте кубик ще раз 🎲",
                    six_count=six_count,
                    moves=[],
                )

        # --- START OF GAME: handle 6-combos exactly as in the rules ---
        if at_start:
            # keep collecting sixes until we see a non-6
            if rolled == 6:
                game.current_six_number = six_count + 1
                game.save(update_fields=["current_six_number"])
                return EntryStepResult(
                    status="continue",
                    message=f"Випала {game.current_six_number}-та шістка. Кидайте далі!",
                    six_count=game.current_six_number,
                    moves=[],
                )

            # we got the first non-6 at start → apply combo rule
            if six_count == 0:
                # no six yet — still waiting for the very first 6
                return EntryStepResult(
                    status="ignored",
                    message=self._wait_six_msg(rolled=rolled),
                    six_count=0,
                    moves=[],
                )

            combo = six_count  # number of 6s collected
            move_no = self._next_move_number(game)
            created_moves: list[Move] = []

            # Build absolute target cells according to the images:
            # 1×6 + X:   0→1→6→(6+X)
            # 2×6 + X:   0→1→6→(6+X)      (X is applied from cell 6, ladders/snakes work)
            # 3×6 + X:   0→1→(1+X)        (ignore all 6s, move only by X from cell 1)
            # 4+×6 + X:  0→1→(sum of all numbers)  (one big move)
            if combo == 1:
                targets = [1, 6, 6 + rolled]
            elif combo == 2:
                targets = [1, 6, 6 + rolled]
            elif combo == 3:
                targets = [1, 1 + rolled]
            else:
                total = combo * 6 + rolled  # e.g. 6+6+6+6+4 = 28
                # 0 -> 1 (normal)
                final_cell_1, chain_1, _ = self._walk_n_steps(0, 1)
                last_no, m1 = self._create_moves_with_chain(
                    game=game,
                    start_move_no=move_no,
                    from_cell=0,
                    rolled=6,
                    final_cell=final_cell_1,
                    chain=chain_1,
                    on_hold=False,
                    at_start=True,
                )
                created_moves.extend(m1)
                move_no = last_no + 1

                # 1 -> 1+total (long move, NO RULES)
                final_cell_2, chain_2, hit_exit = self._walk_pure_no_rules(1, total)
                last_no, m2 = self._create_moves_with_chain(
                    game=game,
                    start_move_no=move_no,
                    from_cell=1,
                    rolled=int(total),  # show the sum in admin/telegram
                    final_cell=final_cell_2,
                    chain=chain_2,  # must be [] here
                    on_hold=False,
                    at_start=True,
                )
                created_moves.extend(m2)

                # mark the long move explicitly in DB so Admin shows it
                if m2:
                    type(m2[0]).objects.filter(pk=m2[0].pk).update(
                        event_type=self._et("LONG_MOVE"),
                        note=f"Довгий хід: {combo}×6 + {rolled} = {total}",
                    )

                prev = final_cell_2

            # короткие сегменты по targets
            prev = 0
            for tgt in targets:
                steps = int(tgt) - int(prev)
                final_cell, chain, hit_exit = self._walk_n_steps(prev, steps)
                last_no, mvs = self._create_moves_with_chain(
                    game=game,
                    start_move_no=move_no,
                    from_cell=prev,
                    rolled=int(rolled),  # same rolled value for this segment
                    final_cell=final_cell,
                    chain=chain,
                    on_hold=False,  # these are confirmed moves
                    at_start=True,
                )
                created_moves.extend(mvs)
                move_no = last_no + 1
                prev = final_cell

                if final_cell == self.EXIT_CELL or final_cell == self.FINISH_CELL or hit_exit:
                    self._persist_finished_record(game, moves=created_moves, reason="finish", player_id=player_id)
                    self._mark_finished_nonactive(game)
                    try:
                        summary = collect_game_summary(game)
                        client = OpenAIClient()
                        analysis = client.send_summary_json(summary)
                        sleep(3.0)
                    except Exception:
                        analysis = ""
                    return EntryStepResult(
                        status="finished",
                        message=self._finish_message(final_cell, analysis),
                        six_count=0,
                        moves=self._serialize_moves(created_moves, player_id=player_id),
                    )

            # persist end of combo
            game.current_cell = prev
            game.current_six_number = 0
            game.last_move_number = move_no - 1
            game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

            return EntryStepResult(
                status="single",
                message=f"Комбінація: {combo}×6 + {rolled} застосована.",
                six_count=0,
                moves=self._serialize_moves(created_moves, player_id=player_id),
            )

        # --- /START OF GAME --- (ниже — обычная логика, когда мы уже не в начальном состоянии)

        # A) старт: нужна 6 (если всё ещё at_start, но без серии)
        if at_start and not series_active:
            if rolled != 6:
                return EntryStepResult(
                    status="ignored",
                    message=self._wait_six_msg(rolled=rolled),
                    six_count=0,
                    moves=[],
                )

            move_no = self._next_move_number(game)
            final_cell, chain, hit_exit = self._walk_n_steps(0, 6)

            last_no, created_moves = self._create_moves_with_chain(
                game=game,
                start_move_no=move_no,
                from_cell=current_cell,  # 0
                rolled=6,
                final_cell=final_cell,
                chain=chain,
                on_hold=True,
                at_start=True,
            )

            game.current_cell = final_cell
            game.current_six_number = 1
            game.last_move_number = last_no
            game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

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

            last_no, created_moves = self._create_moves_with_chain(
                game=game,
                start_move_no=move_no,
                from_cell=current_cell,
                rolled=6,
                final_cell=final_cell,
                chain=chain,
                on_hold=True,
                at_start=at_start,
            )

            game.current_cell = final_cell
            game.current_six_number = six_count + 1
            game.last_move_number = last_no
            game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

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

            last_no, created_moves = self._create_moves_with_chain(
                game=game,
                start_move_no=move_no,
                from_cell=current_cell,
                rolled=6,
                final_cell=final_cell,
                chain=chain,
                on_hold=True,
                at_start=False,
            )

            # если нужно сохранить qa_sequence_in_combo=0 как раньше — проставим на первом созданном ходу
            if created_moves and hasattr(created_moves[0], "qa_sequence_in_combo"):
                type(created_moves[0]).objects.filter(pk=created_moves[0].pk).update(qa_sequence_in_combo=0)

            game.current_cell = final_cell
            game.current_six_number = 1
            game.last_move_number = last_no
            game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

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
            # Если буфер уже достиг 68 — завершаем немедленно
            qs_hold = Move.objects.select_for_update().filter(game=game, on_hold=True).order_by("move_number")
            if qs_hold.filter(to_cell=self.EXIT_CELL).exists():
                released_list = list(qs_hold)
                qs_hold.update(on_hold=False)

                # фиксируем позицию и финиш
                game.current_cell = self.EXIT_CELL
                game.current_six_number = 0
                if released_list:
                    game.last_move_number = released_list[-1].move_number
                    game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])
                else:
                    game.save(update_fields=["current_cell", "current_six_number"])

                self._persist_finished_record(game, moves=released_list, reason="exit_68", player_id=player_id)
                self._mark_finished_nonactive(game)
                try:
                    summary = collect_game_summary(game)
                    client = OpenAIClient()
                    analysis = client.send_summary_json(summary)
                    sleep(3.0)
                except Exception:
                    analysis = ""

                return EntryStepResult(
                    status="finished",
                    message=self._finish_message(game.current_cell, analysis),
                    six_count=0,
                    moves=self._serialize_moves(released_list, player_id=player_id),
                )

            # Комбо внутри игры: 3 шестерки → двигаемся только на X; 4+ → длинный ход без правил
            if (not at_start) and six_count >= 3:
                first_in_series = (
                    Move.objects.filter(game=game, on_hold=True).order_by("move_number").first()
                )
                start_cell = int(first_in_series.from_cell if first_in_series else current_cell)

                # сбрасываем буфер on_hold
                Move.objects.filter(game=game, on_hold=True).delete()

                if six_count == 3:
                    total_steps = int(rolled)
                    move_no = self._next_move_number(game)
                    final_cell, chain, hit_exit = self._walk_n_steps(start_cell, total_steps)
                    shown_roll = total_steps
                else:
                    total_steps = six_count * 6 + int(rolled)
                    move_no = self._next_move_number(game)
                    final_cell, chain, hit_exit = self._walk_pure_no_rules(start_cell, total_steps)
                    shown_roll = total_steps  # show the sum in admin/telegram

                # persist single combined move
                last_no, created_moves = self._create_moves_with_chain(
                    game=game,
                    start_move_no=move_no,
                    from_cell=start_cell,
                    rolled=int(shown_roll),
                    final_cell=final_cell,
                    chain=chain,
                    on_hold=False,
                    at_start=False,
                )

                # помечаем длинный ход
                if created_moves:
                    type(created_moves[0]).objects.filter(pk=created_moves[0].pk).update(
                        event_type=self._et("LONG_MOVE"),
                        note=f"Довгий хід: {six_count}×6 + {rolled} = {shown_roll}",
                    )

                game.current_cell = final_cell
                game.current_six_number = 0
                game.last_move_number = last_no
                game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

                if final_cell == self.EXIT_CELL or final_cell == self.FINISH_CELL or hit_exit:
                    self._persist_finished_record(game, moves=created_moves, reason="exit_68", player_id=player_id)
                    self._mark_finished_nonactive(game)
                    try:
                        summary = collect_game_summary(game)
                        client = OpenAIClient()
                        analysis = client.send_summary_json(summary)
                        sleep(3.0)
                    except Exception:
                        analysis = ""
                    return EntryStepResult(
                        status="finished",
                        message=self._finish_message(game.current_cell, analysis),
                        six_count=0,
                        moves=self._serialize_moves(created_moves, player_id=player_id),
                    )

                return EntryStepResult(
                    status="single",
                    message="Комбо з шістками застосовано.",
                    six_count=0,
                    moves=self._serialize_moves(created_moves, player_id=player_id),
                )

            # обычный финал серии: добавляем последний бросок X, освобождаем буфер on_hold
            move_no = self._next_move_number(game)
            final_cell, chain, hit_exit = self._walk_n_steps(current_cell, int(rolled))

            last_no, created_moves = self._create_moves_with_chain(
                game=game,
                start_move_no=move_no,
                from_cell=current_cell,
                rolled=int(rolled),
                final_cell=final_cell,
                chain=chain,
                on_hold=True,
                at_start=at_start,
            )

            game.current_cell = final_cell
            game.current_six_number = 0
            game.last_move_number = last_no
            game.save(update_fields=["current_cell", "current_six_number", "last_move_number"])

            qs = Move.objects.select_for_update().filter(game=game, on_hold=True).order_by("move_number")
            released_list = list(qs)
            qs.update(on_hold=False)

            if final_cell == self.EXIT_CELL or final_cell == self.FINISH_CELL or hit_exit:
                # снапшот завершающей серии
                reason = "exit_68" if final_cell == self.EXIT_CELL else "finish_72"
                self._persist_finished_record(game, moves=released_list, reason=reason, player_id=player_id)
                self._mark_finished_nonactive(game)
                try:
                    summary = collect_game_summary(game)
                    client = OpenAIClient()
                    analysis = client.send_summary_json(summary)
                    sleep(3.0)
                except Exception:
                    analysis = ""

                return EntryStepResult(
                    status="finished",
                    message=self._finish_message(game.current_cell, analysis),
                    six_count=0,
                    moves=self._serialize_moves(released_list, player_id=player_id),
                )

            return EntryStepResult(
                status="completed",
                message="Серія завершена. Віддаємо всі накопичені ходи.",
                six_count=0,
                moves=self._serialize_moves(released_list, player_id=player_id),
            )

        # E) одиночный ход (без серии)
        if (not series_active) and (rolled != 6):
            move_no = self._next_move_number(game)
            final_cell, chain, hit_exit = self._walk_n_steps(current_cell, int(rolled))

            last_no, created_moves = self._create_moves_with_chain(
                game=game,
                start_move_no=move_no,
                from_cell=current_cell,
                rolled=int(rolled),
                final_cell=final_cell,
                chain=chain,
                on_hold=False,
                at_start=False,
            )

            game.current_cell = final_cell
            game.last_move_number = last_no
            game.save(update_fields=["current_cell", "last_move_number"])

            if final_cell == self.EXIT_CELL or final_cell == self.FINISH_CELL or hit_exit:
                reason = "exit_68" if final_cell == self.EXIT_CELL else "finish_72"
                self._persist_finished_record(game, moves=created_moves, reason=reason, player_id=player_id)
                self._mark_finished_nonactive(game)
                try:
                    summary = collect_game_summary(game)
                    client = OpenAIClient()
                    analysis = client.send_summary_json(summary)
                    sleep(3.0)
                except Exception:
                    analysis = ""

                return EntryStepResult(
                    status="finished",
                    message=self._finish_message(game.current_cell, analysis),
                    six_count=0,
                    moves=self._serialize_moves(created_moves, player_id=player_id),
                )

            return EntryStepResult(
                status="single",
                message="Хід виконано.",
                six_count=0,
                moves=self._serialize_moves(created_moves, player_id=player_id),
            )

        # fallback
        return EntryStepResult(
            status="ignored",
            message="Стан не потребує дій.",
            six_count=int(getattr(game, "current_six_number", 0) or 0),
            moves=[],
        )

    # --- event helpers ---
    def _et(self, name: str):
        """Безопасно вернуть константу из Move.EventType, иначе — строку."""
        ET = getattr(Move, "EventType", None)
        return getattr(ET, name, name) if ET else name

    def _event_from_chain(self, chain: list[list[int]] | list[tuple[int, int]] | None):
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

    def _rules_payload(self, chain: list[list[int]] | list[tuple[int, int]] | None):
        """Сериализация применённых правил в state_snapshot.applied_rules."""
        if not chain:
            return []
        out = []
        for a, b in chain:
            a = int(a)
            b = int(b)
            out.append({
                "from": a,
                "to": b,
                "type": "ladder" if b > a else ("snake" if b < a else "neutral"),
            })
        return out

    def _serialize_moves(self, moves: list[Move], player_id: Optional[int] = None) -> list[dict]:
        """Сериализация списка ходов."""
        return [self._serialize_move(mv, player_id=player_id) for mv in moves]

