"""Check if Generate Paper button is visible on the Credit Paper page."""

import asyncio

from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})

        # Go to login
        await page.goto("http://localhost:5173/login", wait_until="networkidle")
        await page.wait_for_timeout(1000)

        # Login
        await page.fill('input[type="email"]', "uday@cylsy.com")
        await page.fill('input[type="password"]', "Password@2")
        await page.click('button[type="submit"]')
        await page.wait_for_timeout(3000)

        # Navigate to deals
        await page.goto("http://localhost:5173/tenant-admin/deals", wait_until="networkidle")
        await page.wait_for_timeout(2000)

        # Take screenshot to see what we have
        await page.screenshot(path="C:\\Users\\nadim\\AppData\\Local\\Temp\\01_deals.png")
        print("Screenshot 01: deals list")

        # Click on the deal
        deal_link = page.locator("text=Riverbend Rise").first
        if await deal_link.count() > 0:
            await deal_link.click()
            await page.wait_for_timeout(3000)
            await page.screenshot(path="C:\\Users\\nadim\\AppData\\Local\\Temp\\02_workspace.png")
            print("Screenshot 02: workspace")
        else:
            # Try clicking the first deal row
            rows = page.locator(".dw-deal-row, .dl-row, tr")
            count = await rows.count()
            print(f"Found {count} rows")
            if count > 0:
                await rows.first.click()
                await page.wait_for_timeout(3000)
                await page.screenshot(path="C:\\Users\\nadim\\AppData\\Local\\Temp\\02_workspace.png")
                print("Screenshot 02: workspace (from row click)")

        # Click Credit Paper tab
        credit_tab = page.locator("text=Credit Paper").first
        if await credit_tab.count() > 0:
            await credit_tab.click()
            await page.wait_for_timeout(3000)
            await page.screenshot(path="C:\\Users\\nadim\\AppData\\Local\\Temp\\03_credit_paper.png")
            print("Screenshot 03: credit paper tab")

            # Check for Generate Paper button
            gen_btn = page.locator("text=Generate Paper")
            gen_count = await gen_btn.count()
            print(f"'Generate Paper' text found: {gen_count} times")

            # Check for the button class
            gen_btn_class = page.locator(".cp-toolbar-generate")
            cls_count = await gen_btn_class.count()
            print(f".cp-toolbar-generate elements found: {cls_count}")

            # Check for cp-toolbar
            toolbar = page.locator(".cp-toolbar")
            tb_count = await toolbar.count()
            print(f".cp-toolbar elements found: {tb_count}")

            # Check for cp-layout
            layout = page.locator(".cp-layout")
            ly_count = await layout.count()
            print(f".cp-layout elements found: {ly_count}")

            # Dump visible text
            body_text = await page.inner_text("body")
            # Search for relevant keywords
            for kw in [
                "Generate",
                "Paper",
                "Generate Paper",
                "Outline",
                "Back",
                "cp-layout",
                "Select a section",
            ]:
                if kw in body_text:
                    print(f"  Found in body: '{kw}'")
                else:
                    print(f"  NOT in body: '{kw}'")

            # Check console errors
            errors = []
            page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
            await page.wait_for_timeout(1000)
            if errors:
                print(f"Console errors: {errors}")
        else:
            print("Could not find Credit Paper tab")
            await page.screenshot(path="C:\\Users\\nadim\\AppData\\Local\\Temp\\03_no_tab.png")

        await browser.close()


asyncio.run(main())
