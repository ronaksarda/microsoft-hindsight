"""Browser smoke test: real uvicorn + headless Chromium. Skipped if Playwright is unavailable.

Set GRANTANCHOR_SCREENSHOTS=<dir> to save screenshots.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    port = _free_port()
    env = {**os.environ, "GROQ_API_KEY": "", "HINDSIGHT_API_KEY": "", "DATA_DIR": str(tmp_path_factory.mktemp("d"))}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
        cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            if httpx.get(base + "/api/state", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.25)
    else:
        proc.kill()
        pytest.fail("server did not start")
    yield base
    proc.terminate()
    proc.wait(timeout=10)


@pytest.mark.parametrize("name,viewport", [("desktop", (1440, 900)), ("mobile", (390, 844))])
def test_audit_and_compare_flow(server, name, viewport):
    shots = os.environ.get("GRANTANCHOR_SCREENSHOTS")
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browsers not installed
            pytest.skip(f"chromium unavailable: {exc}")
        page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(server, wait_until="networkidle")

        # Selects are populated from the API, not hardcoded.
        state = httpx.get(server + "/api/state").json()
        spender = page.eval_on_selector("form select", "s => s.value")
        assert spender in {m["id"] for m in state["team_members"]}

        page.fill("input[placeholder='Vendor legal name']", "Pacific Air")
        page.fill("input[placeholder='City, Country']", "Tokyo, Japan")
        page.fill("input[type=number]", "3100")
        page.fill("textarea", "Flights to robotics summit")
        page.fill("#f-category", "Travel")

        page.click("text=COMPARE: WITHOUT vs WITH MEMORY")
        page.wait_for_selector("text=CAUGHT ONLY WITH MEMORY")
        assert page.locator("text=CLAWBACK RISK DETECTED").count() >= 1
        if shots:
            page.screenshot(path=f"{shots}/{name}_compare.png", full_page=True)

        page.click("button[type=submit]")
        page.wait_for_selector("text=BLOCKING VIOLATIONS")
        assert page.locator("text=RISK").count() >= 1
        if shots:
            page.screenshot(path=f"{shots}/{name}_audit.png", full_page=True)

        # Near-miss shows amber, not silent approval.
        page.fill("input[type=number]", "1100")
        page.click("button[type=submit]")
        page.wait_for_selector("text=APPROVED WITH WARNINGS")

        assert page.evaluate("document.documentElement.scrollWidth") <= viewport[0]
        assert errors == []
        browser.close()
