import mimetypes
from pathlib import Path
from datetime import timedelta, datetime, timezone

from django.conf import settings
from django.core import signing
from django.http import Http404, HttpResponse
from django.utils.http import http_date

CARD_LINK_TTL_SECONDS = 15 * 60  # 15 минут

def _safe_join(root: Path, rel_path: str) -> Path:
    p = (root / rel_path).resolve()
    if not str(p).startswith(str(root.resolve())):
        raise Http404("not found")
    return p

def card_image(request, signed: str, rel_path: str):
    """Защищённая выдача файла через X-Accel-Redirect."""
    try:
        data = signing.loads(signed, salt="cards", max_age=CARD_LINK_TTL_SECONDS)
    except signing.SignatureExpired:
        raise Http404("expired")
    except signing.BadSignature:
        raise Http404("bad sig")

    if data.get("path") != rel_path:
        raise Http404("mismatch")

    root = Path(getattr(settings, "PROTECTED_MEDIA_ROOT"))
    disk_path = _safe_join(root, rel_path)
    if not disk_path.exists() or not disk_path.is_file():
        raise Http404("no file")

    # Отдаём через Nginx internal location
    resp = HttpResponse()
    resp["X-Accel-Redirect"] = "/_secure_media/" + rel_path  # см. конфиг nginx
    ctype, _ = mimetypes.guess_type(str(disk_path))
    if ctype:
        resp["Content-Type"] = ctype
    expires = datetime.now(timezone.utc) + timedelta(seconds=CARD_LINK_TTL_SECONDS)
    resp["Cache-Control"] = "private, max-age=%d" % CARD_LINK_TTL_SECONDS
    resp["Expires"] = http_date(expires.timestamp())
    return resp

    # Если нет Nginx, можно временно так (НЕ забудь убрать X-Accel-Redirect выше):
    # from django.http import FileResponse
    # return FileResponse(open(disk_path, "rb"), content_type=ctype or "application/octet-stream")
