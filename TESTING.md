# Verification and acceptance

The test harness uses synthetic identities, temporary encrypted storage, a deterministic mock model API, and fake desktop inputs. It does not read your original `Chats` directory or use a real provider account.

## Reproduce in a normal environment

```sh
uv sync --group dev
uv run python tests/manage.py test tests --noinput
uv run python tests/manage.py check --deploy
uv run python tests/manage.py makemigrations --check --dry-run
node --check solar/static/app.js
node tests/test_slash.mjs
node tests/test_folder_picker.mjs
node tests/test_viewers.mjs
node tests/test_pdf.mjs
```

Node is optional for the application; these commands check JavaScript syntax, the slash menu's keyboard controller, and the folder picker's controller with a DOM/event harness. The latter exercises selecting/creating a workspace, preserving draft values, cancelling, reusing a registration, read-only controls, and error handling; it does not substitute for browser rendering checks.

`test_viewers.mjs` runs the real STL, OBJ, PLY, GLB and glTF parsers, tests malformed/oversized/cyclic models and forbidden references, and tests the Markdown DOM renderer and bounded download helper. `test_pdf.mjs` uses the shipped PDF.js to parse a two-page fixture and extract text. Its Node canvas/polyfill warnings are expected: this test does not render pixels. To verify the actual CAD parser after installing the runtime, also run `node tests/test_cad.mjs`; it exits with a pending message if the wasm is missing. Install it with `uv run solar viewer-assets` while the production server is stopped, before starting a test server.

For the HTTP acceptance test, start the synthetic server in one terminal:

```sh
uv run python tests/serve_demo.py
```

It binds only `127.0.0.1:5558` and a mock model at `127.0.0.1:5569`, with data under `.solar-test-browser`. The test-only account is `test-operator` / `Browser-test-passphrase-42!`. These credentials are never created by the production launcher. Host access is enabled for project-browser acceptance; desktop access is disabled. The fixture has only a deterministic API provider and does not invoke a live CLI. Then run in another terminal:

```sh
uv run python tests/http_smoke.py
uv run python tests/http_viewers.py
```

For browser acceptance, install a Playwright Chromium browser and leave that same synthetic server running:

```sh
uv run playwright install chromium
uv run python tests/browser_smoke.py
uv run python tests/browser_viewers.py
```

This exercises login, navigation, a chat with upload and generated output, safe text rendering, native terminal component loading, scheduling, a mobile viewport, and screenshot capture. Artifacts go into the ignored `artifacts/` directory. A successful run is required before treating visual/browser acceptance as complete. Stop the synthetic server with Ctrl+C after testing.

The additional viewer browser test checks the sidebar and file-panel controls, Markdown tables/source switching, a local model link, STL view controls, PDF page navigation, a STEP cube, and a real PowerShell start/reconnect/stop. It requires the CAD runtime and WebGL; a missing runtime deliberately fails STEP acceptance. The HTTP viewer test independently verifies the new static assets/MIME headers, byte-exact authenticated fixture downloads, owner isolation, administrator-only shell access, actual PowerShell commands over WebSocket, resize, reconnect and logout. It uses only the disposable account and harmless output commands on Windows.

The test harness mocks OS ACL mutation only for its disposable storage. Production `uv run solar` has no ACL-bypass switch and fails if it cannot restrict access. No fake model response is reachable through the production provider adapters.

## Automated coverage

- Encryption of identities, messages, provider secrets, files, history, and native state; integrity and cross-chat key separation.
- Ownership checks across chats, events, files, exports, provider models, roles, runs, and admin accounts; foreign-resource selections and delegation.
- Login throttling, password whitespace, password changes, mandatory changes, CSRF, cookie/header policy, administrator reauthentication, deactivation, and WebSocket session revocation/origin checks.
- File/folder uploads and exports, quota, path traversal, Windows names, Unicode/case collisions, file/folder conflicts, ZIP traversal/links/expansion, previews, safe skill import and edited bundle export.
- Queue uniqueness, cancellation, scheduled occurrence claims, overlap avoidance, missed intervals, limits, three-day/three-month schedules, timezone/DST behavior, and impossible calendar dates.
- Streaming mock API replies and tools, restricted writes, nested delegation, read-only inheritance, secret-safe errors, and redirect rejection.
- Actual subprocess I/O and artifact collection, actual Windows ConPTY input/output and resize, run timeouts, recovery, provider role generation, encrypted native-state resume, and file replacement after a simulated database failure.
- Actual host-program execution from an authorized API tool, denied execution for ordinary/read-only accounts, inherited command restrictions, command timeout/output/cancellation cleanup, delegate skill resources, and cancellation during an API read.
- Mocked desktop lease ownership, no capture before Start, dragging, mouse release, and key modifier release without injecting real OS input.
- Chat search/pagination, upload preauthentication, request size limits, and configuration TLS/bound checks.
- Registering/creating project folders, encrypted project names/paths, owner and host-access checks, traversal/private-path/link rejection, non-overwriting uploads, downloads, project locks and original source preservation after actual subprocess execution and private-runtime cleanup.
- Continuing automation history/files across occurrences, adoption of the latest legacy chat, and preventing overlap with a manual run in the automation chat.
- Codex max effort and full-access flags, role restrictions, automatic approval review, interactive approval routing, persistent provider-session IDs, generated resume arguments accepted by the installed Codex CLI, and linked-project API tool restrictions.
- Early WebSocket resize delivery before the worker attaches a terminal, command/argument completion, arrow wrapping, Tab/Enter selection, Escape dismissal, IME and Shift+Enter handling, and preventing completion from submitting a message.
- Standalone terminal administrator/host/password gates, CSRF, no process on GET, login-session binding, one live terminal per administrator, password/logout/demotion revocation, independent idle monitoring, bounded output, real PowerShell ConPTY I/O, and WebSocket input/output/resize/reconnect boundaries.
- CAD asset integrity rejection, atomic installation, offline reuse, correct module/wasm MIME types, and the worker CSP. Tests use a minimal synthetic wasm to validate installation mechanics; this does not substitute for a real CAD tessellation test.

