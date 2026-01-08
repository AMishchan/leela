from games.services.board import resolve_chain, get_cell_image_name
from games.services.images import normalize_image_relpath, image_url_from_board_name
from django.utils import timezone
import random
from django.db.models import Max
from games.models import Game, Move
from typing import List, Optional, Dict
from games.services.entry_step_result import EntryStepResult
from time import sleep
from games.services.game_summary import collect_game_summary
from games.services.openai_client import OpenAIClient

def wait_six_msg(rolled: int) -> str:
    # Messages shown while we wait for the very first 6

    """Pick a random 'waiting for first six' message."""
    msg = random.choice([
        "Try again! We need a 6.",
        "Not a six yet — roll again 🎲",
        "Close, but not 6. One more time!",
        "Almost there. Throw the dice again!",
        "No 6 this time. Keep rolling!",
        "Ще не шістка — кидаймо ще!",
        "Потрібна шістка для старту. Спробуйте знову.",
    ])

    return msg.replace("{rolled}", str(rolled))


def next_move_number(game: Game) -> int:
    last_no = getattr(game, "last_move_number", None)
    if last_no is None:
        agg = Move.objects.filter(game=game).aggregate(Max("move_number"))
        last_no = agg.get("move_number__max") or 0
    return int(last_no) + 1


def walk_n_steps(start_cell: int, steps: int):
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
        if pos == EntryStepResult.BOARD_MAX:
            pos_after_72, chain72 = resolve_full(pos)
            if chain72:
                total_chain.extend(chain72)
            pos = int(pos_after_72)

    # применяем правила на клетке остановки
    final_pos, end_chain = resolve_full(pos)
    if end_chain:
        total_chain.extend(end_chain)

    # Завершение — на 68 или 72
    if int(final_pos) == EntryStepResult.EXIT_CELL or int(final_pos) == EntryStepResult.FINISH_CELL:
        hit_exit = True

    return int(final_pos), total_chain, hit_exit


def create_moves_with_chain(
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
                event_type=EntryStepResult.EVENT_NORMAL,
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
                event_type=et("LADDER")
                if b > a
                else et("SNAKE")
                if b < a
                else EntryStepResult.EVENT_NORMAL,
                note=f"auto rule: {a}->{b}",
                state_snapshot={"applied_rules": rules_payload([[a, b]])},
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
                event_type=EntryStepResult.EVENT_NORMAL,
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
                event_type=EntryStepResult.EVENT_NORMAL,
                note="noop",
                state_snapshot={"applied_rules": []},
                image_url=img_rel_final,
                on_hold=on_hold,
            )
        )
        move_no += 1

    return move_no - 1, created

    # Ход без правил (обрезаем по BOARD_MAX, exit-флаг и для 68, и для 72)


def walk_pure_no_rules(start_cell: int, steps: int):
    final_pos = int(start_cell) + int(steps)
    if final_pos > EntryStepResult.BOARD_MAX:
        final_pos = EntryStepResult.BOARD_MAX
    hit_exit = (final_pos == EntryStepResult.EXIT_CELL or final_pos == EntryStepResult.FINISH_CELL)
    return final_pos, [], hit_exit


# --- event helpers ---
def et(name: str):
    """Безопасно вернуть константу из Move.EventType, иначе — строку."""
    et = getattr(Move, "EventType", None)
    return getattr(et, name, name) if et else name


def persist_finished_record(game: Game, *, moves: list[Move], reason: str,
                            player_id: Optional[int] = None) -> None:
    """
    Пишем факт завершения партии в БД.
    1) Если есть модель CompletedGame — создаём запись там (best effort).
    2) Иначе положим снапшот в JSON-поле игры, если найдём подходящее.
    3) Дополнительно проставим finished_at / finished_reason, если такие поля у Game существуют.
    """
    payload = build_finish_payload(game, moves, reason=reason, player_id=player_id)

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

