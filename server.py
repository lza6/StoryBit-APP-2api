"""StoryBit 2API gateway.

Exposes the reverse-engineered StoryBit v3.0.6 private API as:
  * an OpenAI-compatible image endpoint (/v1/images/generations, /v1/images/edits)
  * a plain REST catalog for templates / AI effects / banners
  * a job API for the async AI pipeline (gen_i + processStatus)

Run:
    python server.py                 # live upstream
    STORYBIT_MOCK=1 python server.py # offline mock (stress tests, demo without upstream)

Environment:
    PORT=8089  HOST=127.0.0.1
    STORYBIT_MOCK=1
    STORYBIT_MAX_CONCURRENCY=8   in-flight AI jobs
    STORYBIT_POLL_TIMEOUT=120    seconds to wait for a job before answering 202
"""
from __future__ import annotations

import asyncio
import base64
import io
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

import rc as rcmod
from storybit_client import Job, StoryBitClient, UpstreamError
from rc import Config

MOCK = os.getenv("STORYBIT_MOCK", "0") == "1"
MAX_CONCURRENCY = int(os.getenv("STORYBIT_MAX_CONCURRENCY", "8"))
POLL_TIMEOUT = float(os.getenv("STORYBIT_POLL_TIMEOUT", "120"))
EFFECT_TTL = 600.0
CATALOG_TTL = 300.0

STARTED = time.time()
cfg = Config()
client = StoryBitClient(cfg)
sem = asyncio.Semaphore(MAX_CONCURRENCY)
stats = {"requests": 0, "errors": 0, "jobs_submitted": 0, "jobs_completed": 0,
         "mock": MOCK, "upstream_down": 0}

app = FastAPI(title="StoryBit 2API", version="3.0.6-r1")


# --------------------------------------------------------------------- helpers

class TTLCache:
    def __init__(self, ttl: float):
        self.ttl = ttl
        self.value = None
        self.at = 0.0
        self.lock = asyncio.Lock()

    async def get(self, loader):
        if self.value is not None and time.time() - self.at < self.ttl:
            return self.value
        async with self.lock:
            if self.value is not None and time.time() - self.at < self.ttl:
                return self.value
            self.value = await loader()
            self.at = time.time()
            return self.value


_effects_cache = TTLCache(EFFECT_TTL)
_categories_cache = TTLCache(CATALOG_TTL)
_upscale_probe = TTLCache(120.0)


