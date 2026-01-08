from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from games.models import Game, Move

@dataclass
class EntryStepResult:
    status: str  # "ignored" | "continue" | "completed" | "single" | "finished"
    message: str
    six_count: int
    moves: List[Dict[str, Any]] = field(default_factory=list)

    EXIT_CELL = 68
    BOARD_MAX = 72
    FINISH_CELL = 72  # явная финишная клетка
    EVENT_NORMAL = getattr(getattr(Move, "EventType", object), "NORMAL", "NORMAL")

    # ленивый кэш для alt-правил
    ALT_MAP: Optional[Dict[int, int]] = None

    # --- поддержка разных ключей в boards.json ---
    ALT_KEYS_PRIORITY = (
        ("snake_to", "ladder_to"),
        ("snake2", "ladder2"),
        ("snake", "ladder"),
        ("snakeTo", "ladderTo"),
    )

    @property
    def final_cell(self) -> Optional[int]:
        if not self.moves:
            return None
        try:
            return int(self.moves[-1].get("to_cell"))
        except Exception:
            return None
