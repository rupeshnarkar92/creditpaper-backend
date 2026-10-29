"""Playwright verification of post-merge UI fixes."""

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
        print(f"[1] Logged in, at: {page.url}")

        # Screenshot 1: Dashboard (default dark mode)
        page.screenshot(path=str(OUT / "01_dashboard_dark.png"), full_page=False)
        print("[2] Dashboard dark mode captured")

        # Screenshot 2: Dashboard light mode
        toggle = page.locator('button[aria-label*="Switch to"]')
        if toggle.count() > 0:
            toggle.first.click()
            page.wait_for_timeout(1000)
            page.screenshot(path=str(OUT / "02_dashboard_light.png"), full_page=False)
            print("[3] Dashboard light mode captured")
            # Switch back to dark
            toggle.first.click()
            page.wait_for_timeout(500)
        else:
            print("[3] WARN: No theme toggle button found!")

        # Screenshot 3: Icon rail close-up (check labels)
        icon_rail = page.locator(".td-icon-rail")
        if icon_rail.count() > 0:
            icon_rail.first.screenshot(path=str(OUT / "03_iconrail_labels.png"))
            print("[4] Icon rail captured")
        else:
            print("[4] WARN: td-icon-rail not found")

        # Screenshot 4: Navigate to deals list
        page.goto(f"{BASE}/tenant-admin/deals")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(OUT / "04_deals_list.png"), full_page=False)
        print("[5] Deals list captured")

        # Screenshot 5: Navigate to a deal workspace (credit paper tab)
        # First check if there's a deal to click
        deal_links = page.locator('[class*="deal-card"], [class*="deals-table"] tr, .adm2-table tbody tr')
        if deal_links.count() > 0:
            deal_links.first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)
            print(f"[6] Clicked deal, at: {page.url}")

            # Click credit paper tab if visible
            cp_tab = page.locator('button:has-text("Credit Paper"), [data-workspace-tab="credit-paper"]')
            if cp_tab.count() > 0:
                cp_tab.first.click()
                page.wait_for_timeout(2000)
                page.screenshot(path=str(OUT / "05_credit_paper.png"), full_page=False)
                print("[7] Credit paper tab captured")
            else:
                page.screenshot(path=str(OUT / "05_deal_workspace.png"), full_page=False)
                print("[7] Deal workspace captured (no credit paper tab found)")

            # Screenshot 6: Check for Generate Paper button
            gen_btn = page.locator("text=Generate Paper")
            if gen_btn.count() > 0:
                print(f"[8] Generate Paper button: VISIBLE (count={gen_btn.count()})")
                gen_btn.first.screenshot(path=str(OUT / "06_generate_btn.png"))
            else:
                print("[8] Generate Paper button: NOT FOUND")
        else:
            print("[6] No deals found in table")

        # Screenshot 7: Deals page with light mode toggle
        page.goto(f"{BASE}/tenant-admin/deals")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)
        toggle2 = page.locator('button[aria-label*="Switch to"]')
        if toggle2.count() > 0:
            toggle2.first.click()
            page.wait_for_timeout(1000)
            page.screenshot(path=str(OUT / "07_deals_light.png"), full_page=False)
            print("[9] Deals list light mode captured")
            toggle2.first.click()
            page.wait_for_timeout(500)

        # Summary
        files = sorted(OUT.glob("*.png"))
        print(f"\n=== DONE: {len(files)} screenshots in {OUT} ===")
        for f in files:
            size = f.stat().st_size
            print(f"  {f.name} ({size:,} bytes)")

        browser.close()


if __name__ == "__main__":
    main()
