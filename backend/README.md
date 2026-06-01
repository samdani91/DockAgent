# DockAgent Backend

Minimal FastAPI backend for the DockAgent VS Code extension.

## Endpoints

- `GET /health` - health check
- `POST /chat` - accepts `{ "message": "..." }`
- `POST /pipeline/{module}` - accepts a pipeline module name

## Run

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```
