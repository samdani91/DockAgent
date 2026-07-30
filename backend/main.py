import json
import logging
import os
import queue as thread_queue
import threading
import traceback
from pathlib import Path
from typing import AsyncGenerator

from dotenv import load_dotenv

load_dotenv()  # loads backend/.env into os.environ before anything reads env vars

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dockagent")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="DockAgent Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Existing endpoints (unchanged)
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    reply = (
        f'DockAgent backend received: "{request.message}". '
        "This is a starter backend; wire your real logic here."
    )
    return ChatResponse(reply=reply)


# ---------------------------------------------------------------------------
# Test-generation pipeline (SSE)
# ---------------------------------------------------------------------------

class TestPipelineRequest(BaseModel):
    dockerfile_path: str
    workspace_path: str
    threshold: float = 0.0


@app.post("/pipeline/test")
async def run_test_generation(request: TestPipelineRequest) -> StreamingResponse:
    output_path = os.path.join(
        request.workspace_path, ".dockagent", "tests", "container-structure-test.yaml"
    )

    event_queue: thread_queue.Queue = thread_queue.Queue()

    def _pipeline_thread() -> None:
        try:
            from test_generation.pipeline import TestPipeline

            def progress(step: str, message: str) -> None:
                log.info("[%s] %s", step, message)
                event_queue.put({"step": step, "message": message})

            result_path = TestPipeline().run(
                dockerfile_path=request.dockerfile_path,
                workspace_path=request.workspace_path,
                output_path=output_path,
                threshold=request.threshold,
                progress=progress,
            )
            event_queue.put({
                "step": "done",
                "message": f"Tests written to {result_path}",
                "output_path": result_path,
            })
        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Pipeline failed:\n%s", tb)
            event_queue.put({
                "step": "error",
                "message": f"{type(exc).__name__}: {exc}\n\n{tb}",
            })
        finally:
            event_queue.put(None)  # sentinel

    thread = threading.Thread(target=_pipeline_thread, daemon=True)
    thread.start()

    async def event_stream() -> AsyncGenerator[str, None]:
        import asyncio
        while True:
            try:
                event = event_queue.get_nowait()
            except thread_queue.Empty:
                await asyncio.sleep(0.1)
                continue
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Dockerfile-generation pipeline (SSE)
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    workspace_path: str
    doc_paths: list[str] = []   # if empty, auto-detected from workspace root
    max_attempts: int = 6
    provider: str = "gemini"          # "gemini" | "openai"
    model: str = Field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-2.5-pro"))
    build_timeout: int = 900
    optimize: bool = False      # DRAFT Phase B — multi-stage image optimization


def _has_project_files(workspace: Path) -> bool:
    """Return True if there is at least one non-hidden file one level deep."""
    for item in workspace.iterdir():
        if item.name.startswith("."):
            continue
        if item.is_file():
            return True
        if item.is_dir():
            for sub in item.iterdir():
                if not sub.name.startswith(".") and sub.is_file():
                    return True
    return False


def _find_docs(workspace: Path) -> list[str]:
    """Return up to 2 build-doc paths found at the workspace root."""
    candidates = [
        "README.md", "README.rst", "README.txt", "README",
        "INSTALL.md", "INSTALL.rst", "INSTALL", "BUILDING.md",
    ]
    found: list[str] = []
    for name in candidates:
        p = workspace / name
        if p.exists() and len(found) < 2:
            found.append(str(p))
    return found


