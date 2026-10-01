import json
import logging
import os
import queue as thread_queue
import threading
import traceback
from collections import OrderedDict
from pathlib import Path
from typing import AsyncGenerator

from dotenv import load_dotenv

load_dotenv()  # loads backend/.env into os.environ before anything reads env vars

from dockagent_logging import configure, get_logger

configure()   # honours DOCKAGENT_LOG_LEVEL, defaults to INFO
log = get_logger("api")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="DockAgent Backend", version="0.1.0")

#: Last completed run per workspace, so /chat can answer from real results.
#: Process-local and lost on restart — deliberately not persisted. Bounded so a
#: long-lived server does not accumulate states for every workspace it ever saw.
_last_state: "OrderedDict[str, object]" = OrderedDict()
_MAX_REMEMBERED_RUNS = 10


def _remember_run(workspace_path: str, state) -> None:
    _last_state.pop(workspace_path, None)      # re-insert so it counts as newest
    _last_state[workspace_path] = state
    while len(_last_state) > _MAX_REMEMBERED_RUNS:
        _last_state.popitem(last=False)


def _recall_run(workspace_path: str | None):
    """The named workspace's run, else the most recent one."""
    if workspace_path and workspace_path in _last_state:
        return _last_state[workspace_path]
    if workspace_path:
        return None                            # asked about a workspace we have not run
    return next(reversed(_last_state.values())) if _last_state else None

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
    workspace_path: str | None = None


class ChatResponse(BaseModel):
    reply: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """Answer a developer question using the most recent run as context."""
    from coordination.explain import answer_query
    from coordination.orchestrator import PipelineRequest
    from coordination.runners import make_llm

    state = _recall_run(request.workspace_path)

    llm = make_llm(PipelineRequest(workspace_path=request.workspace_path or "."))
    reply = answer_query(request.message, state, llm)
    return ChatResponse(reply=reply)



# ---------------------------------------------------------------------------
# Shared SSE plumbing
# ---------------------------------------------------------------------------

#: Emitted while a stage is working so the connection does not look idle.
#: Node's undici — which the extension\'s fetch uses — aborts a response body
#: after 300s without data, surfacing as "terminated". A long docker build or a
#: large test suite easily exceeds that without producing an event.
SSE_HEARTBEAT_SECONDS = 15


async def _sse_stream(
    event_queue: thread_queue.Queue,
    http_request: Request | None = None,
    cancel: threading.Event | None = None,
) -> AsyncGenerator[str, None]:
    """Drain *event_queue* to the client until the sentinel arrives.

    If the client disconnects — the panel is closed, the window reloads — the
    cancel event is set so the worker can stop between stages instead of
    carrying on with builds nobody is waiting for.

    A comment line is sent during quiet periods to keep the body alive; SSE
    clients ignore lines that are not `data:`.
    """
    import asyncio
    import time

    last_sent = time.monotonic()

    while True:
        try:
            event = event_queue.get_nowait()
        except thread_queue.Empty:
            if http_request is not None and await http_request.is_disconnected():
                if cancel is not None:
                    cancel.set()
                log.warning("client disconnected; signalling the run to stop")
                return
            if time.monotonic() - last_sent >= SSE_HEARTBEAT_SECONDS:
                last_sent = time.monotonic()
                yield ": keepalive\n\n"
            await asyncio.sleep(0.1)
            continue
        if event is None:
            return
        last_sent = time.monotonic()
        yield f"data: {json.dumps(event)}\n\n"

# ---------------------------------------------------------------------------
# Test-generation pipeline (SSE)
# ---------------------------------------------------------------------------

class TestPipelineRequest(BaseModel):
    dockerfile_path: str
    workspace_path: str
    threshold: float = 0.0
    execute: bool = True          # S5 — run the generated spec
    execute_timeout: int = 300


