# DockAgent Backend

FastAPI backend for the DockAgent VS Code extension. Full setup, configuration
and troubleshooting are in the [root README](../README.md); this file is a quick
reference.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness check |
| `POST /chat` | Chat assistant |
| `POST /pipeline/generate` | Dockerfile generation (SSE) |
| `POST /pipeline/test` | Container test generation (SSE) |
| `POST /pipeline/flakiness` | Flakiness detection and repair (SSE) |
| `POST /pipeline/run` | Full coordinated pipeline (SSE) |

The four `/pipeline/*` endpoints stream Server-Sent Events, with a keepalive
every 15s so long silent stages do not trip client-side timeouts.

## Run

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then add your GEMINI_API_KEY
uvicorn main:app --host 127.0.0.1 --port 8000
```

Run from this directory — module imports resolve relative to it. Keep port 8000
unless you also change `_backendBaseUrl` in `../src/DockAgentPanel.ts`.

## Tests

```bash
pytest                  # 195 unit tests, <1s, no Docker or API key
pytest -m integration   # 48 integration tests, real Docker + model, ~5 min
```

## Modules

| Package | Paper |
|---|---|
| `dockerfile_generation/` | DRAFT (ICSE 2026) |
| `test_generation/` | Goto et al. (EASE 2025) — stages S0–S5 |
| `flakiness_repair/` | FLAKIDOCK (ICSE 2025) |
| `coordination/` | Unified feedback loop (this project's contribution) |
