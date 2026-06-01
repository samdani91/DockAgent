import json
import logging
import os
import queue as thread_queue
import threading
import traceback
from typing import AsyncGenerator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dockagent")

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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
