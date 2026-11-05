"""FastAPI app: POST /ingest, POST /chat, the Component Lab endpoints, and serves the frontend at /."""

import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from fastapi import FastAPI, File, Form, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import browser, component, llm  # noqa: E402
from .prompts import SUMMARY_REQUEST  # noqa: E402
from .scraper import ScrapeError, scrape_site  # noqa: E402



@asynccontextmanager
async def lifespan(_app):
    yield
    await browser.shutdown()  # close headless Chromium if Component Lab started it


app = FastAPI(title="Website Q&A demo", lifespan=lifespan)
SITES: dict[str, dict] = {}  # site_id -> scraped pages (in memory; cleared on restart)
FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


class IngestRequest(BaseModel):
    url: str


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    site_id: str
    history: list[Message]
    question: str


def _page_status(site: dict) -> dict:
    return {
        key: ({"url": p["url"], "title": p["title"], "chars": len(p["markdown"]), "truncated": p["truncated"]} if p else None)
        for key, p in site.items()
    }


@app.get("/")
def index():
    return FileResponse(FRONTEND)


@app.post("/ingest")
async def ingest(req: IngestRequest):
    try:
        site = await scrape_site(req.url.strip())
    except ScrapeError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        summary, suggestions = await llm.summarize(site)
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))

    site_id = uuid.uuid4().hex
    SITES[site_id] = site
    return {
        "site_id": site_id,
        "pages": _page_status(site),
        "summary_request": SUMMARY_REQUEST,
        "summary": summary,
        "suggestions": suggestions,
    }


@app.post("/chat")
async def chat(req: ChatRequest):
    site = SITES.get(req.site_id)
    if not site:
        raise HTTPException(status_code=404, detail="This site session expired (server restarted). Load the website again.")
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question is empty.")

    try:
        answer, suggestions = await llm.chat(site, [m.model_dump() for m in req.history], req.question.strip())
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"answer": answer, "suggestions": suggestions}


# ---------------- Component Lab ----------------

MAX_UPLOAD = 15 * 1024 * 1024


class SelectRequest(BaseModel):
    job_id: str
    box: dict[str, float]  # x, y, w, h in page pixels


class BuildRequest(BaseModel):
    job_id: str


class EditRequest(BaseModel):
    code: str
    params: list[dict]
    instruction: str
    context: str = ""


def _job(job_id: str) -> dict:
    job = browser.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="This component session expired. Upload the screenshot again.")
    return job


@app.post("/component/locate")
async def component_locate(site_id: str = Form(...), screenshot: UploadFile = File(...)):
    site = SITES.get(site_id)
    if not site:
        raise HTTPException(status_code=404, detail="This site session expired (server restarted). Load the website again.")
    raw = await screenshot.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="Screenshot is too large (max 15 MB).")
    try:
        user_jpeg = browser.normalize_upload(raw)
        job = await browser.open_job(site["homepage"]["url"])
    except browser.BrowserError as e:
        raise HTTPException(status_code=422, detail=str(e))
    job["user_jpeg"] = user_jpeg
    try:
        match = await component.locate(job, user_jpeg)
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {
        "job_id": job["id"],
        "match": match,
        "preview": {"page_width": browser.VIEWPORT["width"], "slice_height": browser.SLICE_H,
                    "slices": [component.data_url(s["jpeg"]) for s in job["slices"]]},
    }


@app.post("/component/select")
async def component_select(req: SelectRequest):
    job = _job(req.job_id)
    if not {"x", "y", "w", "h"} <= req.box.keys() or req.box["w"] < 4 or req.box["h"] < 4:
        raise HTTPException(status_code=400, detail="Draw a box around the component.")
    try:
        return {"match": await component.select_box(job, req.box)}
    except llm.LLMError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/component/build")
async def component_build(req: BuildRequest):
    job = _job(req.job_id)
    if not job.get("selection"):
        raise HTTPException(status_code=400, detail="Pick a component first.")

    async def events():  # newline-delimited JSON progress events
        async for event in component.build(job, job["user_jpeg"]):
            yield json.dumps(event) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/component/edit")
async def component_edit(req: EditRequest):
    if not req.instruction.strip():
        raise HTTPException(status_code=400, detail="Describe the change you want.")
    try:
        return await component.edit(req.code, req.params, req.instruction.strip(), req.context)
    except llm.LLMError as e:
        raise HTTPException(status_code=502, detail=str(e))
