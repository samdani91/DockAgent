# Inventory API

A small inventory service built with FastAPI.

## Requirements

- Python 3.11
- Dependencies are listed in `requirements.txt`

## Build

Install the dependencies into the environment:

```bash
pip install --no-cache-dir -r requirements.txt
```

## Run

The service listens on port **8000** and its entry point is `main.py`,
exposing the ASGI application as `main:app`:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

## Endpoints

- `GET /health` — liveness probe
- `GET /items` — list inventory
- `GET /items/{item_id}` — fetch one item
