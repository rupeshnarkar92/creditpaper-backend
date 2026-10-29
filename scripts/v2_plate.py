"""Take screenshot of credit paper with toolbar."""

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.goto("http://localhost:5173/login")
        page.wait_for_load_state("networkidle")
        page.fill('input[type="email"], input[name="email"], input[placeholder*="email" i]', "uday@cylsy.com")
        page.fill('input[type="password"], input[name="password"]', "Password@2")
        page.click('button[type="submit"]')
        page.wait_for_url("**/tenant-admin/**", timeout=15000)
        page.wait_for_timeout(2000)
        page.goto("http://localhost:5173/tenant-admin/deals")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)
        page.locator("text=Riverbend Rise").first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)
        page.locator('button:has-text("Credit Paper")').first.click()
        page.wait_for_timeout(5000)
        page.screenshot(path=r"D:\OSProjects\creditpaper\scripts\ui_screenshots\v2_plate.png")
        toolbar = page.locator(".cp-section-toolbar").count()
        editors = page.locator("[data-slate-editor]").count()
        print(f"Toolbars: {toolbar}, Editors: {editors}")
        if editors > 0:
            editable = page.locator("[data-slate-editor]").first.get_attribute("contenteditable")
            print(f"Contenteditable: {editable}")
        browser.close()


if __name__ == "__main__":
    main()
