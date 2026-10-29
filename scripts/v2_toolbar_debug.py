"""Debug toolbar visibility."""

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

        # Check toolbar
        tb = page.locator(".cp-section-toolbar").first
        if tb.count() > 0:
            vis = tb.is_visible()
            box = tb.bounding_box()
            html = tb.inner_html()[:300]
            print(f"Toolbar visible: {vis}")
            print(f"Toolbar box: {box}")
            print(f"Toolbar html: {html}")
        else:
            print("No toolbar found")

        # Check buttons inside
        btns = page.locator(".cp-tb-btn")
        print(f"Toolbar buttons: {btns.count()}")
        if btns.count() > 0:
            for i in range(min(btns.count(), 5)):
                btn = btns.nth(i)
                txt = btn.inner_text()
                vis = btn.is_visible()
                print(f"  Button {i}: text='{txt}' visible={vis}")

        page.screenshot(path=r"D:\OSProjects\creditpaper\scripts\ui_screenshots\v2_toolbar_debug.png")
        browser.close()


if __name__ == "__main__":
    main()
