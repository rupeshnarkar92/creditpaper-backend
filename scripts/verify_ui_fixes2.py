"""Phase 2: deal workspace + credit paper screenshots."""

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("D:/OSProjects/creditpaper/scripts/ui_screenshots")
OUT.mkdir(exist_ok=True)

EMAIL = "uday@cylsy.com"
PASSWORD = os.environ.get("SMOKE_TEST_PASSWORD", "")
if not PASSWORD:
    raise SystemExit("SMOKE_TEST_PASSWORD is required for UI verification")
BASE = "http://localhost:5173"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        # Login
        page.goto(f"{BASE}/login")
        page.wait_for_load_state("networkidle")
        page.fill('input[type="email"], input[name="email"], input[placeholder*="email" i]', EMAIL)
        page.fill('input[type="password"], input[name="password"]', PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_url("**/tenant-admin/**", timeout=15000)
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)

        # Go to deals list and click the deal
        page.goto(f"{BASE}/tenant-admin/deals")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        # Click the deal row
        row = page.locator("text=Riverbend Rise").first
        if row.count() > 0:
            row.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(3000)
            print(f"[1] At deal workspace: {page.url}")
            page.screenshot(path=str(OUT / "08_deal_workspace_dark.png"), full_page=False)

            # Click Credit Paper tab
            cp_tab = page.locator('button:has-text("Credit Paper")')
            if cp_tab.count() > 0:
                cp_tab.first.click()
                page.wait_for_timeout(3000)
                print(f"[2] At credit paper tab: {page.url}")
                page.screenshot(path=str(OUT / "09_credit_paper_dark.png"), full_page=False)

                # Check Generate Paper button
                gen_btn = page.locator("text=Generate Paper")
                if gen_btn.count() > 0:
                    print("[3] Generate Paper button: VISIBLE")
                    gen_btn.first.screenshot(path=str(OUT / "10_generate_btn.png"))
                else:
                    print("[3] Generate Paper button: NOT FOUND")
                    # Dump body text for debug
                    body_text = page.locator("body").inner_text()
                    print(f"    Body text (first 500): {body_text[:500]}")

                # Light mode credit paper
                toggle = page.locator('button[title*="Switch"], button[aria-label*="Switch"]')
                if toggle.count() > 0:
                    toggle.first.click()
                    page.wait_for_timeout(1500)
                    page.screenshot(path=str(OUT / "11_credit_paper_light.png"), full_page=False)
                    print("[4] Credit paper light mode captured")
                    toggle.first.click()
                    page.wait_for_timeout(500)
                else:
                    print("[4] No theme toggle found on credit paper page")
            else:
                print("[2] Credit Paper tab NOT FOUND")
                # List all tab buttons
                tabs = page.locator('nav button, [class*="tab"] button, [class*="deal-nav"] button')
                print(f"    Found {tabs.count()} nav/tab buttons")
                for i in range(min(tabs.count(), 10)):
                    print(f"    Tab {i}: {tabs.nth(i).inner_text()}")
        else:
            print("[1] No deal found to click")

        # Screenshot: deals panel light mode (sidebar context)
        page.goto(f"{BASE}/tenant-admin/deals")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)
        # Click the hamburger first to expand
        hamburger = page.locator(".td-topbar-icon").first
        if hamburger.count() > 0:
            hamburger.click()
            page.wait_for_timeout(500)

        files = sorted(OUT.glob("*.png"))
        print(f"\n=== DONE: {len(files)} screenshots ===")
        for f in files:
            size = f.stat().st_size
            print(f"  {f.name} ({size:,} bytes)")

        browser.close()


if __name__ == "__main__":
    main()
