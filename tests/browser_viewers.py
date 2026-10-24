"""Real-browser acceptance for previews, panel controls, and the admin shell.

Start tests/serve_demo.py first. This only uses the synthetic loopback installation.
"""
from pathlib import Path
import uuid

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:5558"
FIXTURES = ROOT / "tests" / "fixtures"


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE)
        page.locator("#login-form input[name=username]").fill("test-operator")
        page.locator("#login-form input[name=password]").fill("Browser-test-passphrase-42!")
        page.locator("#login-form button").click()
        expect(page.locator("#app")).to_be_visible()
        page.locator("#mobile-menu").click()
        expect(page.locator("#sidebar")).to_be_hidden()
        page.locator("#mobile-menu").click()
        expect(page.locator("#sidebar")).to_be_visible()
        page.locator("#new-session").click()
        page.locator("#modal input[name=title]").fill("Viewer acceptance " + uuid.uuid4().hex[:8])
        page.locator("#modal button[type=submit]").click()
        expect(page.locator("#messages")).to_be_visible()
        panel = page.locator("#files-panel")
        if not panel.is_visible():
            page.locator(".files-toggle").click()
        panel.get_by_role("button", name="Expand", exact=True).click()
        expect(panel).to_have_class("file-panel wide-panel")
        panel.get_by_role("button", name="Collapse", exact=True).click()
        expect(panel).to_be_hidden()
        page.locator(".files-toggle").click()
        expect(panel).to_be_visible()
        with page.expect_file_chooser() as chooser:
            panel.get_by_role("button", name="↑ Files", exact=True).click()
        chooser.value.set_files([str(FIXTURES / name) for name in ("preview.md", "preview.pdf", "triangle.stl", "cube.step")])
        tree = page.locator("#file-tree")
        expect(tree).to_contain_text("preview.pdf")
        tree.get_by_role("button", name="preview.md", exact=True).click()
        dialog = page.locator(".file-viewer")
        expect(dialog.locator(".markdown-content h1")).to_have_text("Solar file preview")
        expect(dialog.locator(".markdown-content table")).to_be_visible()
        expect(dialog.locator(".markdown-content script,.markdown-content img")).to_have_count(0)
        dialog.get_by_role("button", name="Show source", exact=True).click()
        expect(dialog.locator(".viewer-text")).to_contain_text("# Solar file preview")
        dialog.get_by_role("button", name="Rendered view", exact=True).click()
        dialog.get_by_role("button", name="Open the model", exact=True).click()
        expect(dialog.locator(".model-stage canvas")).to_be_visible(timeout=20000)
        expect(dialog.locator(".viewer-caption")).to_contain_text("1 triangles")
        dialog.get_by_role("button", name="Wireframe", exact=True).click()
        expect(dialog.get_by_role("button", name="Wireframe", exact=True)).to_have_attribute("aria-pressed", "true")
        dialog.get_by_role("button", name="Expand", exact=True).click()
        expect(dialog).to_have_class("file-viewer viewer-expanded")
        page.screenshot(path=str(ROOT / "artifacts" / "model-preview.png"))
        dialog.get_by_role("button", name="Close", exact=True).click()
        tree.get_by_role("button", name="preview.pdf", exact=True).click()
        expect(dialog.locator(".pdf-stage canvas")).to_be_visible(timeout=20000)
        expect(dialog.locator(".viewer-toolbar")).to_contain_text("of 2")
        dialog.get_by_role("button", name="Next →", exact=True).click()
        expect(dialog.locator(".pdf-page-number")).to_have_value("2")
        page.screenshot(path=str(ROOT / "artifacts" / "pdf-preview.png"))
        dialog.get_by_role("button", name="Close", exact=True).click()
        tree.get_by_role("button", name="cube.step", exact=True).click()
        # This assertion deliberately fails if the CAD runtime has not been installed.
        expect(dialog.locator(".model-stage canvas")).to_be_visible(timeout=60000)
        dialog.get_by_role("button", name="Close", exact=True).click()
        page.goto(BASE + "/#terminal")
        expect(page.locator("#terminal-nav")).to_be_visible()
        expect(page.get_by_role("button", name="Start PowerShell", exact=True)).to_be_enabled()
        page.get_by_role("button", name="Start PowerShell", exact=True).click()
        expect(page.locator(".terminal-status")).to_contain_text("connected")
        terminal = page.locator(".xterm-helper-textarea")
        terminal.focus()
        terminal.press_sequentially("Write-Output ('SOLAR_BROWSER_' + 'SHELL_OK')", delay=10)
        terminal.press("Enter")
        expect(page.locator(".xterm-rows")).to_contain_text("SOLAR_BROWSER_SHELL_OK", timeout=10000)
        page.get_by_role("button", name="Reconnect", exact=True).click()
        expect(page.locator(".terminal-status")).to_contain_text("connected")
        page.get_by_role("button", name="Stop", exact=True).click()
        expect(page.get_by_role("button", name="Start PowerShell", exact=True)).to_be_enabled()
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert not errors, errors
        browser.close()
        print("Viewer and PowerShell browser acceptance passed.")


if __name__ == "__main__":
    (ROOT / "artifacts").mkdir(exist_ok=True)
    main()
