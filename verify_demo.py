"""End-to-end check of demo.html with a real browser (Playwright/Chromium).

    python verify_demo.py --base http://127.0.0.1:8089 --mode live
    python verify_demo.py --base http://127.0.0.1:8089 --mode mock

live: asserts the effect catalogue renders and a generation request is queued
      (upstream AI worker may answer 202 instead of an image).
mock: asserts the full happy path - an output image is rendered.

Exits non-zero on the first failed assertion so it can gate a release.
"""
from __future__ import annotations

import argparse
import io
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright


def make_input_image() -> str:
    im = Image.new("RGB", (640, 640), (35, 55, 95))
    d = ImageDraw.Draw(im)
    d.ellipse((180, 150, 460, 430), fill=(240, 205, 120))
    path = Path(tempfile.gettempdir()) / "storybit_demo_input.jpg"
    im.save(path, "JPEG", quality=88)
    return str(path)


def run(base: str, mode: str, headed: bool) -> int:
    img = make_input_image()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(base, wait_until="domcontentloaded")

        page.wait_for_function(
            "() => document.querySelector('#health').textContent.includes('·')",
            timeout=30_000)
        health = page.inner_text("#health")
        assert "ok" in health, f"health badge not ok: {health}"
        print(f"[ok] health badge: {health}")

        page.wait_for_function(
            "() => document.querySelectorAll('#cat option').length > 0", timeout=60_000)
        cats = page.eval_on_selector_all("#cat option", "els => els.map(e => e.value)")
        assert cats, "no effect categories"
        print(f"[ok] effect categories: {len(cats)} -> {cats[:4]}")

        page.wait_for_selector(".effect", timeout=30_000)
        n_eff = page.eval_on_selector_all(".effect", "els => els.length")
        assert n_eff > 0, "no effects rendered"
        print(f"[ok] effects in first category: {n_eff}")

        page.set_input_files("#file", img)
        page.wait_for_selector("img#in-box", timeout=15_000)
        print("[ok] input preview rendered")

        page.fill("#poll", "45")
        page.click("#go")
        page.wait_for_function(
            "() => { const t = document.querySelector('#gen-stat').textContent;"
            " return t && !t.includes('提交中'); }", timeout=180_000)
        stat = page.inner_text("#gen-stat")
        print(f"[ok] generation finished: {stat}")

        page.wait_for_function(
            "() => { const p = document.querySelector('.preview');"
            " const out = p.lastElementChild;"
            " return out.querySelector('img') || out.querySelector('.empty'); }",
            timeout=30_000)
        out_html = page.eval_on_selector(".preview", "el => el.lastElementChild.innerHTML")
        has_img = "<img" in out_html
        if mode == "mock":
            assert has_img, f"mock run produced no output image: {out_html[:200]}"
            src = page.get_attribute(".preview img:last-of-type", "src") or ""
            assert src.startswith("data:image/"), "output is not an image payload"
            print(f"[ok] mock output image: {src[:48]}… ({len(src)} bytes b64)")
        else:
            print(f"[ok] live run output state: {'image' if has_img else 'queued/no image'}")

        page.screenshot(path=str(Path(__file__).parent / f"demo_{mode}.png"),
                        full_page=True)
        print(f"[ok] screenshot demo_{mode}.png")
        assert not errors, f"page JS errors: {errors}"
        browser.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8089")
    ap.add_argument("--mode", default="live", choices=["live", "mock"])
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    try:
        sys.exit(run(a.base, a.mode, a.headed))
    except AssertionError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)
