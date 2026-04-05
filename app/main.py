# /opt/dashboard-gen/app/main.py
from fastapi import FastAPI, BackgroundTasks, HTTPException
from PIL import Image, ImageDraw, ImageFont
from config import (
    OUT_PATH,
    BIN_BYTES_EXPECTED,
    HA_URL,
    HA_TOKEN,
    WEATHER_ENTITY,
    CALENDAR_ENTITY,
    TODO_ENTITY,
    OLLAMA_URL,
    OLLAMA_MODEL,
    COMFY_INPUT_IMAGE,
    COMFY_URL,
    COMFY_WORKFLOW_FILE,
    EPD_PUSH_ENABLED,
    EPD_PUSH_URL,
    EPD_PUSH_FAIL_HARD,
    EPD_ROTATE_CW,
)
from clothing import choose_clothing_profile

from prompts import (
    BASE_IMAGE_PROMPT,
    build_quote_system_prompt,
    build_quote_user_prompt,
    build_todo_system_prompt,
    build_todo_user_prompt,
    build_overlay_system_prompt,
    build_overlay_user_prompt,
    build_season_system_prompt,
    build_season_user_prompt,
    build_final_prompt,
)

from datetime import date, datetime, timedelta
import threading
import os
import re
import json
import time
import requests
import subprocess
import io
import hashlib

app = FastAPI()

JOB_LOCK = threading.Lock()
JOB_RUNNING = False
LAST_JOB = {"status": "idle", "started": None, "finished": None, "error": None}

def load_font(size: int):
    # Use an explicit path so Pillow never falls back silently
    font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        return ImageFont.truetype(font_path, size)
    except Exception as e:
        print(f"FONT LOAD FAILED size={size} path={font_path}: {e}", flush=True)
        return ImageFont.load_default()

def ha_get_state(entity_id: str) -> dict:
    if not HA_TOKEN:
        return {"state": "unknown", "attributes": {"friendly_name": entity_id, "note": "HA_TOKEN missing"}}
    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=6)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"state": "unknown", "attributes": {"friendly_name": entity_id, "error": str(e)}}

