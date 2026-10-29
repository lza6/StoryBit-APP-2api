"""Load/stress bench for the StoryBit 2API gateway.

    python bench.py                      # mock gateway on :8089, 3 rounds
    python bench.py --base http://127.0.0.1:8089 --levels 1,4,16 --total 600
    python bench.py --endpoint effects   # upstream read-only throughput

Percentiles are computed from real wall-clock samples; a round reports p50/p90/
p99, throughput and the error histogram (status code -> count).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import statistics
import time
from collections import Counter

import httpx

def sample_image(size: int = 512) -> str:
    from PIL import Image
    im = Image.new("RGB", (size, size), (40, 70, 120))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


async def one(client: httpx.AsyncClient, base: str, endpoint: str, img: str) -> tuple[int, float]:
    t0 = time.perf_counter()
    try:
        if endpoint == "generate":
            r = await client.post(f"{base}/v1/images/generations", json={
                "model": "storybit", "effect_id": "mock-ghibli-1",
                "image": img, "size": "1:1", "poll_timeout": 5})
        elif endpoint == "effects":
            r = await client.get(f"{base}/v1/effects")
        elif endpoint == "categories":
            r = await client.get(f"{base}/v1/categories")
        elif endpoint == "health":
            r = await client.get(f"{base}/health")
        else:
            raise ValueError(endpoint)
        await r.aread()
        return r.status_code, (time.perf_counter() - t0) * 1000
    except httpx.HTTPError:
        return 0, (time.perf_counter() - t0) * 1000


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, int(round(q / 100 * (len(values) - 1))))
    return values[idx]


async def run_round(base: str, endpoint: str, concurrency: int, total: int,
                    img: str) -> dict:
    limits = httpx.Limits(max_connections=concurrency * 2,
                          max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=120, limits=limits) as client:
        sem = asyncio.Semaphore(concurrency)

        async def worker():
            async with sem:
                return await one(client, base, endpoint, img)

        t0 = time.perf_counter()
        results = await asyncio.gather(*[worker() for _ in range(total)])
        elapsed = time.perf_counter() - t0

    codes = Counter(c for c, _ in results)
    lat = [ms for _, ms in results]
    ok = sum(v for k, v in codes.items() if 200 <= k < 300)
    return {
        "endpoint": endpoint, "concurrency": concurrency, "total": total,
        "elapsed_s": round(elapsed, 2),
        "rps": round(total / elapsed, 2),
        "ok": ok, "fail": total - ok,
        "codes": dict(codes),
        "p50_ms": round(pct(lat, 50), 1), "p90_ms": round(pct(lat, 90), 1),
        "p99_ms": round(pct(lat, 99), 1), "max_ms": round(max(lat), 1),
        "mean_ms": round(statistics.fmean(lat), 1),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8089")
    ap.add_argument("--endpoint", default="generate",
                    choices=["generate", "effects", "categories", "health"])
    ap.add_argument("--levels", default="1,4,16")
    ap.add_argument("--total", type=int, default=120,
                    help="requests per level")
    ap.add_argument("--image", type=int, default=512, help="input image edge in px")
    ap.add_argument("--json", action="store_true", help="print machine-readable result only")
    args = ap.parse_args()

    img = sample_image(args.image)
    rows = []
    for level in [int(x) for x in args.levels.split(",") if x.strip()]:
        row = await run_round(args.base, args.endpoint, level, args.total, img)
        rows.append(row)
        if not args.json:
            print(f"[{row['endpoint']}] c={row['concurrency']:>2} n={row['total']:>4} "
                  f"ok={row['ok']:>4} fail={row['fail']:>3} "
                  f"rps={row['rps']:>7} p50={row['p50_ms']:>7}ms "
                  f"p90={row['p90_ms']:>7}ms p99={row['p99_ms']:>7}ms "
                  f"codes={row['codes']}")
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
