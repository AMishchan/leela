from typing import Optional, Tuple
from .models import GameSettings


def get_payment_config() -> Tuple[Optional[str], Optional[str]]:
    """
    Возвращает (payment_url, payment_message) из GameSettings.
    Если настроек нет или ссылка пустая — (None, None).
    """
    cfg = GameSettings.objects.first()
    if not cfg or not cfg.payment_url:
        return None, None
    return cfg.payment_url, (cfg.payment_message or "")