def ha_call_service(domain: str, service: str, data: dict | None = None, target: dict | None = None, return_response: bool = False):
    if not HA_TOKEN:
        raise RuntimeError("HA_TOKEN missing")

    base = f"{HA_URL}/api/services/{domain}/{service}"
    url = base + ("?return_response" if return_response else "")
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}

    payload = {}
    if data:
        payload.update(data)
    if target:
        payload["target"] = target

    r = requests.post(url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

def ha_calendar_get_events(entity_id: str, hours_ahead: int = 36):
    now = datetime.now().astimezone()
    end = now + timedelta(hours=hours_ahead)

    data = {
        "start_date_time": now.isoformat(timespec="seconds"),
        "end_date_time": end.isoformat(timespec="seconds"),
        "entity_id": entity_id,
    }

    return ha_call_service("calendar", "get_events", data=data, return_response=True)

def ha_calendar_get_events_range(entity_id: str, start_dt: datetime, end_dt: datetime):
    data = {
        "start_date_time": start_dt.astimezone().isoformat(timespec="seconds"),
        "end_date_time": end_dt.astimezone().isoformat(timespec="seconds"),
        "entity_id": entity_id,
    }

    return ha_call_service("calendar", "get_events", data=data, return_response=True)

def extract_clothing_weather_inputs(state_obj: dict) -> tuple[float, str, str, bool]:
    attr = state_obj.get("attributes", {}) or {}
    state = (state_obj.get("state") or "").lower().strip()

    temp_raw = _first_present(attr, ["temperature", "temp"])
    try:
        temp_c = float(temp_raw)
    except Exception:
        temp_c = 10.0

    precip = "none"
    intensity = "none"
    icy = False

    if any(w in state for w in ["thunderstorm", "pouring", "storm"]):
        precip = "rain"
        intensity = "heavy"
    elif any(w in state for w in ["rainy", "rain", "showers"]):
        precip = "rain"
        intensity = "moderate"
    elif any(w in state for w in ["drizzle"]):
        precip = "rain"
        intensity = "light"
    elif any(w in state for w in ["snowy", "snow", "flurr", "blizzard"]):
        precip = "snow"
        intensity = "moderate"
    elif any(w in state for w in ["sleet", "freezing rain", "hail"]):
        precip = "mixed"
        intensity = "moderate"

    if temp_c <= 0 or "ice" in state or "freezing" in state or precip in {"snow", "mixed"}:
        icy = True

    return temp_c, precip, intensity, icy

TODO_AI_CACHE = {
    "hash": None,
    "lines": [],          # up to 6 lines to draw (already bullet-prefixed or not—your choice)
    "summary_line": "",   # the "+N more — ..." line
    #"quote_idx": None,
    "day": None,
    "ts": 0,
}

QUOTE_LOCK = threading.Lock()
QUOTE_CACHE = {
    "day": None,
    "quote": "",
    "recent": [],   # keep last few quotes
}

TODO_AI_LOCK = threading.Lock()

def _norm_ws(s: str) -> str:
    return " ".join((s or "").replace("\n", " ").split()).strip()

def clamp_to_width(draw, text: str, font, max_w: int) -> str:
    text = (text or "").rstrip()
    if draw.textlength(text, font=font) <= max_w:
        return text
    # Hard truncate until it fits
    s = text
    while s and draw.textlength(s + "…", font=font) > max_w:
        s = s[:-1]
    return (s.rstrip() + "…") if s else ""

def is_effectively_blank_line(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return True

    # treat placeholder-only lines as blank
    stripped = s.replace("•", "").replace("-", "").replace("—", "").replace("_", "").replace(".", "").strip()
    return stripped == ""

def _stable_todo_hash(items: list[dict]) -> str:
    """
    Hash only fields that should affect formatting/display.
    Ignore uid order instability by sorting.
    """
    rows = []
    for it in items:
        status = (it.get("status") or "").lower().strip()
        if status == "completed":
            continue
        summary = _norm_ws(it.get("summary") or it.get("item") or "")
        due = _norm_ws(it.get("due") or "")
        desc = _norm_ws(it.get("description") or "")
        rows.append({"summary": summary, "due": due, "description": desc, "status": status})
    rows.sort(key=lambda r: (r["due"], r["summary"], r["description"]))

    blob = json.dumps(rows, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

def quote_is_usable(s: str) -> bool:
    s = _norm_ws(s)
    if not s:
        return False

    # Reject non-ASCII / non-English-ish spill
    if any(ord(ch) > 127 for ch in s):
        return False

    low = s.lower()

    # Reject meta / apology / restart chatter
    bad_phrases = [
        "sorry",
        "apolog",
        "let me",
        "retry",
        "redo",
        "here's",
        "here is",
        "instead",
        "use this",
        "revised",
    ]
    if any(p in low for p in bad_phrases):
        return False

    # Too long / too short
    if len(s) < 20 or len(s) > 80:
        return False

    return True

def clean_quote_text(s: str) -> str:
    s = _norm_ws(s)
    # Strip non-ascii just in case
    s = "".join(ch for ch in s if ord(ch) < 128)
    return s[:80].rstrip()

def _todo_items_open_sorted(entity_id: str) -> list[dict]:
    items = ha_todo_get_items(entity_id)
    open_items = []
    for it in items:
        status = (it.get("status") or "").lower()
        if status == "completed":
            continue
        summary = _norm_ws(it.get("summary") or it.get("item") or "")
        if not summary:
            continue
        open_items.append(it)

    # sort: all-day first (no due time), then due time
    def sort_key(it):
        due = (it.get("due") or "").strip()
        # due may be "", date only, or ISO datetime. We'll use your existing parser.
        day, tstr, all_day = _parse_event_start(due)  # works for ISO; for "" -> all_day
        # all_day first
        group = 0 if (tstr is None) else 1
        t = tstr or "00:00"
        return (group, t, _norm_ws(it.get("summary") or it.get("item") or "").lower())

    open_items.sort(key=sort_key)
    return open_items

def ollama_daily_quote() -> str:
    """
    Generate 1 quote per day via Ollama and cache it.
    Avoid repeating recent quotes.
    """
    today = date.today().isoformat()
    with QUOTE_LOCK:
        if QUOTE_CACHE["day"] == today and QUOTE_CACHE["quote"]:
            print(f"Using cached daily quote for {today}", flush=True) 
            return QUOTE_CACHE["quote"]
        recent = list(QUOTE_CACHE.get("recent") or [])

    system = build_quote_system_prompt(recent)
    user = build_quote_user_prompt(today)

    url = f"{OLLAMA_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"SYSTEM:\n{system}\n\nUSER:\n{user}\n",
        "stream": False,
        "options": {"temperature": 1.0, "top_p": 0.95, "num_predict": 80},
}

    fallback_quotes = [
        "A quiet day still counts as progress.",
        "Less chaos, more traction.",
        "No fires today. Enjoy the rare upgrade.",
        "Even a calm day is a win.",
        "Momentum rests too. Then it rolls again.",
        "Today’s task list: successfully have a day.",
    ]

    q = ""
    try:
        for _ in range(3):
            r = requests.post(url, json=payload, timeout=20)
            r.raise_for_status()
            raw = (r.json().get("response") or "").strip().splitlines()[0].strip()
            raw = clean_quote_text(raw)
            if quote_is_usable(raw):
                q = raw
                break
    except Exception:
        q = ""

    if not q:
        idx = abs(hash(today)) % len(fallback_quotes)
        q = fallback_quotes[idx]

    with QUOTE_LOCK:
        QUOTE_CACHE["day"] = today
        QUOTE_CACHE["quote"] = q

        updated_recent = list(QUOTE_CACHE.get("recent") or [])
        updated_recent.append(q)

        # keep only the last 10
        QUOTE_CACHE["recent"] = updated_recent[-10:]

    return q

def ollama_format_todo(items_open_sorted: list[dict]) -> dict:
    """
    Returns:
      {"lines": [...<=6], "summary_line": "...", "remaining": int}
    """
    # Prepare compact JSON for the model
    tasks = []
    for it in items_open_sorted:
        summary = _norm_ws(it.get("summary") or it.get("item") or "")
        due = _norm_ws(it.get("due") or "")
        desc = (it.get("description") or "").strip()
        desc = desc.replace("\r\n", "\n").replace("\r", "\n").strip()
        tasks.append({"summary": summary, "due": due, "description": desc})

    system = build_todo_system_prompt()
    user = build_todo_user_prompt(tasks)

    url = f"{OLLAMA_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"SYSTEM:\n{system}\n\nUSER:\n{user}\n",
        "stream": False,
        "options": {"temperature": 0.2, "top_p": 0.9, "num_predict": 220},
    }

    r = requests.post(url, json=payload, timeout=25)
    r.raise_for_status()
    out = (r.json().get("response") or "").strip()

    lines = [ln.rstrip() for ln in out.splitlines()]

# Normalize to exactly 7 lines
    if len(lines) < 7:
        lines = lines + [""] * (7 - len(lines))
    elif len(lines) > 7:
        lines = lines[:7]

    return {"lines": lines[:6], "summary_line": lines[6]}

def ha_todo_get_items(entity_id: str):
    """
    Returns list of items with at least: summary, uid/item_id, status.
    Different HA versions may name the id field differently; we handle both.
    """
    # Most HA installs require return_response for get_items
    data = {"entity_id": entity_id}
    resp = ha_call_service("todo", "get_items", data=data, return_response=True)

    # Common structure: {"service_response": {"todo.xxx": {"items":[...]}}}
    sr = (resp or {}).get("service_response") or {}
    obj = sr.get(entity_id) or {}
    items = obj.get("items") or []
    return items if isinstance(items, list) else []

def ha_todo_add_item(entity_id: str, summary: str, *, due_date: str | None = None, due_datetime: str | None = None, description: str | None = None):
    data = {"entity_id": entity_id, "item": summary}
    if description:
        data["description"] = description
    if due_datetime:
        data["due_datetime"] = due_datetime  # "YYYY-MM-DD HH:MM:SS"
    elif due_date:
        data["due_date"] = due_date          # "YYYY-MM-DD"
    ha_call_service("todo", "add_item", data=data, return_response=False)

def ha_todo_remove_item(entity_id: str, item_text: str):
    # remove_item expects the item SUMMARY TEXT, not an id
    data = {"entity_id": entity_id, "item": item_text}
    ha_call_service("todo", "remove_item", data=data, return_response=False)

def ha_todo_clear_list(entity_id: str):
    """
    Remove ALL items from the todo list (replace behavior).
    Uses remove_item(item=<text>) because your HA expects text, not item_id.
    """
    items = ha_todo_get_items(entity_id)
    removed = 0

    # Remove by summary text
    for it in items:
        summary = (it.get("summary") or it.get("item") or "").strip()
        if not summary:
            continue
        ha_todo_remove_item(entity_id, summary)
        removed += 1

    return removed

@app.get("/todo_debug")
def todo_debug():
    try:
        items = ha_todo_get_items(TODO_ENTITY)
        return {"ok": True, "todo_entity": TODO_ENTITY, "count": len(items), "items": items[:20]}
    except Exception as e:
        return {"ok": False, "todo_entity": TODO_ENTITY, "error": str(e)}

@app.get("/calendar_debug")
def calendar_debug():
    entity_id = CALENDAR_ENTITY
    try:
        resp = ha_calendar_get_events(entity_id, hours_ahead=36)
        return {"ok": True, "entity": entity_id, "raw": resp}
    except Exception as e:
        return {"ok": False, "entity": entity_id, "error": str(e)}

def calendar_events_today(entity_id: str):
    today = datetime.now().astimezone().date()
    today_dt = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt   = today_dt + timedelta(days=1)

    raw = ha_calendar_get_events_range(entity_id, today_dt, end_dt)
    sr = (raw or {}).get("service_response") or {}
    cal_obj = sr.get(entity_id) or {}
    events = cal_obj.get("events") or []
    if not isinstance(events, list):
        return []

    out = []
    for e in events:
        start_raw = e.get("start") or ""
        summary = (e.get("summary") or "(no title)").strip()
        desc = (e.get("description") or "").strip()
        loc  = (e.get("location") or "").strip()

        day, tstr, all_day = _parse_event_start(start_raw)
        if day != today:
            continue

        out.append({
            "start_raw": start_raw,
            "time": tstr,
            "summary": summary,
            "description": desc,
            "location": loc,
            "all_day": all_day,
        })

    def key(it):
        if it["time"] is None:
            return (0, "00:00", it["summary"].lower())  # all-day first
        return (1, it["time"], it["summary"].lower())  # then timed
    out.sort(key=key)

    return out

def parse_task_key_and_text(raw: str):
    raw = (raw or "").strip()
    if raw.startswith("A|"):
        return (0, "00:00", raw[2:].strip())
    if raw.startswith("T@") and "|" in raw:
        t = raw[2:7]
        txt = raw.split("|", 1)[1].strip()
        return (1, t, txt)
    return (2, "99:99", raw)

def build_tasks_from_events(events_today: list[dict]) -> list[dict]:
    out = []
    for e in events_today:
        summary = (e.get("summary") or "").strip()
        if not summary:
            continue

        desc = (e.get("description") or "").strip()
        loc  = (e.get("location") or "").strip()
        full_desc = (desc + ("\n\n" + loc if loc else "")).strip() or None

        # e["start_raw"] will be the original HA "start" string
        start_raw = e.get("start_raw") or ""
        day, tstr, all_day = _parse_event_start(start_raw)

        if all_day or not tstr:
            due_date = day.isoformat() if day else None
            out.append({"summary": summary, "due_date": due_date, "due_datetime": None, "description": full_desc})
        else:
            # Convert "start_raw" to HA "due_datetime" format (YYYY-MM-DD HH:MM:SS)
            dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00")).astimezone()
            due_dt = dt.strftime("%Y-%m-%d %H:%M:%S")
            out.append({"summary": summary, "due_date": None, "due_datetime": due_dt, "description": full_desc})

    return out or [{"summary": "Nothing planned", "due_date": datetime.now().astimezone().date().isoformat(), "due_datetime": None, "description": None}]

# -------- EPD BIN GENERATION (epdoptimize) --------
def generate_epaper_bin_epdoptimize(png_path: str, out_bin_path: str) -> int:
    """
    Uses epdoptimize JS pipeline to generate image_data.bin from wallpaper.png.
    Returns byte count of the output bin.
    """
    env = os.environ.copy()
    env["IN_PNG"] = png_path
    env["OUT_BIN"] = out_bin_path
    env["ROTATE_CW"] = env.get("ROTATE_CW", EPD_ROTATE_CW)
    env["WRITE_PREVIEW"] = "0"  # stop producing wallpaper_epd.png

    cmd = ["node", "/frame-tools/epdoptimize/render_epd_bin.js"]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)

    if r.returncode != 0:
        raise RuntimeError(
            f"epdoptimize failed rc={r.returncode}\n"
            f"STDOUT:\n{r.stdout}\n\nSTDERR:\n{r.stderr}"
        )

    st = os.stat(out_bin_path)
    if st.st_size != BIN_BYTES_EXPECTED:
        raise RuntimeError(f"bin size wrong: {st.st_size} bytes (expected {BIN_BYTES_EXPECTED})")
    return st.st_size

