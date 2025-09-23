from __future__ import annotations
from pathlib import Path
from django.conf import settings
from django.core import signing

def make_card_url(player_id: int, game_id, rel_path: str) -> str:
    """
    rel_path — путь от PROTECTED_MEDIA_ROOT, напр. "cards/8.jpg"
    Возвращает абсолютный URL вида: https://your.domain/card/<signed>/<rel_path>
    """
    payload = {"p": int(player_id), "g": str(game_id), "path": rel_path}
    signed = signing.dumps(payload, salt="cards")
    base = str(getattr(settings, "SITE_BASE_URL", "")).rstrip("/")
    return f"{base}/card/{signed}/{rel_path}"
