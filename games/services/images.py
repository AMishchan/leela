from __future__ import annotations
from pathlib import Path
from typing import Optional
from django.conf import settings
from urllib.parse import urlparse, quote

# опционально: если делаешь подписанные ссылки через securemedia
try:
    from games.services.secure_links import make_card_url  # def make_card_url(player_id, game_id, rel_path) -> str
    _HAS_SECURE = True
except Exception:
    make_card_url = None  # type: ignore
    _HAS_SECURE = False

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

    # относительный путь/имя
    if _HAS_SECURE and player_id and game_id:
        # считаем, что name — относительный путь от PROTECTED_MEDIA_ROOT
        # если это просто имя файла, поместим его в подкаталог PROTECTED_CARDS_DIR (если задан)
        root = Path(getattr(settings, "PROTECTED_MEDIA_ROOT", ""))
        cards_dir = Path(getattr(settings, "PROTECTED_CARDS_DIR", root / "cards"))
        rel_path = name if "/" in name else f"{Path(cards_dir).name}/{name}"
        # генерим подписанную ссылку
        return make_card_url(player_id=int(player_id), game_id=game_id, rel_path=rel_path)

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

def build_abs_image_url(image_rel: Optional[str]) -> Optional[str]:
    if not image_rel:
        return None

    s = str(image_rel).strip()
    if not s:
        return None

    # 1) Уже абсолютный URL → отдать как есть
    if s.startswith("http://") or s.startswith("https://"):
        return s

    # 2) Нормализуем относительный путь и процитируем (пробелы, кириллица и т.п.)
    rel = s.lstrip("/")
    rel = quote(rel, safe="/:@")  # не кодируем разделители/подписи

    base = str(getattr(settings, "BOARD_CELL_IMAGE_URL", "")).rstrip("/")
    site = str(getattr(settings, "SITE_BASE_URL", "")).rstrip("/")

    # base может быть абсолютным CDN-ом
    if base.startswith("http://") or base.startswith("https://"):
        return f"{base}/{rel}"

    # base как путь '/media/board_images' → нужен домен
    if base.startswith("/"):
        return f"{site}{base}/{rel}" if site else None

    # на крайний случай (не рекомендую держать относительный base)
    return f"{site}/{base}/{rel}".rstrip("/") if site and base else (f"{site}/{rel}" if site else None)