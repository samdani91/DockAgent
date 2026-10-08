# DockAgent

**An LLM-Driven VS Code Extension for Automated Dockerfile Generation, Testing, and Flakiness Repair**

---

DockAgent is a VS Code extension that automates the full Docker environment lifecycle for any software repository. Given a GitHub project, DockAgent can automatically generate a working Dockerfile, create tests to verify the container environment is correct, and detect and repair builds that break over time — all driven by Large Language Models working in a continuous feedback loop.

The three modules can be used independently or run together as a single one-click pipeline. A built-in chat assistant (modelled after GitHub Copilot Chat) explains errors in plain language, suggests fixes inline, and answers questions about the Docker setup without leaving the editor.

---

## The Problem

Setting up Docker environments is manual, error-prone, and fragile in three distinct ways:

- **Writing the Dockerfile** requires knowing the exact base image, package manager commands, build tools, and entry points for every project. Henkel et al. found that 26% of Dockerfiles on GitHub fail to build.
- **Verifying correctness** is overlooked — a Dockerfile that builds successfully can still produce the wrong environment: wrong library version, missing binary, misconfigured entry point.
- **Builds break over time** without any change to the Dockerfile itself, due to dependency updates, deprecated packages, or base image changes. Shabani et al. found 9.81% of monitored Dockerfiles exhibit this flaky behaviour.

DockAgent addresses all three problems in a single integrated tool.

---

## Modules

| Module | What it does |
|---|---|
| **Dockerfile Generation** | Reads the repository context (README, source files, dependency manifests), uses an LLM to generate a Dockerfile, then iteratively patches it based on build errors until the build succeeds. Also optimises the final image size. |
| **Test Generation** | Analyses the built Docker image's layer structure, scores files by importance, and automatically generates Container Structure Tests (CST) to verify that files, binaries, and metadata are correctly configured in the container. |
| **Flakiness Detection & Repair** | Runs repeated builds to detect flaky behaviour, retrieves semantically similar past repairs using RAG, and uses an LLM feedback loop to generate and validate patches until the build is stable. |
| **Chat Assistant** | A VS Code sidebar panel that receives errors and reports from all three modules and responds to developer questions in natural language. |

The key architectural contribution of DockAgent is its **Unified Feedback Loop**: test failures from the test generation module and flakiness reports from the repair module are fed back as patch instructions into the generation module, creating a continuous correctness cycle that none of the reference papers implement end-to-end.

---

## Requirements

| | Minimum | Verified on |
|---|---|---|
| Python | 3.11 | 3.14.4 |
| Node.js | 18 | 24.13.0 |
| npm | 9 | 11.8.0 |
| Docker Engine | 24 | 29.4.1 |
| VS Code | 1.75 | — |

**Docker must be running and usable without `sudo`.** Every module builds real images, so verify this first:

```bash
docker run --rm hello-world
```

If that needs `sudo`, add yourself to the `docker` group (`sudo usermod -aG docker $USER`, then log out and back in).

You also need an **LLM API key** — a free Google AI Studio key is enough: https://aistudio.google.com/apikey

