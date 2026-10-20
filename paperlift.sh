#!/usr/bin/env bash
# Paperlift launcher — uv, Python, venv, packages and caches all live in ./.runtime
#   ./paperlift.sh                 start the web app
#   ./paperlift.sh convert *.pdf   batch convert from the terminal
#   ./paperlift.sh ocr             add the optional OCR engine (for scanned PDFs)
#   ./paperlift.sh update          reinstall dependencies
#   ./paperlift.sh reset           delete .runtime (rebuilt on next start)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RT="$HERE/.runtime"
VENV="$RT/venv"
UV="$RT/bin/uv"
PYVER="${PAPERLIFT_PYTHON:-3.12}"
cd "$HERE"

# keep everything uv touches inside the project
export UV_PYTHON_INSTALL_DIR="$RT/python"
export UV_CACHE_DIR="$RT/cache"
export UV_TOOL_DIR="$RT/tools"
export UV_NO_CONFIG=1
export UV_PYTHON_PREFERENCE=only-managed

get_uv() {
  [[ -x "$UV" ]] && return 0
  echo "› Downloading uv into .runtime/bin (one time)" >&2
  mkdir -p "$RT/bin"
  local inst="https://astral.sh/uv/install.sh"
  if command -v curl >/dev/null 2>&1; then curl -LsSf "$inst"; else wget -qO- "$inst"; fi \
    | env UV_INSTALL_DIR="$RT/bin" UV_UNMANAGED_INSTALL="$RT/bin" UV_NO_MODIFY_PATH=1 INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
  [[ -x "$UV" ]] || { echo "Could not download uv (needs curl or wget and an internet connection)." >&2; exit 1; }
}

setup() {
  mkdir -p "$RT"
  get_uv
  if [[ ! -x "$VENV/bin/python" ]]; then
    echo "› Installing private Python $PYVER into .runtime" >&2
    "$UV" venv --quiet --python "$PYVER" "$VENV" >&2
  fi
  local stamp="$RT/.req.sha"
  local want; want="$( (cat requirements.txt; "$VENV/bin/python" -V) | cksum | cut -d' ' -f1)"
  if [[ ! -f "$stamp" || "$(cat "$stamp")" != "$want" ]]; then
    echo "› Installing dependencies into .runtime" >&2
    "$UV" pip install --quiet --python "$VENV/bin/python" -r requirements.txt >&2
    echo "$want" > "$stamp"
  fi
}

case "${1:-}" in
  reset)  rm -rf "$RT"; echo "Removed .runtime — it is rebuilt on the next start."; exit 0;;
  update) rm -f "$RT/.req.sha"; setup; echo "Dependencies up to date."; exit 0;;
  ocr)    setup
          echo "› Installing the OCR add-on (rapidocr + onnxruntime, ~150 MB) into .runtime" >&2
          "$UV" pip install --quiet --python "$VENV/bin/python" -r requirements-ocr.txt >&2
          echo "OCR ready: scanned pages will now be read as text."; exit 0;;
esac
setup
export PYTHONDONTWRITEBYTECODE=1 PYTHONWARNINGS=ignore
exec "$VENV/bin/python" -m paperlift.cli "$@"
