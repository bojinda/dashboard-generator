![20260326_120247_preview](https://github.com/user-attachments/assets/5c1f15a2-e0df-4eea-b57c-afd371a1cef5)

# Dashboard Generator

A self-hosted dashboard image generator for e-paper or wall display use.

This project builds a single dashboard image that combines:
- a generated portrait scene from ComfyUI
- Home Assistant weather, calendar, and to-do data
- Ollama-generated overlay, seasonal scene details, and compact to-do formatting
- optional EPD binary conversion and push to an e-paper display

The FastAPI app exposes endpoints for full renders, async renders, test conversions, to-do refresh, and health/debug checks.

## Features

- Pulls weather, calendar, and to-do data from Home Assistant
- Uses Ollama to:
  - generate a weather-aware image overlay
  - generate seasonal environmental details
  - reformat daily to-do items into compact display-friendly text
  - generate a short daily quote when space allows
- Uses ComfyUI for portrait/image generation
- Renders a final dashboard PNG
- Converts the PNG into an EPD-friendly binary with `epdoptimize`
- Optionally pushes the binary to an ESP32/e-paper display
- Supports soft-fail EPD upload so image generation can still succeed even if the display is offline or unreachable

## How it works

1. Home Assistant provides current weather, calendar events, and to-do items.
2. Ollama generates:
   - a weather overlay block
   - a seasonal environment block
   - compact to-do text
   - an optional daily quote
3. ComfyUI generates the portrait scene using a source image and prompt.
4. The app composes the final dashboard PNG.
5. `epdoptimize` converts the PNG to an EPD binary.
6. The binary can optionally be pushed to a display endpoint.

## Repo structure

```text
dashboard-gen/
├── app/
│   ├── config.py
│   ├── main.py
│   ├── prompts.py
│   └── requirements.txt
├── frame-app/
│   ├── mk_bin_and_upload.py
│   └── mk_e6_bin.py
├── workflows/
│   └── picture_api.json
├── .env.example
├── .gitignore
├── docker-compose.yml
└── Dockerfile
```

## Requirements

You will need:

- Docker and Docker Compose
- Home Assistant with:
  - a weather entity
  - a calendar entity
  - a to-do entity
  - a long-lived access token
- Ollama
- ComfyUI
- Node available inside the container for `epdoptimize`
- The `frame-tools` path mounted so the app can access:
  - `/frame-tools/epdoptimize/render_epd_bin.js`

## Important ComfyUI note

`COMFY_INPUT_IMAGE` is a filename, not an uploaded file.

That image must exist in the **ComfyUI input folder on the ComfyUI machine**. The app sets the workflow `LoadImage` node to whatever filename is configured in `COMFY_INPUT_IMAGE`, then submits the workflow to ComfyUI.

Example:

```env
COMFY_INPUT_IMAGE=portrait.jpg
```

That means `portrait.jpg` must already exist in ComfyUI’s input directory.

## Configuration

Create a `.env` file in the project root. Start from `.env.example`.

Example:

```env
CONTAINER_NAME=dashboard-gen

TZ=America/Toronto

HA_URL=http://homeassistant.local:8123
HA_TOKEN=replace_me
WEATHER_ENTITY=weather.forecast_home
CALENDAR_ENTITY=calendar.example
TODO_ENTITY=todo.daily_to_do

OLLAMA_URL=http://ollama:11434
OLLAMA_MODEL=qwen2.5:32b

COMFY_URL=http://comfyui:8188
COMFY_INPUT_IMAGE=portrait.jpg
COMFY_WORKFLOW_FILE=/app/workflows/picture_api.json

EPD_PUSH_URL=http://epd-device.local/bin
EPD_PUSH_ENABLED=0
EPD_PUSH_FAIL_HARD=0
EPD_ROTATE_CW=270

OUTPUT_DIR=/opt/homepage/generated
WORKFLOWS_DIR=/opt/dashboard-gen/workflows
ASSETS_DIR=/opt/dashboard-gen/assets
FRAME_TOOLS_DIR=/opt/frame-tools

HOST_PORT=8787
CONTAINER_PORT=8787
```

### Key variables

- `HA_URL`  
  Base URL for Home Assistant.

- `HA_TOKEN`  
  Home Assistant long-lived access token.

- `WEATHER_ENTITY`  
  Weather entity used for display and prompt generation.

- `CALENDAR_ENTITY`  
  Calendar entity used to build the right-side calendar panel and daily to-do generation.

- `TODO_ENTITY`  
  To-do entity used for dashboard display and refreshed daily task generation.

- `OLLAMA_URL`  
  Ollama base URL.

- `OLLAMA_MODEL`  
  Model used for quotes, overlay generation, seasonal prompt generation, and to-do formatting.

- `COMFY_URL`  
  ComfyUI base URL.

- `COMFY_INPUT_IMAGE`  
  Filename ComfyUI will load from its input folder.

- `COMFY_WORKFLOW_FILE`  
  Path to the workflow JSON inside the container.

- `EPD_PUSH_ENABLED`  
  `1` to push the generated binary to the display, `0` to disable pushing.

- `EPD_PUSH_FAIL_HARD`  
  `1` makes EPD upload failure fail the render.  
  `0` keeps render success even if upload fails.

- `EPD_ROTATE_CW`  
  Rotation value passed to `epdoptimize`.

## Docker

Build and run:

```bash
docker compose up -d --build
```

Stop:

```bash
docker compose down
```

View logs:

```bash
docker logs -f dashboard-gen
```

## API endpoints

### Health

```http
GET /health
```

Basic health check. Returns `{"ok": true}`.

### Full render

```http
POST /render
```

Generates the dashboard PNG, converts it to EPD binary, and optionally pushes it to the display. If EPD push is enabled but soft-fail is configured, the render can still succeed with an `epd_push` warning state.

### Async render

```http
POST /render_async
```

Queues a render job unless one is already running.

### Render status

```http
GET /render_status
```

Returns the current async render status and last job metadata.

### Test existing wallpaper

```http
POST /render_test
```

Skips Home Assistant, Ollama, and ComfyUI. Reuses the existing wallpaper PNG, converts it to EPD binary, and optionally pushes it to the display. Useful for EPD tuning.

### Convert and push an arbitrary PNG

```http
POST /render_test_path?path=/out/wallpaper.png
```

Converts any existing PNG on disk to EPD binary and optionally pushes it.

### Refresh AI-formatted to-do cache

```http
POST /todo_ai_refresh
```

Reads open to-do items, asks Ollama to format them into six visible lines plus a summary line, and caches the result. May also insert a daily quote when space allows.

### Generate today’s to-do items from calendar

```http
POST /todo_generate_today
```

Reads today’s calendar events and replaces the current to-do list with items derived from those events.

### Debug endpoints

```http
GET /todo_debug
GET /calendar_debug
```

Useful for inspecting raw Home Assistant to-do and calendar responses.

## Workflow notes

The Comfy workflow uses:
- a `LoadImage` source image node
- a Qwen image edit sampler
- prompt injection through node `88`
- dynamic width/height through node `40`
- long-side scaling via node `35`

If you replace the workflow, keep the node IDs expected by `main.py` unless you also update the app code.

## Output files

The app writes:
- `wallpaper.png`
- `image_data.bin`

The binary is expected to be exactly `960000` bytes for the configured display target. The render path validates that size before treating the conversion as successful.

## Troubleshooting

### ComfyUI returns HTTP 400 / prompt validation failed

Most common cause: `COMFY_INPUT_IMAGE` does not exist in the ComfyUI input folder.

Check:
- your `.env` value for `COMFY_INPUT_IMAGE`
- that the file exists on the ComfyUI machine in its input directory

### EPD push fails with “No route to host”

The dashboard may still generate successfully if:

```env
EPD_PUSH_FAIL_HARD=0
```

That means the image render and EPD conversion can succeed even when the display is offline or unreachable.

### VS Code says `fastapi` or `PIL` cannot be resolved

That is usually a local interpreter issue. Make sure your editor is using the project virtual environment, not the system Python.

### Render succeeds but no image appears on the display

Check:
- `EPD_PUSH_ENABLED`
- `EPD_PUSH_URL`
- display reachability
- that the generated `image_data.bin` exists and has the expected size

## Future improvements

- upload firmware for esp32 13" spectra6 e-paper display
- tighten weather prompt and clothing overlay
- add image preview/debug endpoint
- add stricter startup validation for required Home Assistant entities
- expose a lightweight config/status page
- make display dimensions and layout configurable
