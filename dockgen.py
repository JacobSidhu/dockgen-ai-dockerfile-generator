#!/usr/bin/env python3
"""
dockgen: generate a Dockerfile with a local LLM, build it, and let the model
fix its own mistakes.
"""

import os


# --- Settings ----------------------------------------------------------------

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
DEFAULT_MODEL = "qwen2.5-coder:3b"    # fits in 8 GB of RAM; 7b needs more headroom
MAX_ATTEMPTS = 3                      # generate -> build rounds before giving up
OUTPUT_NAME = "Dockerfile.generated"  # never overwrite a hand-written Dockerfile

# A low temperature keeps runs repeatable, which matters when comparing models.
# Ollama silently drops the start of a prompt longer than num_ctx, and the
# start is where the rules are, so the window is set explicitly.
TEMPERATURE = 0.2
NUM_CTX = 8192

# Budgets that keep the prompt well inside NUM_CTX.
MANIFEST_MAX_CHARS = 3000
ENTRYPOINT_MAX_LINES = 60
ERROR_TAIL_LINES = 40

# Never part of the app's source, and big enough to swamp the file tree.
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build", "target"}