@app.post("/pipeline/test")
async def run_test_generation(request: TestPipelineRequest, http_request: Request) -> StreamingResponse:
    output_path = os.path.join(
        request.workspace_path, ".dockagent", "tests", "container-structure-test.yaml"
    )

    event_queue: thread_queue.Queue = thread_queue.Queue()
    cancel = threading.Event()

    def _pipeline_thread() -> None:
        try:
            from test_generation.pipeline import TestPipeline

            plog = get_logger("test")

            def progress(step: str, message: str) -> None:
                plog.info("%s", message)
                event_queue.put({"step": step, "message": message})

            result = TestPipeline().run(
                dockerfile_path=request.dockerfile_path,
                workspace_path=request.workspace_path,
                output_path=output_path,
                threshold=request.threshold,
                progress=progress,
                execute=request.execute,
                execute_timeout=request.execute_timeout,
            )

            done: dict = {
                "step": "done",
                "output_path": result.output_path,
                "results": None,
            }
            if result.test_run is not None:
                tr = result.test_run
                done["message"] = f"{tr.passed} of {tr.total} tests passed."
                done["results"] = {
                    "total": tr.total,
                    "passed": tr.passed,
                    "failed": tr.failed,
                    "cases": [
                        {"name": c.name, "passed": c.passed, "errors": c.errors}
                        for c in tr.results
                    ],
                    "raw_output": tr.raw_output,
                }
            else:
                done["message"] = f"Tests written to {result.output_path}"
                # A runner failure still yields a usable spec — say why, don't fail.
                if result.execution_error:
                    done["warning"] = (
                        f"Tests were generated but not run: {result.execution_error}"
                    )
            event_queue.put(done)
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

    return StreamingResponse(
        _sse_stream(event_queue, http_request, cancel),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# Dockerfile-generation pipeline (SSE)
# ---------------------------------------------------------------------------

class GenerateRequest(BaseModel):
    workspace_path: str
    doc_paths: list[str] = []   # if empty, auto-detected from workspace root
    max_attempts: int = 6
    provider: str = "gemini"          # "gemini" | "openai"
    model: str = Field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-flash-latest"))
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
async def run_dockerfile_generation(request: GenerateRequest, http_request: Request) -> StreamingResponse:
    event_queue: thread_queue.Queue = thread_queue.Queue()
    cancel = threading.Event()

    def _pipeline_thread() -> None:
        try:
            workspace = Path(request.workspace_path)

            plog = get_logger("generate")

            def progress(step: str, message: str) -> None:
                plog.info("%s", message)
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

    return StreamingResponse(
        _sse_stream(event_queue, http_request, cancel),
        media_type="text/event-stream",
    )


# ---------------------------------------------------------------------------
# Flakiness detection and repair (SSE)
# ---------------------------------------------------------------------------

class FlakinessRequest(BaseModel):
    workspace_path: str
    dockerfile_path: str | None = None   # defaults to <workspace>/Dockerfile
    iterations: int = 2                  # paper's n
    max_attempts: int = 3                # paper's T
    top_k: int = 3
    repair: bool = True                  # detect only when False
    apply: bool = False                  # overwrite the Dockerfile in place
    provider: str = "gemini"
    model: str = Field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-flash-latest"))
    build_timeout: int = 900


@app.post("/pipeline/flakiness")
async def run_flakiness_repair(request: FlakinessRequest, http_request: Request) -> StreamingResponse:
    event_queue: thread_queue.Queue = thread_queue.Queue()
    cancel = threading.Event()

    def _pipeline_thread() -> None:
        try:
            workspace = Path(request.workspace_path)

            plog = get_logger("flakiness")

            def progress(step: str, message: str) -> None:
                plog.info("%s", message)
                event_queue.put({"step": step, "message": message})

            # ── Validate ───────────────────────────────────────────────────
            progress("checking", "Checking workspace…")
            if not workspace.is_dir():
                raise ValueError(f"Workspace path does not exist: {workspace}")

            dockerfile_path = (
                Path(request.dockerfile_path) if request.dockerfile_path
                else workspace / "Dockerfile"
            )
            if not dockerfile_path.is_file():
                raise ValueError(
                    f"No Dockerfile found at {dockerfile_path}. Generate one first."
                )
            dockerfile = dockerfile_path.read_text(encoding="utf-8")

            from dockerfile_generation.build import RealDockerBuilder
            from flakiness_repair.detector import detect

            # Caching is what hides flakiness — it must be off.
            builder = RealDockerBuilder(timeout=request.build_timeout, no_cache=True)

            # ── Detect ─────────────────────────────────────────────────────
            progress(
                "detect",
                f"Building {request.iterations}× without cache — this takes a while.",
            )
            report = detect(
                dockerfile, str(workspace), builder,
                iterations=request.iterations, progress=progress,
            )

            done: dict = {
                "step": "done",
                "output_path": str(dockerfile_path),
                "verdict": report.verdict,
                "message": report.summary(),
                "detection": {
                    "verdict": report.verdict,
                    "iterations": report.iterations,
                    "successes": report.successes,
                    "failures": report.failures,
                    "is_flaky": report.is_flaky,
                    "failing_instruction": (
                        report.primary_error.dockerfile_error_line
                        if report.primary_error else ""
                    ),
                },
                "repair": None,
            }

            if not report.needs_repair or not request.repair:
                event_queue.put(done)
                return

            # ── Retrieve ───────────────────────────────────────────────────
            from dockerfile_generation.llm import GeminiClient, OpenAIClient
            from flakiness_repair.embed import default_embedder
            from flakiness_repair.knowledge import KnowledgeBase
            from flakiness_repair.loop import repair_flakiness

            provider = request.provider.lower()
            if provider == "openai":
                if not os.environ.get("OPENAI_API_KEY", "").strip():
                    raise ValueError("OPENAI_API_KEY is not set.")
                llm = OpenAIClient(model=request.model)
            else:
                if not os.environ.get("GEMINI_API_KEY", "").strip():
                    raise ValueError("GEMINI_API_KEY is not set.")
                llm = GeminiClient(model=request.model)

            knowledge = embedder = None
            try:
                embedder = default_embedder()
                knowledge = KnowledgeBase.load()
                knowledge.index(embedder, progress=progress)
            except Exception as exc:            # retrieval is optional
                log.warning("Retrieval unavailable: %s", exc)
                progress("retrieve", f"Retrieval unavailable ({exc}); continuing without examples.")
                knowledge = embedder = None

            # ── Repair ─────────────────────────────────────────────────────
            outcome = repair_flakiness(
                dockerfile=dockerfile,
                context_dir=str(workspace),
                report=report,
                builder=builder,
                llm=llm,
                knowledge=knowledge,
                embedder=embedder,
                iterations=request.iterations,
                max_attempts=request.max_attempts,
                top_k=request.top_k,
                progress=progress,
            )

            done["repair"] = {
                "success": outcome.success,
                "attempts": outcome.attempt_count,
                "message": outcome.message,
                "demonstrations": (
                    outcome.attempts[0].demonstration_ids if outcome.attempts else []
                ),
            }

            if outcome.success and outcome.dockerfile:
                if request.apply:
                    dockerfile_path.write_text(outcome.dockerfile, encoding="utf-8")
                    repaired_path = dockerfile_path
                else:
                    # Written alongside rather than over the user's file; the
                    # extension opens the two side by side.
                    out_dir = workspace / ".dockagent"
                    out_dir.mkdir(parents=True, exist_ok=True)
                    repaired_path = out_dir / "Dockerfile.repaired"
                    repaired_path.write_text(outcome.dockerfile, encoding="utf-8")

                done["repair"]["repaired_path"] = str(repaired_path)
                done["repair"]["applied"] = request.apply
                done["output_path"] = str(dockerfile_path)
                done["message"] = outcome.message
            else:
                done["message"] = outcome.message

            event_queue.put(done)

        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Flakiness pipeline failed:\n%s", tb)
            event_queue.put({
                "step": "error",
                "message": f"{type(exc).__name__}: {exc}",
            })
        finally:
            event_queue.put(None)  # sentinel

    thread = threading.Thread(target=_pipeline_thread, daemon=True)
    thread.start()

    return StreamingResponse(
        _sse_stream(event_queue, http_request, cancel),
        media_type="text/event-stream",
    )




if __name__ == "__main__":
    import uvicorn  # type: ignore[import]
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)


