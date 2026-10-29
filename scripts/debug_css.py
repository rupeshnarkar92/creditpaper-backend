"""Debug: check data-workspace-tab attribute and CSS rules."""

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
        page.wait_for_timeout(2000)
        page.locator('button:has-text("Credit Paper")').first.click()
        page.wait_for_timeout(3000)

        # Check data attribute
        tab = page.evaluate("document.documentElement.dataset.workspaceTab")
        print(f"data-workspace-tab = {tab!r}")

        # Check CSS rules containing 'credit-paper'
        rules = page.evaluate("""() => {
            const sheets = document.styleSheets;
            let found = [];
            for (const sheet of sheets) {
                try {
                    for (const rule of sheet.cssRules) {
                        if (rule.selectorText && rule.selectorText.includes('credit-paper')) {
                            found.push(rule.selectorText + ' -> ' + rule.style.cssText.substring(0, 100));
                        }
                    }
                } catch {}
            }
            return found;
        }""")
        print(f"credit-paper CSS rules: {len(rules)}")
        for r in rules:
            print(f"  {r}")

        # Check if td-icon-rail has display:none
        icon_rail_display = page.evaluate("""() => {
            const el = document.querySelector('.td-icon-rail');
            if (!el) return 'NOT FOUND';
            return window.getComputedStyle(el).display;
        }""")
        print(f"td-icon-rail display: {icon_rail_display}")

        # Check if td-topbar has display:none
        topbar_display = page.evaluate("""() => {
            const el = document.querySelector('.td-topbar');
            if (!el) return 'NOT FOUND';
            return window.getComputedStyle(el).display;
        }""")
        print(f"td-topbar display: {topbar_display}")

        browser.close()


if __name__ == "__main__":
    main()
