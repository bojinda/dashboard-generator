#!/usr/bin/env bash
# Media dashboard: guarded source/env reloading using the existing pinned image.
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
BRANCH=feature/shared-gpu-coordination
SERVICE=dashboard-gen
MODE=${1:---check}
if [[ $# -gt 1 || ( $MODE != --check && $MODE != --apply ) ]]; then
  echo "Usage: $0 [--check|--apply]" >&2; exit 2
fi
fail() { echo "STOP: $*" >&2; exit 1; }

cd "$ROOT"
[[ $(git branch --show-current) == "$BRANCH" ]] || fail 'unexpected Git branch'
git diff --quiet || fail 'uncommitted tracked changes'
git diff --cached --quiet || fail 'staged changes not committed'
git ls-files --error-unmatch deploy.sh >/dev/null 2>&1 || fail 'commit deploy.sh before using it'
[[ $(git rev-parse HEAD) == $(git rev-parse '@{upstream}') ]] || fail 'checkout differs from fetched upstream'
[[ -r .env && -r docker-compose.override.yml ]] || fail 'private env or local Compose override missing'
[[ $(docker inspect -f '{{.State.Running}}' "$SERVICE") == true ]] || fail 'dashboard container is not running'

# No rebuild: the image is explicitly pinned and the client SDK is inside it.
IMAGE=$(docker inspect -f '{{.Config.Image}}' "$SERVICE")
IMAGE_ID=$(docker inspect -f '{{.Image}}' "$SERVICE")
[[ $(docker image inspect -f '{{.Id}}' "$IMAGE") == "$IMAGE_ID" ]] || fail 'local image tag no longer resolves to the running image'
docker compose config --format json | python3 -c '
import json, sys
service = json.load(sys.stdin)["services"]["dashboard-gen"]
if service.get("image") != sys.argv[1] or service.get("build"):
    raise SystemExit("STOP: image differs from running container or build enabled")
targets = {v["target"] for v in service.get("volumes", [])}
if not {"/app", "/app/workflows", "/var/lib/dashboard-gpu-client"}.issubset(targets):
    raise SystemExit("STOP: required app/workflow/client-state bind mounts missing")
print("PASS: pinned-image Compose configuration and persistent mounts")
' "$IMAGE"

check_idle() {
  docker exec -i "$SERVICE" python - <<'PY'
import json
from pathlib import Path
from urllib.request import urlopen
from aihub_gpu_runner.client import JobClient
from main import _output_slot
from config import COMFY_WORKFLOW_FILE

if not Path(COMFY_WORKFLOW_FILE).is_file():
    raise SystemExit('STOP: selected workflow missing')
record = _output_slot().status()
if record.get('status') == 'running':
    raise SystemExit('STOP: dashboard output operation running')
with urlopen('http://127.0.0.1:8787/render_status', timeout=5) as response:
    status = json.load(response)
if status.get('running') is not False:
    raise SystemExit('STOP: dashboard rendering or status uncertain')
code, _, body = JobClient.from_environment().transport.request('GET', '/resources')
if code != 200:
    raise SystemExit('STOP: GPU runner unavailable')
resources = json.loads(body)
if resources.get('ready') is not True or resources.get('owners'):
    raise SystemExit('STOP: GPU runner busy or not ready')
print('PASS: dashboard output idle and GPU runner owner-free')
PY
}

check_idle
VERSION=$(git rev-parse --short HEAD)
if [[ $MODE == --check ]]; then
  echo "CHECK ONLY: Git $VERSION. This script re-creates $SERVICE from the existing image; it never builds or pulls an image."
  exit 0
fi

cat <<'TXT'
Before proceeding: pause scheduled dashboard/AI refresh triggers and prevent
manual render requests during this deployment. This cannot quiesce callers itself.
Dependencies and the GPU runner SDK must ALREADY be present in the pinned image.
TXT
read -r -p 'Type DEPLOY DASHBOARD to continue: ' CONFIRM
[[ $CONFIRM == 'DEPLOY DASHBOARD' ]] || fail 'not approved'
check_idle

# Recreate with the existing image, preserving bind mounts, data and Compose override.
docker compose up -d --no-deps --no-build --force-recreate "$SERVICE"
[[ $(docker inspect -f '{{.State.Running}}|{{.Image}}' "$SERVICE") == "true|$IMAGE_ID" ]] || fail 'new container is not running on the expected image'

docker exec -i "$SERVICE" python - <<'PY'
import json, time
from urllib.request import urlopen
import recovery_status
import aihub_gpu_runner.client
for attempt in range(15):
    try:
        with urlopen('http://127.0.0.1:8787/health', timeout=2) as response:
            if response.status == 200 and json.load(response).get('ok') is True:
                print('PASS: dashboard health, indicator and runner client')
                break
    except Exception:
        if attempt == 14:
            raise
        time.sleep(1)
else:
    raise SystemExit('STOP: dashboard health not verified')
PY

echo "PASS: dashboard redeployed from pinned image at Git $VERSION"
