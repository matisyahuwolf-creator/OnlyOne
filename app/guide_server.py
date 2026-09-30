"""Only the guide, as its own small website: no Neo4j, no WhatsApp, no embeddings.

    pip install -r requirements-guide.txt
    uvicorn app.guide_server:app --host 0.0.0.0 --port 8000

Settings come from the environment (or .env): ANTHROPIC_API_KEY, CLAUDE_MODEL,
GUIDE_QUICK_MODEL, and either CHAT_ACCESS_CODE (people need the code) or
GUIDE_PUBLIC=1 (anyone with the link, under the ceilings in app/guide.py).
"""
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from . import guide, library

app = FastAPI(title="Only One", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(guide.router)
app.include_router(library.router)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/guide")


@app.get("/health")
def health() -> dict:
    return {"ok": guide.PAGE.exists(), "public": guide.PUBLIC}
