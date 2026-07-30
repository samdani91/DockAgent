"""All LLM prompt templates in one place — tune here, import everywhere."""

SYSTEM_GENERATION = (
    "You are an expert DevOps engineer. "
    "Output ONLY a valid Dockerfile — no prose, no markdown fences, no explanation outside the file. "
    "Add concise inline comments (lines starting with #) to explain each stage and non-obvious instruction."
)

# Initial generation — directive about using scanned facts.
# {context} contains both build docs and auto-scanned project facts (entry point, port, etc.)
INITIAL_GENERATION_USER = """\
Generate a production-ready Dockerfile for the following project.
Use ONLY the concrete facts provided below — do NOT guess or invent ports, \
entry points, or commands.

{context}

Critical requirements:
- CMD must invoke the exact entry point / start command listed in the facts above.
- EXPOSE must use the exact application port listed in the facts above.
- Any HEALTHCHECK must target that same port and an existing route (e.g. "/" or "/health").
- Do not add a build step unless the project has one (e.g. TypeScript compile, webpack).
- Add a comment above every stage (e.g. "# ── Stage 1: install dependencies ──").
- Add a short comment above each logical group of instructions explaining what it does.
- Output ONLY the Dockerfile, no markdown fences, no explanation outside comments."""

# Verbatim repair prompt from the paper (DRAFT, Lyu et al., ICSE 2026).
# {scan_hint} is either empty or a compact "do not change these facts" block.
REPAIR_USER = """\
{scan_hint}\
{dockerfile}
The above content is my Dockerfile. When I was building the image, I \
encountered the following error message: '{error}'. Please analyze the above \
information and generate a new Dockerfile for me. \
Preserve or improve existing comments, and add comments above any new instructions. \
Output ONLY the Dockerfile."""

# Context-driven variant: prepend project docs + scan so the model can consult them.
CONTEXT_REPAIR_USER = """\
Project context (authoritative — use these facts when fixing the Dockerfile):
{context}

{dockerfile}
The above content is my Dockerfile. When I was building the image, I \
encountered the following error message: '{error}'. Please analyze the above \
information and generate a new Dockerfile for me. \
Preserve or improve existing comments, and add comments above any new instructions. \
Output ONLY the Dockerfile."""

LLM_LOCALIZE_SYSTEM = "You are a build-log analyzer. Be terse and precise."

LLM_LOCALIZE_USER = """\
The following is the tail of a Docker build log:

{log_tail}

Identify and return ONLY the single most important error line from the log. \
Output just the error text, nothing else."""

LLM_SELECT_SYSTEM = "You are a Dockerfile expert. Reply with only a single digit."

LLM_SELECT_USER = """\
You are reviewing {n} candidate Dockerfiles to fix the error: '{error}'

{candidates}

Which candidate is most likely to fix the error? \
Reply with only the candidate number (1, 2, or 3)."""
