from __future__ import annotations
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, quote
from urllib.parse import urljoin
from django.conf import settings

def image_url_from_board_name(
    image_name: Optional[str],
    *,
    player_id: Optional[int] = None,
    game_id: Optional[int | str] = None,
) -> Optional[str]:
    """
    Собирает URL для картинки по значению из board.json.
    Правила:
      - абсолютные http(s) ссылки возвращаем как есть;
      - строки, начинающиеся с '/', дополняем SITE_BASE_URL (для Telegram);
      - относительные пути 'cards/8.jpg' — если настроен secure, подписываем и считаем,
        что путь относительно PROTECTED_MEDIA_ROOT; иначе отдаём как публичный URL:
        BOARD_CELL_IMAGE_URL + '/' + image_name;
      - просто имя '8.jpg' — как выше, только без подкаталога.
    """
    if not image_name:
        return None

    name = image_name.strip()

    # абсолютный URL
    if name.startswith("http://") or name.startswith("https://"):
        return name

    # абсолютный путь на сервере (для TG нужно дополнить доменом)
    if name.startswith("/"):
        site = str(getattr(settings, "SITE_BASE_URL", "")).rstrip("/")
        return f"{site}{name}" if site else name


    # публичная раздача (static/media): склеим с BOARD_CELL_IMAGE_URL
    base = str(getattr(settings, "BOARD_CELL_IMAGE_URL", "")).rstrip("/")
    if not base:
        return None
    return f"{base}/{name}"

def normalize_image_relpath(image_name: Optional[str]) -> str:
    """
    Принимает значение из board.json (может быть '8.jpg', 'cards/8.jpg', '/media/board/8.jpg', или полный https://...).
    Возвращает ОТНОСИТЕЛЬНЫЙ путь без домена и без базового префикса, т.е. то, что будем хранить в Move.image_url.
    """
    if not image_name:
        return ""
    name = str(image_name).strip()
    if not name:
        return ""

    # Абсолютный URL → оставляем только path
    if name.startswith("http://") or name.startswith("https://"):
        path = urlparse(name).path  # '/media/board/8.jpg'
        rel = path.lstrip("/")
    else:
        # Просто путь/имя
        rel = name.lstrip("/")

    # Уберём базовый префикс, если он задан (например, '/media/board_images')
    base = str(getattr(settings, "BOARD_CELL_IMAGE_URL", "")).strip()
    if base:
        base_path = urlparse(base).path if base.startswith("http") else base
        base_path = base_path.strip().lstrip("/")
        if base_path and rel.startswith(base_path + "/"):
            rel = rel[len(base_path) + 1 :]

    return rel



def build_abs_image_url(rel_path: str | None) -> str | None:
    if not rel_path:
        return None
    rel = rel_path.lstrip('/')  # media/board_images/41-...
    return urljoin(settings.SITE_BASE_URL.rstrip('/') + '/', 'cards/' + rel)