def looks_like_address_line(s: str) -> bool:
    s2 = (s or "").strip()
    if not s2:
        return False
    if not s2[0].isdigit():     # ✅ key fix: "Test1" will NOT match
        return False

    low = s2.lower()
    street_words = r"\b(st|street|rd|road|ave|avenue|blvd|drive|dr|lane|ln|line|way|court|ct|cres|crescent)\b"
    return ("," in s2) or (re.search(street_words, low) is not None)

def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        test = w if not cur else f"{cur} {w}"
        if draw.textlength(test, font=font) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines

def draw_wrapped_lines(draw: ImageDraw.ImageDraw, x: int, y: int, lines: list[str], font, fill, max_lines: int, line_gap: int = 8):
    line_h = font.size + line_gap
    for i, line in enumerate(lines[:max_lines]):
        draw.text((x, y + i * line_h), line, font=font, fill=fill)

def push_bin_to_epd(bin_path: str) -> None:
    """
    Push bin to ESP32 in TUNING mode (POST /bin). Optional; controlled via env.
    """
    if not EPD_PUSH_ENABLED:
        return
    if not EPD_PUSH_URL:
        raise RuntimeError("EPD_PUSH_ENABLED=1 but EPD_PUSH_URL is empty")

    with open(bin_path, "rb") as f:
        data = f.read()

    if len(data) != BIN_BYTES_EXPECTED:
        raise RuntimeError(f"Refusing to push: bin size {len(data)} != {BIN_BYTES_EXPECTED}")

    r = requests.post(
        EPD_PUSH_URL,
        data=data,
        headers={"Content-Type": "application/octet-stream"},
        timeout=30,
    )
    r.raise_for_status()
    # ESP32 replies "OK\n"
    if r.text.strip() != "OK":
        raise RuntimeError(f"EPD push returned unexpected body: {r.text!r}")

