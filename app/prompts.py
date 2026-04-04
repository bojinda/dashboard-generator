# /opt/dashboard-gen/app/prompts.py

from datetime import date, datetime
import json


BASE_IMAGE_PROMPT = (
    "Turn the man from image 1 into a freight train conductor in the style of a mature, gritty shonen anime. "
    "The man is standing by a switch, away from the tracks, and holding a lantern. "
    "Keep original features of the man the same. same dark tanned skin tone. Outdoors in woods, "
    "work clothes, no mask, no phone, goatee, a closed thick black canvass jacket, "
    "orange safety vest worn over jacket.\n"
    "IMPORTANT: keep clothing, pose, identity, location, and composition unchanged. "
    "Only environment weather changes."
)


def build_quote_system_prompt(recent: list[str]) -> str:
    return (
        "You write ONE short motivational quote for a to-do dashboard.\n"
        "Rules:\n"
        "- Output ONE line only.\n"
        "- Output in English only.\n"
        "- Use only plain ASCII characters.\n"
        "- 35 to 80 characters.\n"
        "- Witty, varied, lightly encouraging.\n"
        "- Not cheesy, not generic, not repetitive.\n"
        "- No quotes, no emojis, no prefixes.\n"
        "- No explanations, apologies, retries, or extra commentary.\n"
        "- Avoid these recently used quotes exactly or closely:\n"
        + "\n".join(f"  - {q}" for q in recent[-8:])
    )


def build_quote_user_prompt(today: str | None = None) -> str:
    today = today or date.today().isoformat()
    return f"Today is {today}. Write a fresh quote."


def build_todo_system_prompt() -> str:
    return (
        "You rewrite and format a compact to-do list for an e-paper dashboard.\n"
        "You MUST expand tasks into clearer, more descriptive wording using any details provided.\n"
        "Even if the summary is vague, infer a clearer phrasing from description/location.\n"
        "\n"
        "Output rules:\n"
        "- Output EXACTLY 7 lines of plain text.\n"
        "- Lines 1-6 must fit on ONE line each (renderer will clamp overflow).\n"
        "- Line 7 is a summary line like: +N more — rent, storage, meeting\n"
        "- Always produce line 7.\n"
        "- Aim for <= 70 characters per line, but prioritize clarity.\n"
        "\n"
        "- Each task normally uses EXACTLY 1 line.\n"
        "- A task may use 2 lines ONLY if:\n"
        "  1) It has a location/address AND\n"
        "  2) The task would not fit clearly in one line.\n"
        "\n"
        "- If 2 lines are used:\n"
        "  - Line 1: Expanded action description\n"
        "  - Line 2: Shortened address (street + city only)\n"
        "  - Remove province/state, postal/zip, country.\n"
        "\n"
        "- Across ALL tasks, NEVER exceed 6 visible lines total.\n"
        "- If tasks do not fit:\n"
        "  - Keep all-day tasks first\n"
        "  - Then earliest timed tasks\n"
        "  - Push the rest into line 7 as +N more.\n"
        "\n"
        "- For timed tasks, include time like '10:30 — Doctor visit'\n"
        "- If fewer than 6 visible lines are used, fill remaining lines with blanks.\n"
        "- Do NOT mention dates like 'until March 9th' unless the task JSON clearly requires it.\n"
        "- If there are no tasks today, use a simple present-tense line like 'Nothing planned today.'\n"
        "- Do NOT infer future dates or countdown wording.\n"
        "- Try to be funny/sarcastic, but not overly so or offensive.\n"
    )


def build_todo_user_prompt(tasks: list[dict], today: str | None = None) -> str:
    today = today or datetime.now().astimezone().date().isoformat()
    return (
        f"Today is {today}.\n"
        "Here are the open tasks as JSON.\n"
        "Rewrite each task name into a fuller one-line explanation.\n"
        "Use:\n"
        "- summary as the base title\n"
        "- description for the purpose/details\n"
        "- location/address for where (put on second line)\n"
        "- due for date/time context if helpful\n\n"
        "Return exactly 7 lines.\n\n"
        + json.dumps(tasks, ensure_ascii=False)
    )


def build_overlay_system_prompt(tod: str) -> str:
    celestial_rule = (
        "- It is DAYTIME: you MAY reference sun/daylight; DO NOT mention moon or stars.\n"
        if tod == "day"
        else "- It is NIGHTTIME: you MAY reference moon/stars; DO NOT mention sun or daylight.\n"
    )

    return (
        "You generate ONLY a WEATHER OVERLAY block for an existing image-edit prompt.\n"
        "Rules:\n"
        "- DO NOT change character, clothing, props, location, composition, or style.\n"
        f"{celestial_rule}"
        "- Output EXACTLY 5 lines in this exact format:\n"
        "WEATHER OVERLAY:\n"
        "Sky: ...\n"
        "Air: ...\n"
        "Ground: ...\n"
        "LanternLight: ...\n"
        "- Each line after the label must be 6-16 words.\n"
        "- LanternLight must ONLY describe interaction with weather/surfaces.\n"
        "- No extra text."
    )


def build_overlay_user_prompt(weather_line: str) -> str:
    return (
        "Weather now (for visuals only):\n"
        f"{weather_line}\n\n"
        "Make the overlay match subtly and realistically."
    )


def build_season_system_prompt(tod: str) -> str:
    tod_line = "DAYTIME." if tod == "day" else "NIGHTTIME."
    return (
        "You generate ONLY a SEASONAL ENVIRONMENT block.\n"
        "Rules:\n"
        "- Static season cues only (set dressing): trees, ground cover, colors, distant scenery.\n"
        "- DO NOT describe active weather or moving precipitation (no falling snow, snowing, flurries, raining, drizzle, showers, blowing snow).\n"
        "- You MAY describe static aftermath cues (wet ground, puddles, damp surfaces, mud) WITHOUT mentioning rain.\n"
        "- DO NOT mention the character, clothing, props, train elements, or composition.\n"
        f"- Keep consistent with time-of-day: {tod_line}\n"
        "- Output EXACTLY 5 lines in this exact format:\n"
        "SEASONAL ENVIRONMENT:\n"
        "Trees: ...\n"
        "Ground: ...\n"
        "Air: ...\n"
        "DistantDetails: ...\n"
        "- Each line after the label must be 6-16 words.\n"
        "- Use realistic cues for the given season.\n"
        "- No extra text."
    )


def build_season_user_prompt(season: str, tod: str, weather_line: str) -> str:
    return (
        f"Season: {season}\n"
        f"Time of day: {tod}\n"
        "Current conditions (for subtle realism, avoid describing active weather):\n"
        f"{weather_line}\n"
        "Write subtle seasonal cues (e.g., leaves, ground cover, haze, mud, frost)."
    )