The Test Generation module runs its suites with [container-structure-test](https://github.com/GoogleContainerTools/container-structure-test). You do **not** need to install it: if the `container-structure-test` binary is not on `PATH`, DockAgent falls back to `gcr.io/gcp-runtimes/container-structure-test:latest` and pulls it automatically on first use.

---

## Setup

### 1. Clone

```bash
git clone https://github.com/<your-username>/DockAgent.git
cd DockAgent
```

### 2. Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create your `.env` from the tracked template and add your key:

```bash
cp .env.example .env
```

```ini
# backend/.env
GEMINI_API_KEY=AQ.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GEMINI_MODEL=gemini-flash-latest
```

`.env` is gitignored — never commit a real key. `backend/main.py` loads it at import time, so the key does not need to be exported into your shell.

### 3. Start the backend

```bash
# from the backend/ directory, with the venv active
uvicorn main:app --host 127.0.0.1 --port 8000
```

Two things matter here:

- **Run it from `backend/`.** Module imports resolve relative to that directory.
- **Keep port 8000.** The extension points at `http://127.0.0.1:8000` (`src/DockAgentPanel.ts`); changing the port means changing that constant too.

Confirm it is up:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

Add `--reload` during development, but note it restarts the server when `logs/` is written — leave it off for long pipeline runs.

### 4. Extension

In a second terminal, from the repository root:

```bash
npm install
npm run compile
```

Then launch the Extension Development Host. A fresh clone has no `.vscode/` (it is gitignored), so create `.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Run Extension",
      "type": "extensionHost",
      "request": "launch",
      "args": ["--extensionDevelopmentPath=${workspaceFolder}"],
      "outFiles": ["${workspaceFolder}/out/**/*.js"],
      "preLaunchTask": "npm: compile"
    }
  ]
}
```

Press **F5**. A new VS Code window opens with DockAgent installed. Use `npm run watch` instead of `npm run compile` to recompile on save.

---

## Using DockAgent

In the Extension Development Host window:

1. **Open the project you want containerised** as the workspace folder (File → Open Folder). DockAgent operates on the open workspace, not on the DockAgent repository itself.
2. Click the **DockAgent** icon in the activity bar.
3. Pick an action:

| Action | Result |
|---|---|
| **Generate Dockerfile** | Writes a `Dockerfile` at the workspace root and iterates until it builds. |
| **Generate Tests** | Builds the image, then writes a CST spec and runs it. |
| **Repair Flakiness** | Repeated no-cache builds, then proposes a patch as a diff you accept or reject. |
| **Run Full Pipeline** | All three, with the unified feedback loop between them. |

The status strip above the chat box shows the live stage and elapsed time; progress survives closing and reopening the panel. The **Test score threshold** control trades suite size for focus — a threshold of `0` keeps thousands of base-image targets, so raise it if the suite is slow.

Everything streams over SSE, so the panel updates while a build runs.

### HTTP API

The modules are plain HTTP endpoints if you want to drive them without the extension:

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness check |
| `POST /chat` | Chat assistant |
| `POST /pipeline/generate` | Dockerfile generation (SSE) |
| `POST /pipeline/test` | Container test generation (SSE) |
| `POST /pipeline/flakiness` | Flakiness detection and repair (SSE) |
| `POST /pipeline/run` | Full coordinated pipeline (SSE) |

---

## Running the tests

From `backend/`, with the venv active:

```bash
pytest                  # 195 unit tests, <1s, no Docker or API key needed
pytest -m integration   # 48 integration tests, real Docker + real model, ~5 min
```

The default `pytest` run deselects integration tests (configured in `pyproject.toml`). The integration suite builds real images against real sample projects and skips automatically when Docker or an API key is unavailable. Together they cover the documented test cases T1–T15.

---

## Configuration reference

All backend configuration is environment variables, read from `backend/.env` or the process environment.

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | — | Google AI Studio key. Required unless using OpenAI. |
| `GEMINI_MODEL` | `gemini-flash-latest` | Benchmarked as matching `gemini-2.5-pro` on repair correctness at 3–8× the speed. |
| `OPENAI_API_KEY` | — | Alternative provider. |
| `DOCKAGENT_LOG_LEVEL` | `INFO` | Console verbosity. `DEBUG` shows per-file probe detail. |
| `DOCKAGENT_FILE_LOG_LEVEL` | `DEBUG` | Verbosity of the log file. |
| `DOCKAGENT_LOG_FILE` | `backend/logs/dockagent.log` | Rolling log, 5 MB × 5 files. Set to `off` to disable file logging. |

---

## Troubleshooting

**`GEMINI_API_KEY is not set`** — `.env` is missing, in the wrong directory, or the backend was started from somewhere other than `backend/`.

**Extension shows nothing / requests fail** — the backend is not running on port 8000. Check `curl http://127.0.0.1:8000/health`.

**`⚠ terminated` during a long run** — Node's HTTP client drops a response after 300s of silence. The backend sends SSE keepalives every 15s to prevent this; if you see it, the backend process has most likely died. Check `backend/logs/dockagent.log`.

**Test generation is very slow** — the score threshold is too low. Each command test is a container exec; a threshold of `0` can keep thousands of targets. Raise it.

**`permission denied` on the Docker socket** — see the Docker note under [Requirements](#requirements).

**Images accumulating** — the test pipeline tags one image per workspace per run. Clean up with `docker image prune -f` and `docker rmi $(docker images -q 'dockagent-*')`.

---

## Project layout

```
DockAgent/
├── src/                          # VS Code extension (TypeScript)
│   ├── extension.ts              # activation, command registration
│   └── DockAgentPanel.ts         # webview panel, SSE client, status strip
├── backend/
│   ├── main.py                   # FastAPI app, SSE endpoints
│   ├── dockagent_logging.py      # console + rotating file logging
│   ├── dockerfile_generation/    # Module 1 (DRAFT)
│   ├── test_generation/          # Module 2 (Goto et al.) — S0–S5
│   ├── flakiness_repair/         # Module 3 (FLAKIDOCK)
│   ├── coordination/             # Module 4 — unified feedback loop
│   └── tests/                    # unit tests + tests/integration/
└── resources/                    # extension icon
```

---

## Academic Foundation

DockAgent is built on top of three peer-reviewed research papers:

**[1] Toward Automated Test Generation for Dockerfiles Based on Analysis of Docker Image Layers**
Yuki Goto, Shinsuke Matsumoto, Shinji Kusumoto
*Evaluation and Assessment in Software Engineering (EASE '25), June 2025, Istanbul, Turkey*
https://doi.org/10.1145/3756681.3757020
→ Basis for the Test Generation module

**[2] Dockerfile Flakiness: Characterization and Repair**
Taha Shabani, Noor Nashid, Parsa Alian, Ali Mesbah
*IEEE/ACM International Conference on Software Engineering (ICSE), 2025*
*(FLAKIDOCK)*
→ Basis for the Flakiness Detection & Repair module

**[3] Automatic Dockerfile Generation with Large Language Models**
Lyu et al.
*IEEE/ACM International Conference on Software Engineering (ICSE), 2026*
*(DRAFT)*
https://conf.researchr.org/details/icse-2026/icse-2026-research-track/120
→ Basis for the Dockerfile Generation module

---

## Project Info

**Student:** A. M Samdani Mozumder (Roll: 1412)
**Supervisor:** Mridha Md. Nafis Fuad
**Programme:** B.Sc. in Software Engineering
**Institution:** Institute of Information Technology (IIT), University of Dhaka
**Course:** SPL-3, 2026
