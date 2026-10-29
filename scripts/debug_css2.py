"""Debug: deeper check of WorkspacePage mounting."""

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

        # Go to deals
        page.goto("http://localhost:5173/tenant-admin/deals")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1000)

        # Click the deal row
        page.locator("text=Riverbend Rise").first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(3000)

        # Check current URL
        url = page.url
        print(f"Current URL: {url}")

        # Check if WorkspacePage is rendered
        has_workspace = page.locator(".dw-workspace").count()
        has_cp_tab = page.locator('button:has-text("Credit Paper")').count()
        has_loading = page.locator(".dw-loading").count()
        has_error = page.locator(".dw-error").count()
        print(
            f"dw-workspace: {has_workspace}, Credit Paper btn: {has_cp_tab}, loading: {has_loading}, error: {has_error}"
        )

        # Check deal nav panel
        has_nav = page.locator(".dw-deal-nav").count()
        print(f"dw-deal-nav: {has_nav}")

        # Get all visible text in the nav area
        if has_nav > 0:
            nav_text = page.locator(".dw-deal-nav").inner_text()
            print(f"Nav text: {nav_text[:200]}")

        # Check TenantAdminLayout
        has_td_layout = page.locator(".td-layout-3col").count()
        has_td_content = page.locator(".td-content-3col").count()
        print(f"td-layout-3col: {has_td_layout}, td-content-3col: {has_td_content}")

        # Get Outlet content
        if has_td_content > 0:
            outlet_html = page.locator(".td-content-3col").inner_html()[:500]
            print(f"td-content-3col html (first 500): {outlet_html}")

        # Now click Credit Paper
        if has_cp_tab > 0:
            page.locator('button:has-text("Credit Paper")').first.click()
            page.wait_for_timeout(3000)

            tab = page.evaluate("document.documentElement.dataset.workspaceTab")
            print("\nAfter clicking Credit Paper:")
            print(f"data-workspace-tab = {tab!r}")

            has_cp_layout = page.locator(".cp-layout").count()
            has_cp_toolbar = page.locator(".cp-toolbar").count()
            print(f"cp-layout: {has_cp_layout}, cp-toolbar: {has_cp_toolbar}")

            # Check inner html of td-content-3col
            if page.locator(".td-content-3col").count() > 0:
                outlet_html = page.locator(".td-content-3col").inner_html()[:500]
                print(f"td-content-3col html after CP: {outlet_html}")

        browser.close()


if __name__ == "__main__":
    main()
