#!/usr/bin/env python3
"""
dockgen: generate a Dockerfile with a local LLM, build it, and let the model
fix its own mistakes.
"""

import os
from pathlib import Path


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


# --- Language detection ------------------------------------------------------

# Dependency manifest -> language. Checked in order; the first match wins.
MANIFESTS = {
    "requirements.txt": "python", "pyproject.toml": "python", "Pipfile": "python",
    "package.json": "node", "go.mod": "go",
    "pom.xml": "java", "build.gradle": "java", "Cargo.toml": "rust",
}

# Common names for the file that starts the app. Only the first one found is
# shown to the model, because that's where the port and start command live.
ENTRYPOINTS = ["app.py", "main.py", "server.py", "manage.py",
               "server.js", "index.js", "app.js", "main.go"]


def detect_language(project: Path) -> str:
    """Guess the project's language from its dependency manifest."""
    for name, lang in MANIFESTS.items():
        if (project / name).exists():
            return lang
    return "unknown"


# --- Project context ---------------------------------------------------------

def file_tree(project: Path, max_files: int = 200) -> list[str]:
    """List the project's files as POSIX paths relative to its root."""
    files = []
    for root, dirs, names in os.walk(project):
        # Pruning in place stops os.walk from descending into skipped folders.
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for name in sorted(names):
            # Leave out our own output, or a rerun would show the model its last answer.
            if name == OUTPUT_NAME:
                continue
            files.append((Path(root) / name).relative_to(project).as_posix())
            if len(files) >= max_files:
                return files + ["... (more files not shown)"]
    return files


def read_snippet(path: Path, max_chars: int = MANIFEST_MAX_CHARS) -> str:
    """Read a text file, truncated so one large file can't flood the prompt."""
    text = path.read_text(errors="ignore").rstrip()
    if len(text) > max_chars:
        text = text[:max_chars] + "\n... (truncated)"
    return text


def build_context(project: Path) -> str:
    """Describe the project for the model: language, files, manifests, entry point."""
    lang = detect_language(project)
    parts = [f"Project name: {project.resolve().name}", f"Detected language: {lang}", "", "File tree:"]
    parts += [f"  {f}" for f in file_tree(project)]

    # Every manifest that exists, since a project can have more than one.
    for name in MANIFESTS:
        p = project / name
        if p.exists():
            parts += ["", f"--- {name} ---", read_snippet(p)]

    # Only the first entry point found.
    for name in ENTRYPOINTS:
        p = project / name
        if p.exists():
            parts += ["", f"--- {name} (entry point, first {ENTRYPOINT_MAX_LINES} lines) ---"]
            parts += p.read_text(errors="ignore").splitlines()[:ENTRYPOINT_MAX_LINES]
            break
    return "\n".join(parts)
