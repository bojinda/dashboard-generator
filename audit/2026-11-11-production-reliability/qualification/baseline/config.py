import os


def get_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and (value is None or str(value).strip() == ""):
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip() if isinstance(value, str) else value


OUT_PATH = "/out/wallpaper.png"
BIN_BYTES_EXPECTED = 960000  # 1200*1600/2

HA_URL = get_env("HA_URL", required=True).rstrip("/")
HA_TOKEN = get_env("HA_TOKEN", required=True)

WEATHER_ENTITY = get_env("WEATHER_ENTITY", "weather.forecast_home")
CALENDAR_ENTITY = get_env("CALENDAR_ENTITY", required=True)
TODO_ENTITY = get_env("TODO_ENTITY", "todo.daily_to_do")

OLLAMA_URL = get_env("OLLAMA_URL", required=True).rstrip("/")
OLLAMA_MODEL = get_env("OLLAMA_MODEL", "qwen2.5:14b")

COMFY_INPUT_IMAGE = get_env("COMFY_INPUT_IMAGE", "portrait.jpg")
COMFY_URL = get_env("COMFY_URL", required=True).rstrip("/")
COMFY_WORKFLOW_FILE = get_env("COMFY_WORKFLOW_FILE", "/app/workflows/picture_api.json")

EPD_PUSH_ENABLED = get_env("EPD_PUSH_ENABLED", "0") == "1"
EPD_PUSH_URL = get_env("EPD_PUSH_URL", "")
EPD_PUSH_FAIL_HARD = get_env("EPD_PUSH_FAIL_HARD", "0") == "1"
EPD_ROTATE_CW = get_env("EPD_ROTATE_CW", "270")