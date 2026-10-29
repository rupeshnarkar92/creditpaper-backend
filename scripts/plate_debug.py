"""Quick screenshot of credit paper after Plate fix."""

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

        # Click Credit Paper
        page.locator('button:has-text("Credit Paper")').first.click()
        page.wait_for_timeout(5000)

        # Check for console errors
        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.wait_for_timeout(1000)

        # Screenshot
        page.screenshot(path=r"D:\OSProjects\creditpaper\scripts\ui_screenshots\plate_test.png")
        print("Screenshot saved")

        # Check for Plate content
        plate_count = page.locator("[data-slate-editor]").count()
        cp_sections = page.locator(".cp-document-section").count()
        print(f"Slate editors: {plate_count}, cp-sections: {cp_sections}")

        # Check visible text
        body_text = (
            page.locator(".cp-editor").inner_text()[:300]
            if page.locator(".cp-editor").count() > 0
            else "no cp-editor"
        )
        print(f"Editor text: {body_text[:200]}")

        # Check for errors in console
        page.wait_for_timeout(2000)
        if errors:
            print(f"Console errors: {errors[:5]}")

        browser.close()


if __name__ == "__main__":
    main()