@app.post("/pipeline/generate")
async def run_dockerfile_generation(request: GenerateRequest) -> StreamingResponse:
    event_queue: thread_queue.Queue = thread_queue.Queue()

    def _pipeline_thread() -> None:
        try:
            workspace = Path(request.workspace_path)

            def progress(step: str, message: str) -> None:
                log.info("[%s] %s", step, message)
                event_queue.put({"step": step, "message": message})

            # ── Validate workspace ──────────────────────────────────────────
            progress("checking", "Checking workspace…")
            if not workspace.exists() or not workspace.is_dir():
                raise ValueError(f"Workspace path does not exist: {workspace}")

            if not _has_project_files(workspace):
                raise ValueError(
                    "No project files found in this folder. "
                    "Open a project with source files before generating a Dockerfile."
                )

            dockerfile_out = workspace / "Dockerfile"
            if dockerfile_out.exists():
                progress("checking", "Existing Dockerfile found — it will be replaced on success.")

            # ── Discover build docs ────────────────────────────────────────
            doc_paths = [
                str(workspace / d) if not os.path.isabs(d) else d
                for d in request.doc_paths
            ] or _find_docs(workspace)

            if doc_paths:
                progress("context", f"Reading {len(doc_paths)} build doc(s): {', '.join(Path(p).name for p in doc_paths)}")
            else:
                progress("context", "No build docs found — will generate from project structure alone.")

            from dockerfile_generation.context import build_context
            context = build_context(doc_paths, workspace)

            # ── Instantiate LLM ────────────────────────────────────────────
            from dockerfile_generation.build import RealDockerBuilder
            from dockerfile_generation.generate import generate_initial
            from dockerfile_generation.llm import GeminiClient, OpenAIClient
            from dockerfile_generation.loop import run_loop

            provider = request.provider.lower()
            if provider == "openai":
                if not os.environ.get("OPENAI_API_KEY", "").strip():
                    raise ValueError(
                        "OPENAI_API_KEY is not set. Export it before starting the backend."
                    )
                llm = OpenAIClient(model=request.model)
            else:
                if not os.environ.get("GEMINI_API_KEY", "").strip():
                    raise ValueError(
                        "GEMINI_API_KEY is not set. Export it before starting the backend."
                    )
                llm = GeminiClient(model=request.model)
            builder = RealDockerBuilder(timeout=request.build_timeout)

            # ── Initial generation ─────────────────────────────────────────
            progress("generating", "Asking LLM for initial Dockerfile…")
            initial = generate_initial(context, llm)

            # ── Repair loop ────────────────────────────────────────────────
            progress("building", f"Starting build/repair loop (max {request.max_attempts} attempts)…")

            result = run_loop(
                initial_dockerfile=initial,
                context=context,
                context_dir=str(workspace),
                builder=builder,
                llm=llm,
                max_attempts=request.max_attempts,
                on_attempt=lambda attempt, msg: progress("building", msg),
            )

            if result.success:
                final_dockerfile = result.dockerfile
                summary = f"Dockerfile generated successfully in {result.attempts} attempt(s)."

                # ── Phase B: optimization (optional) ───────────────────────
                if request.optimize:
                    from dockerfile_generation.optimize import optimize

                    progress("optimizing", "Phase B — optimizing image (multi-stage)…")
                    opt = optimize(
                        dockerfile=final_dockerfile,
                        context=context,
                        context_dir=str(workspace),
                        builder=builder,
                        llm=llm,
                        on_progress=lambda m: progress("optimizing", m),
                    )
                    if opt.success:
                        final_dockerfile = opt.dockerfile
                        if opt.reduction_pct is not None:
                            summary += (
                                f"\n\nOptimized (multi-stage): "
                                f"{opt.original_size / 1e6:.1f} MB → "
                                f"{opt.optimized_size / 1e6:.1f} MB "
                                f"(**{opt.reduction_pct:.1f}% smaller**)."
                            )
                        else:
                            summary += "\n\nOptimization applied (size unknown)."
                    else:
                        summary += f"\n\nOptimization skipped: {opt.note}"

                dockerfile_out.write_text(final_dockerfile, encoding="utf-8")
                event_queue.put({
                    "step": "done",
                    "message": f"{summary}\nWritten to `{dockerfile_out}`",
                    "output_path": str(dockerfile_out),
                })
            else:
                msg = f"Generation failed after {result.attempts} attempt(s)."
                if result.last_error:
                    msg += f"\n\nLast error:\n```\n{result.last_error}\n```"
                event_queue.put({"step": "error", "message": msg})

        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Generate pipeline failed:\n%s", tb)
            event_queue.put({
                "step": "error",
                "message": f"{type(exc).__name__}: {exc}",
            })
        finally:
            event_queue.put(None)  # sentinel

    thread = threading.Thread(target=_pipeline_thread, daemon=True)
    thread.start()

    async def event_stream() -> AsyncGenerator[str, None]:
        import asyncio
        while True:
            try:
                event = event_queue.get_nowait()
            except thread_queue.Empty:
                await asyncio.sleep(0.1)
                continue
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Generic pipeline stub (other modules still work)
# ---------------------------------------------------------------------------

class PipelineResponse(BaseModel):
    module: str
    status: str
    detail: str


@app.post("/pipeline/{module}", response_model=PipelineResponse)
def pipeline(module: str) -> PipelineResponse:
    return PipelineResponse(
        module=module,
        status="queued",
        detail=f"Pipeline module '{module}' was accepted by the backend stub.",
    )


if __name__ == "__main__":
    import uvicorn  # type: ignore[import]
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