# -------- TIME / WEATHER HELPERS --------
def _parse_ha_iso(dt_str: str):
    """Parse HA ISO datetime like '2026-02-02T07:12:00-05:00' into aware datetime."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str)
    except Exception:
        return None

def infer_day_or_night_from_weather(state_obj: dict) -> str:
    """
    Returns "day" or "night".
    Prefers sunrise/sunset from weather attributes if present; falls back to local hour.
    """
    attr = state_obj.get("attributes", {}) or {}

    sunrise = _parse_ha_iso(attr.get("sunrise"))
    sunset  = _parse_ha_iso(attr.get("sunset"))
    now = datetime.now().astimezone()

    if sunrise and sunset:
        if sunrise.tzinfo is None:
            sunrise = sunrise.replace(tzinfo=now.tzinfo)
        if sunset.tzinfo is None:
            sunset = sunset.replace(tzinfo=now.tzinfo)
        return "day" if sunrise <= now <= sunset else "night"

    return "night" if (now.hour < 6 or now.hour >= 18) else "day"

def ha_try_get(entity_id: str) -> dict | None:
    if not HA_TOKEN:
        return None
    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    try:
        r = requests.get(url, headers=headers, timeout=6)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def _first_present(d: dict, keys: list):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None

def infer_season(state_weather: dict) -> str:
    """
    Returns: 'winter' | 'spring' | 'summer' | 'autumn'
    Prefers HA sensor.season if present; falls back to month + temperature.
    """
    s = ha_try_get("sensor.season")
    if s:
        val = (s.get("state") or "").lower().strip()
        if val in ("winter", "spring", "summer", "autumn", "fall"):
            return "autumn" if val == "fall" else val

    now = datetime.now().astimezone()
    m = now.month

    attr = state_weather.get("attributes", {}) or {}
    temp = _first_present(attr, ["temperature", "temp"])
    try:
        t = float(temp) if temp is not None else None
    except Exception:
        t = None

    if m in (12, 1, 2):
        base = "winter"
    elif m in (3, 4, 5):
        base = "spring"
    elif m in (6, 7, 8):
        base = "summer"
    else:
        base = "autumn"

    if t is not None and t <= -1 and m in (3, 4, 11):
        return "winter"

    return base

def draw_text_bold(draw, xy, text, font, fill, stroke_fill=None, stroke_width=2):
    # Pillow supports stroke_width in recent versions; this is the cleanest bold.
    try:
        draw.text(
            xy, text, font=font, fill=fill,
            stroke_width=stroke_width,
            stroke_fill=(stroke_fill if stroke_fill is not None else fill),
        )
    except TypeError:
        # Fallback: manual "fake bold" by drawing multiple offsets
        x, y = xy
        for dx, dy in [(0,0), (1,0), (0,1), (1,1)]:
            draw.text((x+dx, y+dy), text, font=font, fill=fill)

def _parse_event_start(s: str):
    """
    Returns (date_obj, time_str_or_None, is_all_day)
    Accepts:
      - YYYY-MM-DD (all-day)
      - ISO datetimes with Z/offset
      - Naive datetimes like "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DDTHH:MM:SS"
        (assumed local timezone)
    """
    if not s:
        return None, None, True

    s = s.strip()

    # all-day format
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            d = datetime.fromisoformat(s).date()
            return d, None, True
        except Exception:
            return None, None, True

    try:
        # Normalize Zulu
        s2 = s.replace("Z", "+00:00")

        dt = datetime.fromisoformat(s2)

        # If dt is naive, assume local timezone
        if dt.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo
            dt = dt.replace(tzinfo=local_tz)

        dt = dt.astimezone()
        return dt.date(), dt.strftime("%H:%M"), False
    except Exception:
        return None, None, True

def group_events_by_day(events: list[dict]) -> list[tuple]:
    """
    Input: list of HA event dicts with start/end/summary.
    Output: sorted list of (date_obj, [event_items]) where event_items are dicts:
      {"time": "HH:MM"|None, "summary": str, "all_day": bool}
    """
    buckets: dict = {}
    for e in events:
        summary = (e.get("summary") or "(no title)").strip()
        start = e.get("start") or ""
        day, tstr, all_day = _parse_event_start(start)
        if day is None:
            continue
        buckets.setdefault(day, []).append({"time": tstr, "summary": summary, "all_day": all_day})

    # sort events within each day: timed first, then all-day (can flip later if you want)
    for day, items in buckets.items():
        def key(it):
            if it["time"] is None:
                return (1, "99:99", it["summary"].lower())
            return (0, it["time"], it["summary"].lower())
        items.sort(key=key)

    days_sorted = sorted(buckets.keys())
    return [(d, buckets[d]) for d in days_sorted]

def push_existing_wallpaper_to_epd() -> dict:
    """
    Re-use the current /out/wallpaper.png, regenerate the EPD bin,
    and optionally push it to the display.
    Fast path for tuning epdoptimize without calling HA/Ollama/Comfy.
    """
    if not os.path.exists(OUT_PATH):
        raise FileNotFoundError(f"Wallpaper not found: {OUT_PATH}")

    bin_path = os.path.join(os.path.dirname(OUT_PATH), "image_data.bin")
    n = generate_epaper_bin_epdoptimize(OUT_PATH, bin_path)

    pushed = False
    if EPD_PUSH_ENABLED:
        push_bin_to_epd(bin_path)
        pushed = True

    return {
        "ok": True,
        "out": OUT_PATH,
        "bin": bin_path,
        "bin_bytes": n,
        "epd_push": "ok" if pushed else "disabled",
    }

def format_weather_for_display(state_obj: dict) -> str:
    state = state_obj.get("state", "unknown")
    attr = state_obj.get("attributes", {}) or {}
    name = attr.get("friendly_name", WEATHER_ENTITY)

    temp = _first_present(attr, ["temperature", "temp"])
    unit = _first_present(attr, ["temperature_unit", "unit_of_measurement"]) or "\u00b0C"
    humidity = _first_present(attr, ["humidity"])
    wind = _first_present(attr, ["wind_speed", "wind_speed_kmh", "wind"])
    wind_unit = _first_present(attr, ["wind_speed_unit"]) or "km/h"

    parts = [f"{name}: {state}"]
    if temp is not None:
        parts.append(f"{temp}{unit}")
    if humidity is not None:
        parts.append(f"{humidity}% humidity")
    if wind is not None:
        parts.append(f"wind {wind} {wind_unit}")
    if "error" in attr:
        parts.append("(weather fetch error)")
    return " - ".join(parts)

# -------- OLLAMA OVERLAY / SEASON --------
_OVERLAY_RE = re.compile(
    r"^WEATHER\s*OVERLAY:\nSky:\s+.+\nAir:\s+.+\nGround:\s+.+\nLanternLight:\s+.+\s*$",
    re.MULTILINE
)

def overlay_is_valid(text: str, tod: str, weather_line: str = "") -> bool:
    if not text:
        return False

    t = text.strip()
    if not _OVERLAY_RE.match(t):
        return False

    low = t.lower()
    weather_low = (weather_line or "").lower()

    forbidden_always = ["indoors", "umbrella", "mask", "phone", "helmet"]
    forbidden_day = ["moon", "moonlight", "starlight", "stars"]
    forbidden_night = ["sun", "sunlight", "daylight"]

    if any(w in low for w in forbidden_always):
        return False
    if tod == "day" and any(w in low for w in forbidden_day):
        return False
    if tod == "night" and any(w in low for w in forbidden_night):
        return False

    rain_now = any(w in weather_low for w in [
        "rain", "rainy", "pouring", "shower", "showers", "drizzle", "storm", "thunderstorm"
    ])
    snow_now = any(w in weather_low for w in [
        "snow", "snowy", "flurr", "blizzard", "sleet", "hail"
    ])
    fog_now = any(w in weather_low for w in [
        "fog", "foggy", "mist", "misty", "haze", "hazy"
    ])
    clear_now = any(w in weather_low for w in [
        "clear", "sunny", "fair"
    ])

    rain_terms = ["rain", "drizzle", "showers", "raindrops", "rain streaks", "falling rain"]
    snow_terms = ["snow", "flurries", "falling snow", "snowflakes", "blowing snow"]
    fog_terms = ["fog", "mist", "haze", "low visibility"]
    clear_terms = ["clear", "dry air", "bright", "sunlit", "crisp air"]

    if rain_now and not any(term in low for term in rain_terms):
        return False
    if snow_now and not any(term in low for term in snow_terms):
        return False
    if fog_now and not any(term in low for term in fog_terms):
        return False

    # Optional: stop the model from inventing rain during clearly dry weather
    if clear_now and any(term in low for term in rain_terms + snow_terms):
        return False

    return True

def ollama_generate_overlay(weather_line: str, tod: str, retries: int = 2) -> str:

    system = build_overlay_system_prompt(tod)
    user = build_overlay_user_prompt(weather_line)

    url = f"{OLLAMA_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"SYSTEM:\n{system}\n\nUSER:\n{user}\n",
        "stream": False,
        "options": {"temperature": 0.3, "top_p": 0.9, "num_predict": 200},
    }

    last = ""
    for _ in range(retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=25)
            r.raise_for_status()
            txt = (r.json().get("response") or "").strip()
            last = txt
            if overlay_is_valid(txt, tod, weather_line):
                return txt
        except Exception as e:
            last = (
                "WEATHER OVERLAY:\n"
                "Sky: overlay error while contacting ollama service\n"
                f"Air: {str(e)[:80]}\n"
                "Ground: check ollama connectivity and model availability\n"
                "LanternLight: check ollama logs for generation failures"
            )
    return last.strip()

_SEASON_RE = re.compile(
    r"^SEASONAL\s*ENVIRONMENT:\nTrees:\s+.+\nGround:\s+.+\nAir:\s+.+\nDistantDetails:\s+.+\s*$",
    re.MULTILINE
)

def season_is_valid(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if not _SEASON_RE.match(t):
        return False

    forbidden = [
        "falling snow", "snow falling", "snowflakes", "flurr", "snowing",
        "blowing snow", "drift", "whiteout",
        "raining", "rainfall", "drizzle", "showers", "downpour",
        "sleet", "hail", "thunderstorm", "storming",
        "precip", "precipitation",
    ]
    low = t.lower()
    return not any(w in low for w in forbidden)

def scrub_precip_words(s: str) -> str:
    return re.sub(
        r"\b(snow|snowing|flurr(?:y|ies)|rain|raining|drizzle|showers?|sleet|hail)\b",
        "precipitation",
        s,
        flags=re.I,
    )

def ollama_generate_season_block(season: str, tod: str, weather_line: str, retries: int = 2) -> str:

    system = build_season_system_prompt(tod)

    safe_weather = scrub_precip_words(weather_line)
    user = build_season_user_prompt(season, tod, safe_weather)

    url = f"{OLLAMA_URL}/api/generate"
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": f"SYSTEM:\n{system}\n\nUSER:\n{user}\n",
        "stream": False,
        "options": {"temperature": 0.3, "top_p": 0.9, "num_predict": 200},
    }

    last = ""
    for _ in range(retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=25)
            r.raise_for_status()
            txt = (r.json().get("response") or "").strip()
            last = txt
            if season_is_valid(txt):
                return txt
        except Exception as e:
            last = (
                "SEASONAL ENVIRONMENT:\n"
                "Trees: season block error contacting ollama service\n"
                f"Ground: {str(e)[:80]}\n"
                "Air: check ollama connectivity and model availability\n"
                "DistantDetails: check ollama logs for generation failures"
            )
    return last.strip()

def build_clothing_block(profile: dict) -> str:
    extras = ", ".join(profile.get("extras") or []) or "none"
    return (
        "CLOTHING OVERLAY:\n"
        f"BaseTop: {profile.get('base_top') or 'none'}\n"
        f"Outerwear: {profile.get('outerwear') or 'none'}\n"
        f"Legwear: {profile.get('legs')}\n"
        f"Footwear: {profile.get('boots')}\n"
        f"Extras: {extras}"
    )

def clean_for_json(s: str) -> str:
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = "".join(ch for ch in s if ch in ("\n", "\t") or ord(ch) >= 32)
    return s

# -------- COMFYUI --------
def comfy_render_image(final_prompt: str, width: int, height: int, timeout_s: int = 900) -> Image.Image | None:
    with open(COMFY_WORKFLOW_FILE, "r", encoding="utf-8") as f:
        workflow = json.load(f)

    # 1) set prompt + input image
    workflow["88"]["inputs"]["value"] = final_prompt
    workflow["9"]["inputs"]["image"] = COMFY_INPUT_IMAGE

    # 2) set target size on node 40 (BEFORE queueing!)
    workflow["40"]["inputs"]["aspect_ratio"] = "custom"
    workflow["40"]["inputs"]["proportional_width"] = int(width)
    workflow["40"]["inputs"]["proportional_height"] = int(height)

    # keep existing fit/method/scale_to_side as-is; just avoid black bars
    workflow["40"]["inputs"]["background_color"] = "#F5F5F5"

    # node 40 reads scale_to_length from node 35, so keep long-side limit consistent
    workflow["35"]["inputs"]["value"] = int(max(width, height))

    #print(f"COMFY POST -> {COMFY_URL}/prompt", flush=True)
    #print(f"COMFY workflow file -> {COMFY_WORKFLOW_FILE}", flush=True)

    # 3) queue
    r = requests.post(f"{COMFY_URL}/prompt", json={"prompt": workflow}, timeout=30)
    #print(f"COMFY /prompt status -> {r.status_code}", flush=True)
    #print(f"COMFY /prompt body -> {r.text[:800]}", flush=True)

    if r.status_code != 200:
        raise RuntimeError(f"Comfy /prompt failed: HTTP {r.status_code} body={r.text[:1000]}")

    prompt_id = r.json()["prompt_id"]
    #print(f"COMFY prompt_id -> {prompt_id}", flush=True)

    deadline = time.time() + timeout_s
    last_err = None

    while time.time() < deadline:
        #print(f"Polling Comfy history for {prompt_id}", flush=True)
        h = requests.get(f"{COMFY_URL}/history/{prompt_id}", timeout=30)
        h.raise_for_status()
        hist = h.json().get(prompt_id)

        # If the workflow failed, Comfy usually includes an "error" object somewhere in history
        if hist and hist.get("status") == "error":
            last_err = str(hist.get("error") or hist.get("messages") or "unknown comfy error")
            break

        if hist and hist.get("outputs"):
            for _, out in hist["outputs"].items():
                imgs = out.get("images") or []
                if imgs:
                    img_meta = imgs[0]
                    params = {
                        "filename": img_meta["filename"],
                        "subfolder": img_meta.get("subfolder", ""),
                        "type": img_meta.get("type", "output"),
                    }
                    img_r = requests.get(f"{COMFY_URL}/view", params=params, timeout=60)
                    img_r.raise_for_status()
                    #print(f"History status keys: {list(h.json().keys())[:5]}", flush=True)
                    return Image.open(io.BytesIO(img_r.content)).convert("RGB")

        time.sleep(1.5)

    # Surface a useful error if we saw one
    if last_err:
        raise RuntimeError(f"Comfy workflow error: {last_err}")

    raise TimeoutError(f"ComfyUI render timed out after {timeout_s}s waiting for output image.")

def draw_multiline(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, line_spacing: int = 8):
    x, y = xy
    for line in text.splitlines():
        draw.text((x, y), line, font=font, fill=fill)
        y += font.size + line_spacing

# -------- WALLPAPER RENDER --------
def render_wallpaper(path: str):
    W, H = 1600, 1200
    img = Image.new("RGB", (W, H), (245, 245, 245))
    d = ImageDraw.Draw(img)

    pad = 15
    top_h = int(H * 0.70)

    pic_w = int((W - pad*3) * 0.70)
    cal_w = (W - pad*3) - pic_w

    pic = (pad, pad, pad + pic_w, pad + top_h)
    cal = (pad*2 + pic_w, pad, W - pad, pad + top_h)
    todo = (pad, pad*2 + top_h, W - pad, H - pad)

    def box(rect, title: str | None):
        # Border only (black)
        d.rounded_rectangle(rect, radius=18, fill=(245,245,245), outline=(0,0,0), width=6)
        if title:
            d.text((rect[0] + 28, rect[1] + 20), title, font=load_font(44), fill=(0, 0, 0))

    # No titles in these boxes (you requested removing Calendar label)
    box(cal, None)
    box(pic, None)
    box(todo, None)

    now = datetime.now().strftime("%a %b %d %H:%M:%S %Z %Y")

    # -------- Weather / Prompt / Picture generation (unchanged) --------
    w = ha_get_state(WEATHER_ENTITY)
    weather_line = format_weather_for_display(w)

    base_prompt = BASE_IMAGE_PROMPT

    tod = infer_day_or_night_from_weather(w)
    season = infer_season(w)

    overlay = ollama_generate_overlay(weather_line, tod)
    season_block = ollama_generate_season_block(season, tod, weather_line)
    temp_c, precip, intensity, icy = extract_clothing_weather_inputs(w)
    clothing_profile = choose_clothing_profile(temp_c, precip, intensity, icy)

    clothing_block = build_clothing_block(clothing_profile)

    final_prompt = build_final_prompt(
        base_prompt,
        overlay,
        season_block,
        clothing_block,
    )
    final_prompt = clean_for_json(final_prompt)

    title_font = load_font(60)
    meta_font = load_font(34)
    small_font = load_font(28)

#    d.text((W // 2 - 520, H // 2 - 120), "WALLPAPER PIPELINE TEST", font=title_font, fill=(245, 245, 245))
#    d.text((W // 2 - 360, H // 2 - 20), now, font=meta_font, fill=(220, 220, 220))
#    d.text((W // 2 - 700, H // 2 + 35), weather_line, font=small_font, fill=(210, 210, 210))
#    draw_multiline(d, (W // 2 - 700, H // 2 + 80), overlay, font=small_font, fill=(205, 205, 205), line_spacing=8)

    picture = None
    try:
    # inner area
        inner_pad = 16
        x0, y0, x1, y1 = pic
        x0 += inner_pad
        y0 += inner_pad
        x1 -= inner_pad
        y1 -= inner_pad
        box_w, box_h = (x1 - x0), (y1 - y0)

        picture = comfy_render_image(final_prompt, width=box_w, height=box_h)
    except Exception as e:
        err = f"Comfy error:\n{str(e)[:220]}"
        draw_multiline(d, (pic[0] + 28, pic[1] + 28), err, font=small_font, fill=(230, 180, 180))

    if picture:
    # picture should already be exact size; paste directly
        img.paste(picture, (x0, y0))

    # -------- Calendar rendering (NEW) --------
    # Fonts tuned for your narrow calendar panel
    cal_header_font = load_font(28)  # "March 2026"
    day_num_font = load_font(22)     # 1,2,3...
    evt_font = load_font(18)
    print("FONT_SIZES calendar:", cal_header_font.size, day_num_font.size, evt_font.size, flush=True)
 #   print("fonts:", getattr(cal_header_font, "size", None),
 #                 getattr(day_num_font, "size", None),
 #                 getattr(evt_font, "size", None))
    cx0, cy0, cx1, cy1 = cal
    inner_x = 26

    # Header month/year (always long month, as requested)
    now_local = datetime.now().astimezone()
    month_header = now_local.strftime("%B %Y")  # "March 2026"
    header_w = d.textlength(month_header, font=cal_header_font)
    header_x = cx0 + ((cx1 - cx0) - header_w) / 2

    draw_text_bold(d, (header_x, cy0 + 18), month_header,
                   font=cal_header_font, fill=(40,40,40),
                   stroke_fill=(60,60,60), stroke_width=1)

    # Grab events from HA (we'll ask for ~5 days ahead so we can show 3-5 days)
    events = []
    cal_err = None
    try:
        today0 = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = today0 + timedelta(days=5)

        raw = ha_calendar_get_events_range(CALENDAR_ENTITY, today0, end_dt)
        sr = (raw or {}).get("service_response") or {}
        cal_obj = sr.get(CALENDAR_ENTITY) or {}
        events = cal_obj.get("events") or []
        if not isinstance(events, list):
            events = []
    except Exception as e:
        cal_err = str(e)

    def _group_events_by_day(evts: list[dict]):
        buckets = {}
        for e in evts:
            summary = (e.get("summary") or "(no title)").strip()
            start = e.get("start") or ""
            day, tstr, all_day = _parse_event_start(start)
            if day is None:
                continue
            buckets.setdefault(day, []).append({"time": tstr, "summary": summary, "all_day": all_day})

        # timed first, then all-day (agenda feel)
        for day, items in buckets.items():
            def key(it):
                if it["time"] is None:
                    return (1, "99:99", it["summary"].lower())
                return (0, it["time"], it["summary"].lower())
            items.sort(key=key)

        return [(d0, buckets[d0]) for d0 in sorted(buckets.keys())]

    # Where content begins under the header
    y = cy0 + 90
    max_w = (cx1 - cx0) - inner_x * 2

    if cal_err:
        d.text((cx0 + inner_x, y), f"Calendar error: {cal_err[:120]}", font=evt_font, fill=(230, 180, 180))
    else:
        # ---- Build buckets: date -> list of items ----
        today = datetime.now().astimezone().date()
        days_to_show = 5
        max_events_per_day = 5

        buckets = {}
        for e in events:
            summary = (e.get("summary") or "(no title)").strip()
            start = e.get("start") or ""
            day, tstr, all_day = _parse_event_start(start)
            if day is None:
                continue
            buckets.setdefault(day, []).append({"time": tstr, "summary": summary, "all_day": all_day})

        # timed first, then all-day
        for day, items in buckets.items():
            def key(it):
                if it["time"] is None:
                    return (1, "99:99", it["summary"].lower())
                return (0, it["time"], it["summary"].lower())
            items.sort(key=key)

        # ---- Render consecutive days, even if empty ----
        # ----- Fixed-slot day layout -----
        days_to_show = 5
        max_events_per_day = 3  # tune if you want

# vertical region available for day blocks
        header_top = cy0 + 18
        header_height = cal_header_font.size + 18
        content_top = header_top + header_height + 18
        content_bottom = cy1 - 18

        slot_h = (content_bottom - content_top) / days_to_show

        today = datetime.now().astimezone().date()

        for i in range(days_to_show):
            day = today + timedelta(days=i)
            items = buckets.get(day, [])

            slot_y0 = int(content_top + i * slot_h)
            slot_y1 = int(content_top + (i + 1) * slot_h)

    # baseline positions within slot
            y_num = slot_y0
            y_line = y_num + day_num_font.size + 8
            y_text = y_line + 10

# day number (bold)
            draw_text_bold(
                d, (cx0 + inner_x, y_num), str(day.day),
                font=day_num_font, fill=(14, 14, 14),
                stroke_width=1, stroke_fill=(60, 60, 60)
            )

# weekday initials (e.g. "Sun")
            dow = day.strftime("%a")  # Sun/Mon/Tue...
            dow_font = evt_font       # or load_font(evt_font.size) if you prefer separate
            num_text = str(day.day)
            num_w = d.textlength(num_text, font=day_num_font)
            dow_x = cx0 + inner_x + int(num_w) + 14  # space to the right of the big number
            dow_y = y_num + int(day_num_font.size * 0.25)

            draw_text_bold(
                d, (dow_x, dow_y), dow,
                font=dow_font, fill=(60, 60, 60),
                stroke_width=1
            )
    # day number
            #draw_text_bold(d, (cx0 + inner_x, y_num), str(day.day),
            #               font=day_num_font, fill=(14, 14, 14), stroke_width=1, stroke_fill=(60,60,60))

    # divider
            d.line((cx0 + inner_x, y_line, cx1 - inner_x, y_line), fill=(30, 30, 30), width=3)

    # how many text lines fit in this slot?
            line_h = evt_font.size + 6
            max_lines_in_slot = max(1, (slot_y1 - y_text - 8) // line_h)

    # build display lines
            lines = []
            if not items:
                lines = ["• Nothing planned"]
            else:
                for it in items:
                    if it["time"]:
                        lines.append(f"• {it['time']}  {it['summary']}")
                    else:
                        lines.append(f"• {it['summary']}")

    # wrap + flatten (so long titles wrap but still respect max lines)
            wrapped_lines = []
            for line in lines:
                wrapped_lines.extend(wrap_text(d, line, evt_font, max_w))

    # truncate if needed
            if len(wrapped_lines) > max_lines_in_slot:
                wrapped_lines = wrapped_lines[:max_lines_in_slot]
        # optional: replace last line with "+ more…" indicator
                if items:
                    wrapped_lines[-1] = "• + more…"

            draw_wrapped_lines(d, cx0 + inner_x, y_text, wrapped_lines,
                               evt_font, (14, 14, 14), max_lines=max_lines_in_slot, line_gap=6)

        if not events:
            d.text((cx0 + inner_x, cy0 + 110), "No upcoming events", font=evt_font, fill=(210, 210, 210))

    # -------- To-do (leave empty for now) --------

    # -------- To-do (Daily) --------
    tx0, ty0, tx1, ty1 = todo
    t_inner = 26

    todo_header_font = load_font(18)
    todo_font = evt_font
    todo_header = "Daily To-Do"

    tw = d.textlength(todo_header, font=todo_header_font)
    tx = tx0 + ((tx1 - tx0) - tw) / 2

    draw_text_bold(
        d, (tx, ty0 + 18), todo_header,
        font=todo_header_font, fill=(40,40,40),
        stroke_fill=(60,60,60), stroke_width=1
    )

    line_y = ty0 + 18 + todo_header_font.size + 10
    d.line((tx0 + t_inner, line_y, tx1 - t_inner, line_y), fill=(30,30,30), width=3)

    y = line_y + 14
    max_w = (tx1 - tx0) - t_inner * 2

    # Try cached AI lines first (cheap)
    with TODO_AI_LOCK:
        ai_lines = list(TODO_AI_CACHE.get("lines") or [])
        ai_summary = (TODO_AI_CACHE.get("summary_line") or "").strip()
        quote_idx = TODO_AI_CACHE.get("quote_idx")

    if ai_lines:
    # Quote appears on visible line 5 (idx 4) only when we inserted it:
    # buffer line (idx 3) blank + idx 4 non-empty
        quote_idx = 4 if (len(ai_lines) >= 5 and not (ai_lines[3] or "").strip() and (ai_lines[4] or "").strip()) else None

        prev_was_task = False

        for idx, ln in enumerate(ai_lines[:6]):
            ln = (ln or "").strip()
            if is_effectively_blank_line(ln):
                prev_was_task = False
                y += (todo_font.size + 6) + 10
                continue

        # address line gets no bullet and is indented
            if prev_was_task and looks_like_address_line(ln):
                txt = clamp_to_width(d, "  " + ln, todo_font, max_w)
                d.text((tx0 + t_inner, y), txt, font=todo_font, fill=(14,14,14))
                prev_was_task = False
            else:
            # quote line has no bullet ONLY if we actually inserted a quote there
                prefix = "" if (quote_idx == idx) else "• "
                txt = clamp_to_width(d, f"{prefix}{ln}", todo_font, max_w)
                d.text((tx0 + t_inner, y), txt, font=todo_font, fill=(14,14,14))
                prev_was_task = True

            y += (todo_font.size + 6) + 10
            if y > ty1 - 40:
                break

    # Reserved line 7
        if ai_summary and y <= ty1 - 30:
            d.text((tx0 + t_inner, y), clamp_to_width(d, ai_summary, todo_font, max_w), font=todo_font, fill=(80,80,80))

    else:
        # Fallback to raw (your current behavior)
        items = ha_todo_get_items(TODO_ENTITY)
        open_raw = []
        for it in items:
            status = (it.get("status") or "").lower()
            if status == "completed":
                continue
            raw = (it.get("summary") or it.get("item") or "").strip()
            if raw:
                open_raw.append(raw)

        parsed = [parse_task_key_and_text(s) for s in open_raw]
        parsed.sort(key=lambda x: (x[0], x[1], x[2].lower()))
        open_display = [p[2] for p in parsed if p[2]]

        MAX_SHOW = 6
        shown = open_display[:MAX_SHOW]
        remaining = max(0, len(open_display) - MAX_SHOW)

        if not shown:
            shown = ["Nothing planned"]

        for text in shown:
            line = f"• {text}"
            wrapped = wrap_text(d, line, todo_font, max_w)
            draw_wrapped_lines(d, tx0 + t_inner, y, wrapped, todo_font, (14,14,14), max_lines=1, line_gap=6)
            y += (todo_font.size + 6) + 10
            if y > ty1 - 40:
                break

        if remaining > 0 and y <= ty1 - 30:
            d.text((tx0 + t_inner, y), f"+{remaining} more…", font=todo_font, fill=(80,80,80))

    # -------- Save Image --------
    img.save(path, "PNG")

# -------- JOB RUNNER --------
def _run_render_job():
    global JOB_RUNNING, LAST_JOB
    with JOB_LOCK:
        JOB_RUNNING = True
        LAST_JOB = {"status": "running", "started": time.time(), "finished": None, "error": None}

    try:
        os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

        # 1) wallpaper
        render_wallpaper(OUT_PATH)

        # 2) bin
        bin_path = os.path.join(os.path.dirname(OUT_PATH), "image_data.bin")
        n = generate_epaper_bin_epdoptimize(OUT_PATH, bin_path)

        # 3) optional push
        try:
            push_bin_to_epd(bin_path)
        except Exception as e:
            if EPD_PUSH_FAIL_HARD:
                raise
            # soft-fail: log but still mark render ok
            print(f"EPD push failed (soft): {e}")

        with JOB_LOCK:
            LAST_JOB["status"] = "ok"
            LAST_JOB["finished"] = time.time()
            LAST_JOB["error"] = None

    except Exception as e:
        with JOB_LOCK:
            LAST_JOB["status"] = "error"
            LAST_JOB["error"] = str(e)
            LAST_JOB["finished"] = time.time()

    finally:
        with JOB_LOCK:
            JOB_RUNNING = False

# -------- API --------
@app.post("/todo_ai_refresh")
def todo_ai_refresh():
    items_open = _todo_items_open_sorted(TODO_ENTITY)
    h = _stable_todo_hash(items_open)

    today = date.today().isoformat()

    with TODO_AI_LOCK:
        if TODO_AI_CACHE["hash"] == h and TODO_AI_CACHE.get("day") == today:
            return {"ok": True, "changed": False, "hash": h, "cached": True}

    # Not cached → run ollama
    formatted = ollama_format_todo(items_open)

    # Build EXACTLY 7 lines: 1-6 visible, 7 summary
    lines = list(formatted.get("lines") or [])[:6]
    while len(lines) < 6:
        lines.append("")
    lines.append((formatted.get("summary_line") or "").strip())  # line 7

    # Clean fake blank placeholders like "—" or "-"
    lines = [("" if is_effectively_blank_line(ln) else ln) for ln in lines]

    # If the model accidentally put a "+N more" line in the first 6 lines, move it to line 7.
    for i in range(6):
        if lines[i].lstrip().startswith("+") and "more" in lines[i].lower():
            lines[6] = lines[i].strip()
            lines[i] = ""
            break

    # ---- pack + optional quote (ONLY touch lines 0..5) ----
    packed = [ln for ln in lines[:6] if (ln or "").strip()]

    new6 = [""] * 6
    for i, ln in enumerate(packed[:6]):
        new6[i] = ln

    used = sum(1 for ln in new6 if ln.strip())
    blanks = 6 - used

    print(f"todo_ai_refresh: used={used} quote_allowed={used == 0 or used <= 3}", flush=True)

    # If no tasks, or at least 2 free lines, insert quote with a buffer line above it
    quote_idx = None
    if used == 0 or used <= 3:
        q = ollama_daily_quote()
        new6[3] = ""   # buffer (line 4)
        new6[4] = q    # quote  (line 5)
        quote_idx = 4

    lines[:6] = new6

    # If line 7 is empty, synthesize it.
    if not lines[6].strip():
        lines[6] = "+0 more — no more tasks"

    with TODO_AI_LOCK:
        TODO_AI_CACHE["hash"] = h
        TODO_AI_CACHE["lines"] = lines[:6]
        TODO_AI_CACHE["summary_line"] = lines[6]
        TODO_AI_CACHE["ts"] = int(time.time())
        TODO_AI_CACHE["day"] = today

    return {
        "ok": True,
        "changed": True,
        "hash": h,
        "lines": lines[:6],
        "summary": lines[6],
    }

@app.post("/render_test")
def render_test():
    """
    Fast test path:
    - does NOT call HA
    - does NOT call Ollama
    - does NOT call Comfy
    - just converts the existing wallpaper and pushes it to the EPD
    """
    try:
        return push_existing_wallpaper_to_epd()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/todo_generate_today")
def todo_generate_today():
    try:
        ev = calendar_events_today(CALENDAR_ENTITY)
        tasks = build_tasks_from_events(ev)

        removed = ha_todo_clear_list(TODO_ENTITY)

        for t in tasks:
            ha_todo_add_item(
                TODO_ENTITY,
                t["summary"],
                due_date=t.get("due_date"),
                due_datetime=t.get("due_datetime"),
                description=t.get("description"),
            )

        return {
            "ok": True,
            "calendar": CALENDAR_ENTITY,
            "todo": TODO_ENTITY,
            "removed": removed,
            "added": len(tasks),
            "tasks": tasks,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/render_async")
def render_async(background_tasks: BackgroundTasks):
    with JOB_LOCK:
        if JOB_RUNNING:
            return {"ok": True, "queued": False, "status": "busy"}
    background_tasks.add_task(_run_render_job)
    return {"ok": True, "queued": True}

@app.post("/render_test_path")
def render_test_path(path: str):
    """
    Convert and push any existing PNG on disk.
    Example:
      /render_test_path?path=/out/wallpaper.png
    """
    try:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Image not found: {path}")

        bin_path = os.path.join(os.path.dirname(OUT_PATH), "image_data.bin")
        n = generate_epaper_bin_epdoptimize(path, bin_path)

        if EPD_PUSH_ENABLED:
            push_bin_to_epd(bin_path)

        return {
            "ok": True,
            "source": path,
            "bin": bin_path,
            "bin_bytes": n,
            "epd_push": "ok" if EPD_PUSH_ENABLED else "disabled",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/render_status")
def render_status():
    with JOB_LOCK:
        return {"ok": True, "running": JOB_RUNNING, "last": LAST_JOB}

@app.post("/render")
def render():
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    render_wallpaper(OUT_PATH)

    bin_path = os.path.join(os.path.dirname(OUT_PATH), "image_data.bin")
    n = generate_epaper_bin_epdoptimize(OUT_PATH, bin_path)

    # optional push
    if EPD_PUSH_ENABLED:
        try:
            push_bin_to_epd(bin_path)
        except Exception as e:
            if EPD_PUSH_FAIL_HARD:
                raise HTTPException(status_code=500, detail=f"EPD push failed: {e}")
            # soft: include it in response
            return {
                "ok": True,
                "out": OUT_PATH,
                "bin": bin_path,
                "bin_bytes": n,
                "epd_push": "failed_soft",
                "epd_push_error": str(e),
                "weather_entity": WEATHER_ENTITY,
                "ollama_model": OLLAMA_MODEL,
            }

    if n != BIN_BYTES_EXPECTED:
        raise HTTPException(status_code=500, detail=f"bin size wrong: {n} bytes (expected {BIN_BYTES_EXPECTED})")

    return {
        "ok": True,
        "out": OUT_PATH,
        "bin": bin_path,
        "bin_bytes": n,
        "epd_push": "ok" if EPD_PUSH_ENABLED else "disabled",
        "weather_entity": WEATHER_ENTITY,
        "ollama_model": OLLAMA_MODEL,
    }

@app.get("/health")
def health():
    return {"ok": True}