## Delivery environment results and limitations

The viewer/PowerShell update passes **118 automated Python tests**, the slash menu and folder picker suites, the new Markdown/model parser suite, and the PDF.js parser suite. Both HTTP integration scripts pass against the synthetic server, including actual PowerShell commands over an authenticated WebSocket and reconnecting to the same process. Django deployment checks and migration consistency pass. The lockfile validates offline, the wheel builds with the viewer modules/fonts/decoders/licenses, and changed-module lint and JavaScript syntax checks pass.

Two acceptance checks remain unavailable here. The pinned OpenCascade wasm download was blocked by the managed network, so actual STEP/IGES/BREP parsing has not been exercised; normal startup installs that dependency or reports its failure. Browser acceptance was attempted with `browser_viewers.py` and failed before launching a browser because Windows denied Playwright's IPC pipe (`WinError 5`). PDF canvas rendering, WebGL presentation and the new panel layouts therefore remain visually unverified. Automated parser and transport success must not be reported as a successful browser test.

The current workspace has Python **3.12.2**, Django **5.2.18**, and a `uv.lock`. The updated tests run against this supported Django version. The managed Windows environment still restricts OS ACL changes, external network access and browser process/IPC startup.

At the previous session update, **101 automated Python tests passed**, plus the JavaScript slash-menu controller checks. Django deployment checks reported no issues, migration consistency and JavaScript syntax checks passed, and targeted lint checks for the changed backend modules and new tests passed. Actual Windows ConPTY I/O and HTTP integration against a local mock model passed. The HTTP chain includes login, upload, the actual worker queue, streaming, generated file download, ZIP export, scheduling, account creation/password replacement, CSRF, cross-user denial, and rejection of oversized chunked request bodies. The installed Codex **0.164.0** accepted the generated structured-resume options through its help parser without requesting inference. An offline wheel build succeeded with the new migrations and frontend assets included, and `uv lock --check --offline` accepted the existing lockfile.

The direct folder-picker update passed **103 Python tests**, both JavaScript controller checks, syntax checks, targeted lint and an offline wheel build. The new coverage includes invalid/unwritable new folders and instructions directing deliverables to the chosen workspace. `browser_smoke.py` now covers browsing, creating and selecting a folder without losing the session draft. A normal Playwright launch was attempted again and failed during Windows IPC creation (`WinError 5`); browser acceptance remains unverified. The live server returned the updated frontend containing the folder picker; existing browser tabs may need Ctrl+F5. Restart Solar to load the changed Python output-location instructions and folder-creation validation.

The server on port 5557 was still running the older backend at verification. Stop and restart it with `uv run solar` to load the update and apply migrations; the updated frontend detects an old backend and explains that restart. Production data was not migrated under a running old worker. `uv run --locked solar check` could not apply the required Windows private ACLs from the managed session, so the existing production server was left running.

Not completed in this sandbox:

- A fresh online dependency resolution or current vulnerability audit. `pip-audit` could not contact PyPI through the restricted environment's proxy; no clean audit result is claimed. The existing lockfile is preserved.
- A restart of the user's existing production server and live migration of their data. Tests use isolated installations.
- Browser rendering/screenshots and interactive UI acceptance; browser engines failed to launch because of environment permissions. The browser test is supplied but must not be reported as passing.
- Paid/live Codex or Claude inference, OAuth login refresh, model-specific API compatibility, or complete native slash-command acceptance.
- Capture/control of the real PC desktop, including monitor scaling and real keyboard layouts.
- Linux/macOS runtime acceptance or public-internet/load/penetration testing.

Do not work around the production checks by disabling ACL protection or lowering the Django version. Run the app and remaining acceptance checks from a normal OS session with network access. Inspect dependency results with `uv run pip-audit` after syncing; repeat relevant tests after dependency or provider CLI upgrades.
