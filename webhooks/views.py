import json, time, os
from pathlib import Path
from django.http import JsonResponse, HttpResponseNotAllowed
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings

# Where to dump webhook payloads
DUMP_DIR = Path(getattr(settings, "WEBHOOK_DUMP_DIR",
                        Path(settings.BASE_DIR) / "var" / "webhooks"))
DUMP_DIR.mkdir(parents=True, exist_ok=True)

def _dump(filename_stem: str, data: dict, raw: bytes, headers: dict):
    # Save pretty JSON with some metadata, and raw as well (optional)
    ts = int(time.time())
    base = f"{filename_stem}_{ts}"
    json_path = DUMP_DIR / f"{base}.json"
    raw_path  = DUMP_DIR / f"{base}.raw"

    payload = {
        "received_at": ts,
        "headers": headers,
        "data": data,
    }
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # raw body (useful if JSON fails or to debug signatures in future)
    with raw_path.open("wb") as f:
        f.write(raw)

@csrf_exempt
def telegram_dice_webhook(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    # capture raw first
    raw_body = request.body or b""
    heads = {k: v for k, v in request.headers.items()}

    # try parse JSON; if it fails, still dump raw and return ok
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except Exception:
        data = {"_parse_error": True}

    # filename stem: try to include update_id / message_id if present
    stem = "tg"
    try:
        if "update_id" in data:
            stem = f"tg_{data['update_id']}"
        elif "message" in data and "message_id" in data["message"]:
            stem = f"tgmsg_{data['message']['message_id']}"
    except Exception:
        pass

    _dump(stem, data, raw_body, heads)

    # Optional: pull out dice result if present (for quick server log/response)
    dice_value = None
    try:
        if "message" in data and "dice" in data["message"]:
            dice_value = data["message"]["dice"].get("value")
    except Exception:
        pass

    return JsonResponse({"ok": True, "captured": True, "dice_value": dice_value})