def _probe_jpeg() -> bytes:
    """Tiny valid JPEG used only to ask upstream whether upscale is alive."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (120, 120, 120)).save(buf, "JPEG")
    return buf.getvalue()

MOCK_EFFECTS = {
    "category_list": [
        {"_id": "", "category_name": "Ghibli", "category_type": 2, "effects": [
            {"_id": "mock-ghibli-1", "effect_name": "Barbie",
             "thumb_img_url": "https://aa.snovxgames.com:5001/effect_thumb/mock.jpg",
             "thumb_video_url": ""}]},
        {"_id": "", "category_name": "Anime", "category_type": 2, "effects": [
            {"_id": "mock-anime-1", "effect_name": "anime_8",
             "thumb_img_url": "https://aa.snovxgames.com:5001/effect_thumb/mock.jpg",
             "thumb_video_url": ""}]},
    ]
}


async def get_effects() -> dict:
    if MOCK:
        return MOCK_EFFECTS
    return await _effects_cache.get(client.ai_effects)


def flatten_effects(payload: dict) -> list[dict]:
    out: list[dict] = []
    for cat in payload.get("category_list") or []:
        for eff in cat.get("effects") or []:
            out.append({
                "effect_id": eff.get("_id"),
                "name": eff.get("effect_name"),
                "category": cat.get("category_name"),
                "category_type": cat.get("category_type"),
                "thumb_img_url": eff.get("thumb_img_url"),
                "thumb_video_url": eff.get("thumb_video_url"),
            })
    return out


def resolve_effect(effects: list[dict], query: str) -> dict | None:
    """`model`/`prompt` -> effect. Exact id, then exact name, then substring."""
    q = (query or "").strip()
    if not q:
        return None
    for eff in effects:
        if eff["effect_id"] == q:
            return eff
    low = q.lower()
    for eff in effects:
        if (eff["name"] or "").lower() == low:
            return eff
    for eff in effects:
        if low in (eff["name"] or "").lower() or low in (eff["category"] or "").lower():
            return eff
    return None


def decode_image(image: str | None, image_url: str | None) -> bytes:
    if image_url:
        try:
            r = httpx.get(image_url, timeout=30, follow_redirects=True,
                          headers={"User-Agent": "okhttp/4.12.0"})
            r.raise_for_status()
            return r.content
        except httpx.HTTPError as exc:
            raise HTTPException(400, f"cannot fetch image_url: {exc}") from exc
    if not image:
        raise HTTPException(400, "image (base64) or image_url is required")
    raw = image.split(",", 1)[1] if image.startswith("data:") else image
    try:
        return base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise HTTPException(400, "image is not valid base64") from exc


def image_size(data: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return (768, 768)


async def wait_job(job_id: str, ratio: str, timeout: float) -> Job:
    deadline = time.time() + timeout
    job = Job(job_id=job_id, state="unknown", done=False, url="")
    while time.time() < deadline:
        job = await client.job_status(job_id, ratio)
        if job.done or job.state not in ("waiting", "processing", "unknown"):
            return job
        await asyncio.sleep(4)
    return job


def mock_job(request_bytes: bytes, size: tuple[int, int]) -> tuple[dict, Job]:
    """Deterministic stand-in so the gateway/demo/bench stay exercisable while
    the upstream AI worker is in maintenance."""
    seed = int.from_bytes(request_bytes[:8].ljust(8, b"\0"), "big") % 360
    from PIL import Image, ImageDraw
    im = Image.new("RGB", size, (20, 24, 32))
    d = ImageDraw.Draw(im)
    for i in range(0, size[0], 24):
        d.line([(i, 0), (i, size[1])], fill=(30 + seed % 60, 40, 70))
    d.ellipse((size[0] // 4, size[1] // 4, size[0] * 3 // 4, size[1] * 3 // 4),
              fill=(250, 200, 90))
    d.text((size[0] // 2 - 40, size[1] // 2), "STORYBIT MOCK", fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode()
    job = Job(job_id=f"mock-{seed}", state="completed", done=True, url=f"data:image/jpeg;base64,{b64}")
    return {"status": True, "job_id": job.job_id}, job


def upstream_failure(exc: Exception) -> HTTPException:
    stats["errors"] += 1
    if isinstance(exc, UpstreamError):
        return HTTPException(502, {"error": {"type": "upstream_error",
                                             "code": exc.code, "message": exc.message}})
    return HTTPException(502, {"error": {"type": "network_error", "message": str(exc)}})


# ---------------------------------------------------------------------- models

class ImageGenRequest(BaseModel):
    model: str = Field("storybit", description="storybit | storybit/<effect name or id>")
    prompt: str = Field("", description="free text used to pick an effect when model has no suffix")
    image: str | None = Field(None, description="base64 input image (image-to-image app)")
    image_url: str | None = None
    effect_id: str | None = None
    size: str = "1:1"
    n: int = 1
    response_format: str = "url"  # url | b64_json
    poll_timeout: float = Field(POLL_TIMEOUT, ge=0, le=600)


class UpscaleRequest(BaseModel):
    image: str | None = None
    image_url: str | None = None
    effect_id: str = "6527ee586890ad39dc36e81a"
    size: str = "1:1"
    poll_timeout: float = Field(POLL_TIMEOUT, ge=0, le=600)


# --------------------------------------------------------------------- gateway

@app.on_event("startup")
async def _startup() -> None:
    if not MOCK:
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, cfg.refresh)
        print(f"[storybit] remote config: {'live' if ok else 'fallback'}; "
              f"api={cfg.api_url} ai={cfg.ai_url}")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await client.aclose()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    demo = Path(__file__).parent / "demo.html"
    if demo.exists():
        return demo.read_text(encoding="utf-8")
    return "<h1>StoryBit 2API</h1><p>demo.html not found</p>"


@app.get("/health")
async def health() -> dict:
    probe = {"upstream": "mock" if MOCK else "unknown"}
    if not MOCK:
        t0 = time.time()
        try:
            await client.version_update()
            probe = {"upstream": "ok", "latency_ms": int((time.time() - t0) * 1000)}
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            stats["upstream_down"] += 1
            probe = {"upstream": "down", "error": str(exc)[:200]}
    return {
        "status": "ok" if probe.get("upstream") in ("ok", "mock") else "degraded",
        "version": "3.0.6-r1",
        "uptime_s": int(time.time() - STARTED),
        "rc_source": cfg.source,
        "hosts": {"api": cfg.api_url, "ai": cfg.ai_url},
        **probe, **stats,
    }


@app.get("/v1/models")
async def models() -> dict:
    gen = stats.get("last_generate") or {}
    gen_status = "available" if gen.get("done") else ("degraded" if gen else "unverified")
    return {"object": "list", "data": [
        {"id": "storybit", "object": "model", "owned_by": "snovxgames",
         "status": gen_status, "last_job": gen or None,
         "description": "AI photo effect generation (image-to-image), effect chosen by prompt"},
        {"id": "storybit-upscale", "object": "model", "owned_by": "snovxgames",
         "status": "maintenance" if not stats.get("upscale_ok") else "available",
         "description": "server-side image upscale"},
    ]}


@app.get("/v1/capabilities")
async def capabilities() -> dict:
    """What actually works right now. Generation is judged from real job traffic
    (we never submit a throwaway job just to probe); upscale is probed directly,
    cached for 120 s because that call is cheap and stateless upstream."""
    if MOCK:
        return {"mode": "mock", "generate": {"state": "mock"},
                "upscale": {"state": "mock"}, "catalog": {"state": "mock"}}

    async def probe_upscale() -> dict:
        try:
            await client.upscale("6527ee586890ad39dc36e81a", _probe_jpeg(),
                                 width=64, height=64, file_name="probe.jpg")
            stats["upscale_ok"] = True
            return {"state": "available"}
        except UpstreamError as exc:
            stats["upscale_ok"] = False
            return {"state": "maintenance" if exc.code == "120" else "error",
                    "code": exc.code, "message": exc.message}
        except Exception as exc:  # noqa: BLE001
            return {"state": "unreachable", "message": str(exc)[:200]}

    upscale = await _upscale_probe.get(probe_upscale)
    gen = stats.get("last_generate") or {}
    gen_state = "available" if gen.get("done") else ("accepted-but-queued" if gen else "unverified")
    try:
        effects = flatten_effects(await get_effects())
        catalog = {"state": "available", "effects": len(effects),
                   "categories": len({e["category"] for e in effects})}
    except Exception as exc:  # noqa: BLE001
        catalog = {"state": "unreachable", "message": str(exc)[:200]}
    return {
        "generate": {"state": gen_state, "model": "storybit",
                     "last_job": gen or None,
                     "note": "gen_i 受理后 state 恒 waiting = 厂商 worker 下线"},
        "upscale": {"model": "storybit-upscale", **upscale},
        "catalog": catalog,
        "templates": {"state": "available", "categories": len(
            (await _categories_cache.get(client.categories)).get("categoryData") or [])},
        "rc_source": cfg.source,
    }


@app.get("/v1/effects/categories")
async def effect_categories() -> dict:
    try:
        payload = await get_effects()
    except Exception as exc:  # noqa: BLE001
        raise upstream_failure(exc) from exc
    cats = [{"name": c.get("category_name"), "type": c.get("category_type"),
             "count": len(c.get("effects") or [])} for c in payload.get("category_list") or []]
    return {"object": "list", "data": cats, "total": len(cats)}


@app.get("/v1/effects")
async def effect_list(category: str | None = Query(None), q: str | None = Query(None)) -> dict:
    try:
        effects = flatten_effects(await get_effects())
    except Exception as exc:  # noqa: BLE001
        raise upstream_failure(exc) from exc
    if category:
        effects = [e for e in effects if (e["category"] or "").lower() == category.lower()]
    if q:
        low = q.lower()
        effects = [e for e in effects if low in (e["name"] or "").lower()
                   or low in (e["category"] or "").lower()]
    return {"object": "list", "data": effects, "total": len(effects)}


@app.post("/v1/images/generations")
async def images_generations(req: ImageGenRequest) -> JSONResponse:
    stats["requests"] += 1
    target = req.model.split("/", 1)[1] if "/" in req.model else (req.prompt or "")
    raw = decode_image(req.image, req.image_url)
    size = image_size(raw)

    if req.effect_id:
        effect_id = req.effect_id
    else:
        try:
            effects = flatten_effects(await get_effects())
        except Exception as exc:  # noqa: BLE001
            raise upstream_failure(exc) from exc
        hit = resolve_effect(effects, target)
        if not hit:
            raise HTTPException(400, {"error": {
                "type": "invalid_request_error",
                "message": f"no effect matches '{target}'; use /v1/effects to browse"}})
        effect_id = hit["effect_id"]

    if MOCK:
        body, job = mock_job(raw, size)
    else:
        async with sem:
            try:
                body = await client.gen_image(effect_id, raw, width=size[0], height=size[1],
                                              ratio=req.size, file_name="image.jpg")
            except Exception as exc:  # noqa: BLE001
                raise upstream_failure(exc) from exc
            stats["jobs_submitted"] += 1
            job_id = str(body.get("job_id") or "")
            if not body.get("status") or not job_id:
                raise HTTPException(502, {"error": {"type": "upstream_error",
                                                    "message": "gen_i rejected the job",
                                                    "upstream": body}})
            job = await wait_job(job_id, req.size, req.poll_timeout)
            if job.done:
                stats["jobs_completed"] += 1

    stats["last_generate"] = {"job_id": job.job_id, "state": job.state,
                              "done": job.done, "effect_id": effect_id,
                              "at": int(time.time())}
    if not job.done:
        # upstream accepted the job but has not produced output yet
        return JSONResponse(status_code=202, content={
            "created": int(time.time()), "job_id": job.job_id, "state": job.state,
            "data": [], "message": "job accepted; poll /v1/jobs/{job_id}"})

    if req.response_format != "b64_json":
        item = {"url": job.url}
    elif job.url.startswith("data:"):
        item = {"b64_json": job.url.split(",", 1)[1]}
    elif job.url.startswith(("http://", "https://")):
        try:
            r = httpx.get(job.url, timeout=60, follow_redirects=True,
                          headers={"User-Agent": "okhttp/4.12.0"})
            r.raise_for_status()
            item = {"b64_json": base64.b64encode(r.content).decode()}
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"cannot download result: {exc}") from exc
    else:
        item = {"b64_json": job.url}
    return JSONResponse({"created": int(time.time()), "job_id": job.job_id,
                         "data": [item] * max(1, req.n)})


@app.post("/v1/images/edits")
async def images_edits(file: UploadFile = File(...), model: str = Form("storybit"),
                       prompt: str = Form(""), size: str = Form("1:1"),
                       response_format: str = Form("url")) -> JSONResponse:
    raw = await file.read()
    req = ImageGenRequest(model=model, prompt=prompt, size=size,
                          response_format=response_format,
                          image=base64.b64encode(raw).decode())
    return await images_generations(req)


@app.post("/v1/upscale")
async def upscale(req: UpscaleRequest) -> JSONResponse:
    stats["requests"] += 1
    raw = decode_image(req.image, req.image_url)
    size = image_size(raw)
    if MOCK:
        _, job = mock_job(raw, size)
    else:
        async with sem:
            try:
                body = await client.upscale(req.effect_id, raw, width=size[0], height=size[1],
                                            ratio=req.size)
            except Exception as exc:  # noqa: BLE001
                raise upstream_failure(exc) from exc
            job = Job(job_id=str(body.get("job_id") or ""), state="waiting", done=False, url="")
            if job.job_id:
                job = await wait_job(job.job_id, req.size, req.poll_timeout)
    if not job.done:
        return JSONResponse(status_code=202, content={"job_id": job.job_id, "state": job.state,
                                                      "data": []})
    return JSONResponse({"job_id": job.job_id, "data": [{"url": job.url}]})


@app.get("/v1/jobs/{job_id}")
async def job_detail(job_id: str, size: str = "1:1") -> dict:
    if MOCK:
        return {"job_id": job_id, "state": "completed", "done": True, "url": ""}
    try:
        job = await client.job_status(job_id, size)
    except Exception as exc:  # noqa: BLE001
        raise upstream_failure(exc) from exc
    return job.raw


@app.delete("/v1/jobs/{job_id}")
async def job_cancel(job_id: str) -> dict:
    if MOCK:
        return {"job_id": job_id, "cancelled": True}
    try:
        return await client.cancel_job(job_id)
    except Exception as exc:  # noqa: BLE001
        raise upstream_failure(exc) from exc


# --------------------------------------------------------------------- catalog

@app.get("/v1/categories")
async def categories() -> dict:
    if MOCK:
        return {"object": "list", "data": [{"id": "mock", "name": "Explore", "image": ""}]}
    try:
        payload = await _categories_cache.get(client.categories)
    except Exception as exc:  # noqa: BLE001
        raise upstream_failure(exc) from exc
    data = [{"id": c.get("_id"), "name": c.get("category_name"), "image": c.get("cat_img"),
             "explore": c.get("explore")} for c in payload.get("categoryData") or []]
    return {"object": "list", "data": data, "total": len(data)}


@app.get("/v1/templates/explore")
async def templates_explore() -> dict:
    if MOCK:
        return {"data": []}
    try:
        return await client.explore()
    except Exception as exc:  # noqa: BLE001
        raise upstream_failure(exc) from exc


@app.get("/v1/templates")
async def templates(cat_id: str = Query(...), page: int = 1, limit: int = 20) -> dict:
    if MOCK:
        return {"data": []}
    try:
        data = await client.templates_by_category(cat_id, page, limit)
    except Exception as exc:  # noqa: BLE001
        raise upstream_failure(exc) from exc
    return {"page": page, "limit": limit, "data": data.get("categoryData") or data.get("templates") or data}


@app.get("/v1/templates/{temp_id}")
async def template_detail(temp_id: str) -> dict:
    if MOCK:
        return {"data": {}}
    try:
        return await client.template_detail(temp_id)
    except Exception as exc:  # noqa: BLE001
        raise upstream_failure(exc) from exc


@app.get("/v1/banners")
async def banners() -> dict:
    if MOCK:
        return {"data": []}
    try:
        return {"dashboard": await client.dashboard_banners(), "more_apps": await client.more_apps()}
    except Exception as exc:  # noqa: BLE001
        raise upstream_failure(exc) from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"),
                port=int(os.getenv("PORT", "8089")), log_level="info")
