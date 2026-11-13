#!/usr/bin/env bash
# bench.sh: run dockgen on every sample with every model and collect results.csv

set -u

MODELS="${MODELS:-qwen2.5-coder:3b qwen2.5-coder:7b}"
RESULTS="results.csv"
PAUSE=10   # seconds between runs, so memory can settle

cd "$(dirname "$0")" || exit 1

# The project's virtual environment: Scripts/ on Windows, bin/ on Linux and Mac
if [ -x .venv/Scripts/python ]; then PY=.venv/Scripts/python; else PY=.venv/bin/python; fi

# Stop early if Ollama or Docker is down (timeout, because a frozen Docker never answers)
curl -sf "${OLLAMA_URL:-http://localhost:11434}/api/version" >/dev/null \
  || { echo "Ollama is not running"; exit 1; }
timeout 20 docker info >/dev/null 2>&1 \
  || { echo "Docker is not responding; restart Docker Desktop"; exit 1; }

# Keep earlier results in a dated file instead of mixing them into this run
if [ -f "$RESULTS" ]; then
  mv "$RESULTS" "results-$(date +%Y%m%d-%H%M%S).csv"
fi

for model in $MODELS; do
  for sample in samples/*/; do
    echo "=== $model on $sample ==="
    "$PY" dockgen.py "$sample" --model "$model" --run --csv "$RESULTS"
    sleep "$PAUSE"
  done
  ollama stop "$model" >/dev/null 2>&1   # free this model's memory before the next one
done

docker image prune -f >/dev/null   # remove layers left behind by rebuilt images
echo
echo "Done. Results in $RESULTS:"
cat "$RESULTS"