#!/usr/bin/env python3
"""
dockgen: generate a Dockerfile with a local LLM, build it, and let the model
fix its own mistakes.
"""

import os
import re
import subprocess
import time
from pathlib import Path

import requests


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


# --- Prompt ------------------------------------------------------------------

# Docker best practices written as rules. This is the biggest lever on output
# quality, so changes to it should be deliberate and benchmarked.
SYSTEM_PROMPT = """You are a senior DevOps engineer who writes production-quality Dockerfiles.
Rules:
- Use an official, slim or alpine base image with a pinned, currently supported version tag (never :latest).
- Copy dependency manifests first and install dependencies BEFORE copying the rest of the source, so layer caching works.
- Use a multi-stage build for compiled languages (Go, Java, Rust) so the final image has no compiler.
- Run the app as a non-root user.
- EXPOSE the port the app listens on and set a CMD that starts the app.
- Do not invent files that are not in the project.
Reply with ONLY the Dockerfile inside one ```dockerfile code block. No explanation."""


# --- Model -------------------------------------------------------------------

def first_messages(context: str) -> list[dict]:
    """Open the conversation with the rules and the project description."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Write a Dockerfile for this project.\n\n" + context},
    ]


def ask_model(model: str, messages: list[dict]) -> tuple[str, dict]:
    """Send the conversation to Ollama.

    Returns the reply text and timing stats: total seconds (including model
    load), tokens generated and generation speed in tokens per second.
    """
    start = time.time()
    resp = requests.post(f"{OLLAMA_URL}/api/chat", json={
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": TEMPERATURE, "num_ctx": NUM_CTX},
    }, timeout=600)
    if resp.status_code != 200:
        # Ollama's own message is the useful part: unknown model, out of memory, ...
        raise RuntimeError(f"Ollama returned an error ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    tokens = data.get("eval_count", 0)
    gen_seconds = data.get("eval_duration", 0) / 1e9  # reported in nanoseconds
    stats = {
        "seconds": round(time.time() - start, 1),
        "tokens": tokens,
        "tok_per_s": round(tokens / gen_seconds, 1) if gen_seconds else 0.0,
    }
    return data["message"]["content"], stats


# --- Reply parsing -----------------------------------------------------------

# A fenced code block with any (or no) language tag; non-greedy, so each block
# is matched on its own.
FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
# A line starting with FROM, the one instruction every Dockerfile has.
FROM_RE = re.compile(r"^\s*FROM\s", re.MULTILINE | re.IGNORECASE)


def extract_dockerfile(reply: str) -> str:
    """Pull the Dockerfile out of a model reply.

    Small models wrap the answer in prose or include other files, so this
    takes the first fenced block that contains a FROM line, falls back to
    everything from the first FROM, and as a last resort returns the reply
    as-is and lets `docker build` report what's wrong with it.
    """
    for block in FENCE_RE.findall(reply):
        if FROM_RE.search(block):
            return block.strip() + "\n"
    match = FROM_RE.search(reply)
    if match:
        return reply[match.start():].strip() + "\n"
    return reply.strip() + "\n"


# --- Docker ------------------------------------------------------------------

def docker(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a short docker command and capture its output as text."""
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def docker_ready() -> bool:
    """Check the Docker engine answers, so a broken Docker isn't blamed on the model."""
    # A frozen engine never replies, so a hang counts as "not ready".
    try:
        return docker("info", timeout=20).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def docker_build(project: Path, dockerfile: Path, tag: str,
                 timeout: int = 900) -> tuple[bool, str, float]:
    """Build an image from the given Dockerfile.

    Returns whether the build succeeded, the full build log and the seconds it took.
    """
    start = time.time()
    try:
        # --progress=plain gives a line-by-line log the model can read, and the
        # explicit UTF-8 decoding avoids crashes on Windows' default code page.
        proc = subprocess.run(
            ["docker", "build", "--progress=plain", "-f", str(dockerfile), "-t", tag, str(project)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"docker build timed out after {timeout}s", round(time.time() - start, 1)
    # BuildKit writes most of its output to stderr.
    return proc.returncode == 0, proc.stdout + proc.stderr, round(time.time() - start, 1)


def smoke_test(tag: str, wait: int = 5) -> tuple[bool, str]:
    """Start the image and check the container is still running after `wait` seconds.

    A Dockerfile can build and still be broken (a CMD pointing at a missing
    file, a USER that doesn't exist), and those containers exit almost at once.
    Returns whether it stayed up and, if it stopped, the container's logs.
    """
    started = docker("run", "-d", tag)
    if started.returncode != 0:
        return False, started.stderr.strip()
    container = started.stdout.strip()
    try:
        time.sleep(wait)
        running = docker("inspect", "-f", "{{.State.Running}}", container).stdout.strip() == "true"
        if running:
            return True, ""
        logs = docker("logs", container)
        return False, (logs.stdout + logs.stderr).strip()
    finally:
        # Always remove the container, including a healthy one that is still serving.
        docker("rm", "-f", container)


# --- Generate, build, retry --------------------------------------------------

def last_lines(text: str, n: int = ERROR_TAIL_LINES) -> str:
    """Return the last n lines of a log, where docker build reports the real error."""
    return "\n".join(text.splitlines()[-n:])


def generate(project: Path, model: str = DEFAULT_MODEL, max_attempts: int = MAX_ATTEMPTS) -> dict:
    """Ask for a Dockerfile, build it, and feed failures back until it builds.

    The whole exchange stays in one conversation, so on each retry the model
    sees its previous attempt next to the error it caused. Returns a summary
    with success, attempts, total model and build seconds, and the image tag.
    """
    dockerfile = project / OUTPUT_NAME
    tag = f"dockgen/{project.resolve().name.lower()}:test"  # image names must be lowercase
    messages = first_messages(build_context(project))
    result = {"success": False, "attempts": 0, "llm_seconds": 0.0, "build_seconds": 0.0, "tag": tag}

    for attempt in range(1, max_attempts + 1):
        result["attempts"] = attempt
        print(f"[{attempt}/{max_attempts}] asking {model} ...")
        reply, stats = ask_model(model, messages)
        result["llm_seconds"] += stats["seconds"]
        print(f"    model: {stats['seconds']}s, {stats['tokens']} tokens, {stats['tok_per_s']} tok/s")
        dockerfile.write_text(extract_dockerfile(reply))
        messages.append({"role": "assistant", "content": reply})

        print(f"    building {tag} ...")
        ok, log, build_seconds = docker_build(project, dockerfile, tag)
        result["build_seconds"] += build_seconds
        # If Docker itself has died, the failure says nothing about the Dockerfile.
        if not ok and not docker_ready():
            raise RuntimeError("Docker stopped responding during the build; restart Docker Desktop")
        if ok:
            print(f"    build OK in {build_seconds}s")
            result["success"] = True
            break
        if attempt < max_attempts:
            print("    build FAILED, sending the error back to the model")
            # Only the tail of the log: it holds the cause, and a full log can
            # overflow the context window.
            messages.append({"role": "user", "content":
                f"The docker build failed with this error:\n\n{last_lines(log)}\n\n"
                "Fix the Dockerfile. Reply with only the full corrected Dockerfile."})
        else:
            print("    build FAILED, no attempts left")
            print(last_lines(log, 10))

    result["llm_seconds"] = round(result["llm_seconds"], 1)
    result["build_seconds"] = round(result["build_seconds"], 1)
    return result
