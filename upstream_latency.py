"""Sample real upstream latency (keep totals small - this hits a third party)."""
import asyncio, sys, time, statistics
sys.path.insert(0, '.')
from rc import Config
from storybit_client import StoryBitClient

def pct(v,q):
    v=sorted(v); return v[min(len(v)-1,int(round(q/100*(len(v)-1))))]

async def main():
    cfg=Config(); cfg.refresh()
    c=StoryBitClient(cfg)
    print("hosts:", cfg.api_url, cfg.ai_url, "rc:", cfg.source)
    for label, coro_fn in [
        ("config/vu   (main)", lambda: c.version_update()),
        ("effects     (ai)  ", lambda: c.ai_effects()),
        ("categories  (main)", lambda: c.categories()),
    ]:
        lat=[]
        for _ in range(8):
            t0=time.perf_counter()
            try:
                await coro_fn(); lat.append((time.perf_counter()-t0)*1000)
            except Exception as e:
                print(label,"ERR",repr(e)[:80])
            await asyncio.sleep(0.4)
        if lat:
            print(f"{label} n={len(lat)} p50={pct(lat,50):7.0f}ms p90={pct(lat,90):7.0f}ms "
                  f"min={min(lat):6.0f}ms max={max(lat):6.0f}ms mean={statistics.fmean(lat):7.0f}ms")
    await c.aclose()

asyncio.run(main())
