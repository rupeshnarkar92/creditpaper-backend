"""Phase 3: verify markdown rendering + full-screen credit paper."""

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

        # Navigate to deal credit paper
        page.goto(f"{BASE}/tenant-admin/deals")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        # Click the deal
        row = page.locator("text=Riverbend Rise").first
        if row.count() > 0:
            row.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2000)

            # Click Credit Paper tab
            cp_tab = page.locator('button:has-text("Credit Paper")')
            if cp_tab.count() > 0:
                cp_tab.first.click()
                page.wait_for_timeout(3000)

                # Screenshot: full credit paper page (dark)
                page.screenshot(path=str(OUT / "20_cp_fullscreen_dark.png"), full_page=False)
                print("[1] Credit paper full-screen dark captured")

                # Check if icon rail is hidden
                icon_rail = page.locator(".td-icon-rail")
                is_visible = icon_rail.is_visible() if icon_rail.count() > 0 else False
                print(f"[2] Icon rail visible: {is_visible}")

                # Check if topbar is hidden
                topbar = page.locator(".td-topbar")
                is_visible_tb = topbar.is_visible() if topbar.count() > 0 else False
                print(f"[3] Topbar visible: {is_visible_tb}")

                # Check rendered content
                headings = page.locator(".cp-block-heading")
                paragraphs = page.locator(".cp-block-paragraph")
                table_rows = page.locator(".cp-block-table-row")
                print(
                    f"[4] Rendered blocks: headings={headings.count()}, paragraphs={paragraphs.count()}, table_rows={table_rows.count()}"
                )

                # Check for raw markdown
                body_text = page.locator(".cp-document").inner_text()
                has_raw_h2 = "## " in body_text
                has_raw_bold = "**" in body_text
                has_raw_table = "|---" in body_text
                print(f"[5] Raw markdown: h2={has_raw_h2}, bold={has_raw_bold}, table={has_raw_table}")

                # Screenshot: zoomed into document area
                doc = page.locator(".cp-document")
                if doc.count() > 0:
                    doc.first.screenshot(path=str(OUT / "21_cp_document_detail.png"))
                    print("[6] Document detail captured")

                # Light mode
                toggle = page.locator('button[title*="Switch"], button[aria-label*="Switch"]')
                if toggle.count() > 0:
                    toggle.first.click()
                    page.wait_for_timeout(1500)
                    page.screenshot(path=str(OUT / "22_cp_fullscreen_light.png"), full_page=False)
                    print("[7] Credit paper full-screen light captured")
                    toggle.first.click()
                    page.wait_for_timeout(500)
            else:
                print("[!] Credit Paper tab NOT FOUND")
        else:
            print("[!] No deal found")

        files = sorted(OUT.glob("*.png"))
        print(f"\n=== DONE: {len(files)} screenshots ===")
        for f in files:
            size = f.stat().st_size
            print(f"  {f.name} ({size:,} bytes)")

        browser.close()


if __name__ == "__main__":
    main()