# ——— завершение игры (единый хелпер) ———
def mark_finished_nonactive(game: Game):
    game.current_six_number = 0
    game.status = getattr(Game.Status, "FINISHED", "finished")
    if hasattr(game, "is_active"):
        game.is_active = False
        game.save(update_fields=["current_six_number", "status", "is_active"])
    else:
        game.save(update_fields=["current_six_number", "status"])


def serialize_moves(moves: list[Move], player_id: Optional[int] = None) -> list[dict]:
    """Сериализация списка ходов."""
    return [serialize_move(mv, player_id=player_id) for mv in moves]


# Сообщение о финише (без рекурсии и переменных вне области видимости)
def finish_message(cell: int, analysis: str = "") -> str:
    if int(cell) == EntryStepResult.EXIT_CELL:
        base = "Вихід через 68. Гра завершена."
    else:
        base = "Гра завершена."
    return (f"{base} {analysis}").strip()


def resolve_full(cell: int):
    """
    1) resolve_chain (базовые змеи/стрелы),
    2) alt: snake_to/ladder_to (и синонимы),
    цикл до стабилизации (max 10 итераций).
    Возврат: (final_cell, chain_pairs)
    """
    pos = int(cell)
    applied: List[List[int]] = []
    alt_map = get_alt_map()

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


def serialize_move(mv: Move, player_id: Optional[int] = None) -> dict:
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


def get_alt_map() -> Dict[int, int]:
    """Строим {cell: to_cell} по snake*_to/ladder*_to (и синонимам) из boards.json, кэшируем."""
    if EntryStepResult.ALT_MAP is not None:
        return EntryStepResult.ALT_MAP

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
            for i in range(1, EntryStepResult.BOARD_MAX + 1):
                try:
                    meta = getter(i) or {}
                    to = extract_alt_to(meta)
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
                            to = extract_alt_to(v or {})
                            if to is not None:
                                mapping[cell] = int(to)
                        except Exception:
                            continue
                elif isinstance(raw, list):
                    for idx, v in enumerate(raw):
                        cell = idx + 1  # 1-базная нумерация
                        try:
                            to = extract_alt_to(v or {})
                            if to is not None:
                                mapping[cell] = int(to)
                        except Exception:
                            continue
                if mapping:
                    break
    except Exception:
        mapping = {}

    EntryStepResultALT_MAP = mapping
    return mapping


def rules_payload(chain: list[list[int]] | list[tuple[int, int]] | None):
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


def build_finish_payload(game: Game, moves: list[Move], *, reason: str, player_id: Optional[int]) -> dict:
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


def extract_alt_to(meta: dict) -> Optional[int]:
    if not isinstance(meta, dict):
        return None
    # Приоритет: snake > ladder
    for snake_key, ladder_key in EntryStepResult.ALT_KEYS_PRIORITY:
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


def finish_game_and_release(game: Game, player_id: Optional[int] = None) -> EntryStepResult:
    qs = Move.objects.select_for_update().filter(game=game, on_hold=True).order_by("move_number")
    released_list = list(qs)
    qs.update(on_hold=False)

    # Запишем завершение в БД (с полным списком финальных ходов)
    reason = "exit_68" if int(game.current_cell) == EntryStepResult.EXIT_CELL else "finish_72"
    persist_finished_record(game, moves=released_list, reason=reason, player_id=player_id)

    mark_finished_nonactive(game)
    try:
        summary = collect_game_summary(game)
        client = OpenAIClient()
        analysis = client.send_summary_json(summary)
        sleep(3.0)
    except Exception:
        analysis = ""

    return EntryStepResult(
        status="finished",
        message=finish_message(game.current_cell, analysis),
        six_count=0,
        moves=serialize_moves(released_list, player_id=player_id),
    )


def six_continue_text(six_count: int) -> str:
    # синоним на русский вариант (чтобы не падало, если где-то зовётся по старому имени)
    sleep(3.0)
    return six_continue_text_ru(six_count)


def six_continue_text_ru(six_count: int) -> str:
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
