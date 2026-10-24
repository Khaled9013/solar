"""Browser acceptance suite for the disposable server: python tests/serve_demo.py.

Requires Playwright and its Chromium browser. This never visits external providers.
Screenshots are written to ignored artifacts/, and contain only synthetic test data.
"""
from pathlib import Path
import uuid

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "artifacts"
ARTIFACTS.mkdir(exist_ok=True)


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width":1440,"height":1000}, accept_downloads=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:5558")
        expect(page.locator("#login-view")).to_be_visible()
        page.screenshot(path=str(ARTIFACTS / "login.png"), full_page=True)
        page.locator("#login-form input[name=username]").fill("test-operator")
        page.locator("#login-form input[name=password]").fill("Browser-test-passphrase-42!")
        page.locator("#login-form button").click()
        expect(page.locator("#app")).to_be_visible()
        expect(page.locator("#page h1")).to_contain_text("test-operator")
        page.screenshot(path=str(ARTIFACTS / "overview.png"), full_page=True)

        page.locator("#new-session").click()
        page.locator("#modal input[name=title]").fill("Browser acceptance " + uuid.uuid4().hex[:6])
        page.locator("#modal input[name=model]").fill("solar-test")
        draft_title=page.locator("#modal input[name=title]").input_value()
        page.locator("#modal").get_by_role("button", name="Browse folders…", exact=True).click()
        picker=page.locator(".remote-folder-dialog")
        expect(picker).to_be_visible()
        picker.locator("input[name=remote_folder_path]").fill(str(ARTIFACTS))
        picker.get_by_role("button", name="Go", exact=True).click()
        expect(picker.get_by_role("button", name="New folder", exact=True)).to_be_enabled()
        picker.get_by_role("button", name="New folder", exact=True).click()
        folder_name="session-output-"+uuid.uuid4().hex[:8]
        picker.locator("input[name=remote_new_folder]").fill(folder_name)
        page.screenshot(path=str(ARTIFACTS / "session-folder-picker.png"), full_page=True)
        picker.get_by_role("button", name="Create & use folder", exact=True).click()
        expect(picker).to_have_count(0)
        expect(page.locator("#modal input[name=title]")).to_have_value(draft_title)
        expect(page.locator("#modal .workspace-selected-path")).to_contain_text(folder_name)
        project_id=page.locator("#modal select[name=project_id]").input_value()
        assert project_id
        page.locator("#modal").get_by_role("button", name="Enable workspace writes", exact=True).click()
        expect(page.locator("#modal select[name=permission]")).to_have_value("workspace-write")
        page.locator("#modal").get_by_role("button", name="Browse folders…", exact=True).click()
        picker.locator(".form-actions").get_by_role("button", name="Cancel", exact=True).click()
        expect(picker).to_have_count(0)
        expect(page.locator("#modal select[name=project_id]")).to_have_value(project_id)
        page.locator("#modal button[type=submit]").click()
        expect(page.locator("#messages")).to_be_visible()
        page.locator("#files-panel").get_by_role("button", name="Project", exact=True).click()
        expect(page.locator(".project-pane")).to_contain_text(folder_name)
        page.locator("#files-panel").get_by_role("button", name="Chat files", exact=True).click()
        composer = page.locator("textarea[name=message]")
        composer.fill("/eff")
        expect(page.locator(".slash-menu")).to_be_visible()
        composer.press("Enter")
        expect(composer).to_have_value("/effort ")
        composer.press("ArrowUp")
        composer.press("Tab")
        expect(composer).to_have_value("/effort ultra")
        expect(page.locator(".message.user")).to_have_count(0)
        composer.fill("/perm")
        composer.press("Escape")
        expect(page.locator(".slash-menu")).to_be_hidden()
        composer.fill("")
        with page.expect_file_chooser() as chooser:
            page.locator("#files-panel").get_by_role("button", name="↑ Files", exact=True).click()
        chooser.value.set_files({"name": "browser-input.txt", "mimeType": "text/plain", "buffer": b"Synthetic browser upload"})
        expect(page.locator("#file-tree")).to_contain_text("browser-input.txt")
        page.locator("textarea[name=message]").fill("Create a file. Treat this as text: <img src=x onerror=alert(1)>")
        page.locator("#send-message").click()
        expect(page.locator(".message.assistant .message-body")).to_contain_text("Your task is complete", timeout=30000)
        expect(page.locator("#file-tree")).to_contain_text("result.txt", timeout=10000)
        expect(page.locator(".message.user img")).to_have_count(0)
        with page.expect_download() as download:
            page.locator("#files-panel").get_by_role("link", name="↓ ZIP", exact=True).click()
        download.value.save_as(str(ARTIFACTS / "chat-export.zip"))
        page.screenshot(path=str(ARTIFACTS / "chat.png"), full_page=True)
        chat_url = page.url

        # Validate the locally bundled full terminal emulator independently of any provider login.
        exports = page.evaluate("""async () => {
            const {default: xterm} = await import('/static/vendor/xterm.js');
            const host = document.createElement('div');
            host.style.cssText='width:600px;height:200px';
            document.body.append(host);
            const terminal = new xterm.Terminal();
            const fit = new xterm.FitAddon();
            terminal.loadAddon(fit); terminal.open(host); fit.fit();
            await new Promise(resolve => terminal.write('First row\\r\\nSecond row\\x1b[1;12HColumn 12',resolve));
            const a=terminal.buffer.active;
            const ok=terminal.cols>20 && terminal.rows>5 && a.getLine(0).translateToString().startsWith('First row  Column 12') && a.getLine(1).translateToString().startsWith('Second row');
            const rows=host.querySelectorAll('.xterm-rows > div');
            const positioned=rows.length>1 && rows[1].getBoundingClientRect().top>rows[0].getBoundingClientRect().top;
            host.style.width='350px';fit.fit();
            const resized=terminal.cols<60;
            terminal.dispose(); host.remove(); return ok && positioned && resized;
        }""")
        assert exports

        page.goto("http://127.0.0.1:5558/#projects")
        page.get_by_role("button", name="Add project", exact=True).first.click()
        project_path=ARTIFACTS / ("browser-project-" + uuid.uuid4().hex[:8])
        page.locator("#modal input[name=name]").fill("Browser project")
        page.locator("#modal input[name=path]").fill(str(project_path))
        page.locator("#modal input[name=create]").check()
        page.locator("#modal button[type=submit]").click()
        expect(page.locator("#page")).to_contain_text(str(project_path))
        page.screenshot(path=str(ARTIFACTS / "projects.png"), full_page=True)

        for route in ("agents", "skills", "schedules", "runs", "providers", "users", "account", "desktop"):
            page.goto("http://127.0.0.1:5558/#"+route)
            expect(page.locator("#page h1")).to_be_visible()
            assert "Something needs attention" not in page.locator("#page").inner_text()
        page.goto("http://127.0.0.1:5558/#schedules")
        page.get_by_role("button", name="＋ Add automation", exact=True).click()
        page.locator("#modal input[name=name]").fill("Browser three-day task")
        page.locator("#modal textarea[name=prompt]").fill("Review files and create a short report.")
        page.locator("#modal input[name=model]").fill("solar-test")
        page.locator("#modal input[name=every]").fill("3")
        page.locator("#modal select[name=unit]").select_option("86400")
        page.locator("#modal input[name=duration_months]").fill("3")
        page.locator("#modal button[type=submit]").click()
        expect(page.get_by_role("heading", name="Browser three-day task", exact=True)).to_be_visible()

        page.set_viewport_size({"width":390,"height":844})
        page.goto(chat_url)
        expect(page.locator("#messages")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        page.screenshot(path=str(ARTIFACTS / "mobile-chat.png"), full_page=True)
        page.locator("#mobile-menu").click()
        expect(page.locator("#sidebar")).to_be_visible()
        page.locator("#nav a[data-page=agents]").click()
        expect(page.locator("#page h1")).to_have_text("A role for every kind of work")
        assert not errors, errors
        browser.close()
        print("Browser acceptance passed: desktop/mobile layouts, chat, streaming, export, XSS text rendering, xterm, navigation, and automation form.")


if __name__ == "__main__":
    main()
