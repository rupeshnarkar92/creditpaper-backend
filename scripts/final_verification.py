"""Final verification: take screenshots of all states."""

import os

from playwright.sync_api import sync_playwright


def main():
    out = r"D:\OSProjects\creditpaper\scripts\ui_screenshots"
    os.makedirs(out, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        # Login
        page.goto("http://localhost:5173/login")
        page.wait_for_load_state("networkidle")
        page.fill('input[type="email"], input[name="email"], input[placeholder*="email" i]', "uday@cylsy.com")
        page.fill('input[type="password"], input[name="password"]', "Password@2")
        page.click('button[type="submit"]')
        page.wait_for_url("**/tenant-admin/**", timeout=15000)
        page.wait_for_timeout(2000)

        # Screenshot: Home (light mode)
        page.goto("http://localhost:5173/tenant-admin/home")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
        page.screenshot(path=f"{out}/final_01_home_light.png")
        print("1. Home light mode")

        # Screenshot: Home (dark mode) - toggle
        page.evaluate(
            "document.documentElement.dataset.theme = 'dark'; localStorage.setItem('creditpaper.theme', 'dark')"
        )
        page.wait_for_timeout(500)
        page.screenshot(path=f"{out}/final_02_home_dark.png")
        print("2. Home dark mode")

        # Screenshot: Deals list (dark mode)
        page.goto("http://localhost:5173/tenant-admin/deals")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
        page.screenshot(path=f"{out}/final_03_deals_dark.png")
        print("3. Deals list dark")

        # Screenshot: Light mode toggle
        page.evaluate(
            "document.documentElement.dataset.theme = 'light'; localStorage.setItem('creditpaper.theme', 'light')"
        )
        page.wait_for_timeout(500)
        page.screenshot(path=f"{out}/final_04_deals_light.png")
        print("4. Deals list light")

        # Screenshot: Deal workspace - Documents tab
        page.locator("text=Riverbend Rise").first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
        page.screenshot(path=f"{out}/final_05_documents_tab.png")
        print("5. Documents tab")

        # Screenshot: Credit Paper tab (light mode)
        page.locator('button:has-text("Credit Paper")').first.click()
        page.wait_for_timeout(3000)
        page.screenshot(path=f"{out}/final_06_credit_paper_light.png")
        print("6. Credit paper light mode")

        # Verify layout state
        tab = page.evaluate("document.documentElement.dataset.workspaceTab")
        rail_display = page.evaluate("getComputedStyle(document.querySelector('.td-icon-rail')).display")
        topbar_display = page.evaluate("getComputedStyle(document.querySelector('.td-topbar')).display")
        print(f"\nLayout check: tab={tab}, rail={rail_display}, topbar={topbar_display}")

        # Screenshot: Credit Paper dark mode
        page.evaluate(
            "document.documentElement.dataset.theme = 'dark'; localStorage.setItem('creditpaper.theme', 'dark')"
        )
        page.wait_for_timeout(500)
        page.screenshot(path=f"{out}/final_07_credit_paper_dark.png")
        print("7. Credit paper dark mode")

        browser.close()
        print(f"\nScreenshots saved to {out}")


if __name__ == "__main__":
    main()
