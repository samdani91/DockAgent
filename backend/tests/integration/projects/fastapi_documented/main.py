"""A small but real FastAPI service, used as an integration fixture."""

from fastapi import FastAPI

app = FastAPI(title="Inventory API", version="1.0.0")

_ITEMS: dict[int, dict] = {1: {"id": 1, "name": "widget", "qty": 4}}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/items")
def list_items() -> list[dict]:
    return list(_ITEMS.values())


@app.get("/items/{item_id}")
def get_item(item_id: int) -> dict:
    return _ITEMS.get(item_id, {})