# ---------------------------------------------------------------------------
# Agentic coordination — full pipeline with the unified feedback loop (SSE)
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    workspace_path: str
    threshold: float = 0.0
    iterations: int = 2                 # flakiness builds per check
    max_attempts: int = 6               # Module 1 build/repair cap
    max_feedback_rounds: int = 2        # test-failure rounds routed back
    provider: str = "gemini"
    model: str = Field(default_factory=lambda: os.environ.get("GEMINI_MODEL", "gemini-flash-latest"))
    optimize: bool = False
    apply: bool = False                 # apply a flakiness repair in place
    build_timeout: int = 900
    execute_timeout: int = 300


@app.post("/pipeline/run")
async def run_full_pipeline(request: RunRequest, http_request: Request) -> StreamingResponse:
    """Run Modules 1-3 under agent coordination, closing the feedback loop."""
    event_queue: thread_queue.Queue = thread_queue.Queue()
    cancel = threading.Event()

    def _pipeline_thread() -> None:
        try:
            from coordination.orchestrator import PipelineRequest, run_pipeline

            loggers = {
                "generate": get_logger("generate"),
                "test": get_logger("test"),
                "flakiness": get_logger("flakiness"),
                "agent": get_logger("agent"),
            }

            def progress(stage: str, step: str, message: str) -> None:
                loggers.get(stage, log).info("%s", message)
                event_queue.put({"stage": stage, "step": step, "message": message})

            state = run_pipeline(
                PipelineRequest(
                    workspace_path=request.workspace_path,
                    threshold=request.threshold,
                    iterations=request.iterations,
                    max_attempts=request.max_attempts,
                    max_feedback_rounds=request.max_feedback_rounds,
                    provider=request.provider,
                    model=request.model,
                    optimize=request.optimize,
                    apply=request.apply,
                    build_timeout=request.build_timeout,
                    execute_timeout=request.execute_timeout,
                ),
                progress,
                cancelled=cancel.is_set,
            )

            # Remembered so /chat can answer questions about this run.
            _remember_run(request.workspace_path, state)

            closing = next(
                (r.message for r in reversed(state.history)
                 if r.stage == "agent" and r.outcome == "success"),
                "Pipeline finished.",
            )
            event_queue.put({
                "stage": "agent",
                "step": "done",
                "message": closing,
                "output_path": state.dockerfile_path,
                "state": state.to_dict(),
            })
        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Coordinated run failed:\n%s", tb)
            event_queue.put({
                "stage": "agent",
                "step": "error",
                "message": f"{type(exc).__name__}: {exc}",
            })
        finally:
            event_queue.put(None)  # sentinel

    thread = threading.Thread(target=_pipeline_thread, daemon=True)
    thread.start()

    return StreamingResponse(
        _sse_stream(event_queue, http_request, cancel),
        media_type="text/event-stream",
    )
