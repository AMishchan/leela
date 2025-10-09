from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Iterable


@dataclass
class MoveRecord:
    move_number: int
    from_cell: Optional[int]
    to_cell: Optional[int]
    dice_value: Optional[int]
    hit_ladder: Optional[bool]
    hit_snake: Optional[bool]
    question: Optional[str]
    answer: Optional[str]
    asked_at: Optional[str]
    answered_at: Optional[str]


def _coerce_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    # common fallbacks like "ladder", "snake", "none", "L", "S", 0/1, etc.
    s = str(value).strip().lower()
    if s in {"1", "true", "t", "yes", "y", "ladder", "l"}:
        return True
    if s in {"0", "false", "f", "no", "n", "snake", "s"}:
        return False
    return None


def _infer_snake_ladder(
    mv
) -> (Optional[bool], Optional[bool]):
    """
    Try to detect whether this move involved a ladder or a snake.
    We look for common fields first; if not present, we fall back to a
    simple heuristic based on from_cell/to_cell.
    """
    # 1) Explicit flags if your model has them
    for ladder_attr in ("hit_ladder", "was_ladder", "is_ladder"):
        if hasattr(mv, ladder_attr):
            hit_ladder = _coerce_bool(getattr(mv, ladder_attr))
            break
    else:
        hit_ladder = None

    for snake_attr in ("hit_snake", "was_snake", "is_snake"):
        if hasattr(mv, snake_attr):
            hit_snake = _coerce_bool(getattr(mv, snake_attr))
            break
    else:
        hit_snake = None

    # 2) Heuristic if unknown: compare delta ignoring dice
    # (If you store dice_value, we could refine this further later.)
    from_cell = getattr(mv, "from_cell", None) or getattr(mv, "start_cell", None)
    to_cell = getattr(mv, "to_cell", None) or getattr(mv, "end_cell", None)

    if hit_ladder is None and hit_snake is None and from_cell is not None and to_cell is not None:
        if to_cell > from_cell:
            hit_ladder = True
            hit_snake = False
        elif to_cell < from_cell:
            hit_ladder = False
            hit_snake = True

    return hit_ladder, hit_snake


def collect_game_summary(
    game,
    moves: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """
    Build a serializable summary for a finished game:
    - all moves with ladder/snake flags
    - all user answers / prompts timing
    - basic game meta
    """
    if moves is None:
        # Avoid circular imports; import locally
        from games.models import Move
        moves = (
            Move.objects
            .filter(game=game)
            .order_by("move_number")
            .all()
        )

    move_records: List[MoveRecord] = []
    ladders = snakes = 0

    for mv in moves:
        from_cell = getattr(mv, "from_cell", None) or getattr(mv, "start_cell", None)
        to_cell = getattr(mv, "to_cell", None) or getattr(mv, "end_cell", None)
        dice_value = getattr(mv, "dice_value", None)

        # Questions/answers (use your field names)
        question = getattr(mv, "question_text", None) or getattr(mv, "question", None)
        answer = getattr(mv, "player_answer", None)
        asked_at = getattr(mv, "question_sent_at", None) or getattr(mv, "asked_at", None)
        answered_at = getattr(mv, "player_answer_at", None)

        hit_ladder, hit_snake = _infer_snake_ladder(mv)
        if hit_ladder:
            ladders += 1
        if hit_snake:
            snakes += 1

        move_records.append(
            MoveRecord(
                move_number=getattr(mv, "move_number", 0),
                from_cell=from_cell,
                to_cell=to_cell,
                dice_value=dice_value,
                hit_ladder=hit_ladder,
                hit_snake=hit_snake,
                question=question,
                answer=answer,
                asked_at=asked_at.isoformat() if asked_at else None,
                answered_at=answered_at.isoformat() if answered_at else None,
            )
        )

    data = {
        "game_id": getattr(game, "id", None),
        "player_id": getattr(game, "player_id", None) or getattr(game, "player_id_id", None),
        "started_at": getattr(game, "created_at", None) and game.created_at.isoformat(),
        "finished_at": getattr(game, "finished_at", None) and game.finished_at.isoformat(),
        "total_moves": len(move_records),
        "total_ladders": ladders,
        "total_snakes": snakes,
        "moves": [asdict(m) for m in move_records],
    }
    return data


def render_summary_prompt(summary: Dict[str, Any]) -> str:
    """
    Optional helper that turns the summary dict into a concise text prompt.
    Use this if you prefer sending text instead of JSON to the model.
    """
    lines = []
    lines.append(f"Game #{summary.get('game_id')}, Player: {summary.get('player_id')}")
    lines.append(
        f"Started: {summary.get('started_at')} | Finished: {summary.get('finished_at')}"
    )
    lines.append(
        f"Total moves: {summary.get('total_moves')} | Ladders: {summary.get('total_ladders')} | Snakes: {summary.get('total_snakes')}"
    )
    lines.append("")
    lines.append("Moves:")
    for m in summary["moves"]:
        tag = "ladder" if m["hit_ladder"] else ("snake" if m["hit_snake"] else "normal")
        q = (m["question"] or "").strip()
        a = (m["answer"] or "").strip()
        lines.append(
            f"- #{m['move_number']}: {m['from_cell']} → {m['to_cell']} (dice={m['dice_value']}, {tag})"
        )
        if q:
            lines.append(f"  Q: {q}")
        if a:
            lines.append(f"  A: {a}")
    return "\n".join(lines)
