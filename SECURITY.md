# Security model

Solar executes programs and can control the host desktop. Its account permissions must reflect who you trust with those capabilities.

## Access boundaries

- Every chat, event, file, provider, agent, skill, run, and schedule endpoint checks the signed-in owner. Application administrator status does not bypass those ownership filters.
- Project registration, host directory browsing, project file transfers and linked project execution additionally require host access. Project names and paths are encrypted in the database. Linked project contents stay as normal files on the host; the encrypted chat vault does not protect those existing files. Project access rejects traversal, links/junctions and Solar private data paths. Trusted native programs still have the OS account's access, subject to the selected provider sandbox.
- Administrators manage account identities and permissions and can reset another account's password. Password reset is a privileged account takeover capability; the app cannot prevent a hostile administrator from using that power.
- New ordinary users have neither `host_access` nor `remote_access`. Grant either only to trusted PC operators. Codex/Claude and explicitly enabled API host commands run as Solar's OS account, and remote desktop controls that account's visible desktop. They are not contained in per-user VMs or OS accounts.
- Native provider sandbox flags, agent instructions, and skill restrictions are useful controls, but they do not provide hostile-tenant isolation. A trusted host user can potentially reach other application data or credentials through operating-system tools. Use separate OS accounts/VMs and separate Solar installations for mutually untrusted people who need host execution.
- The standalone PowerShell API and WebSocket both require an active administrator with host execution and a changed temporary password. Terminals are bound to the administrator and originating login session; the monitor checks account/session revocation even without a connected browser. Commands run with the server OS account's rights. This page has no provider sandbox. Start/stop events are audited, but terminal contents are only buffered in memory; shell programs may keep their own history.
- Full host access is an explicit trusted-account choice. Never-ask approval does not itself disable sandboxing. Codex automatic review requires workspace-write; interactive approval launches the native terminal and is disallowed for unattended automations. A role can narrow the chat's sandbox and shell/network settings. Full access and API host commands cannot be network-restricted by a checkbox.
- Accounts without host access receive bounded, per-chat virtual file tools and allowlisted agent delegation. A configured model server is itself a trusted external processor. Trusted host accounts may explicitly enable `run_command` through a role with shell and workspace-write permissions; this permits arbitrary installed programs under the OS account, including programs with their own network access. The network-tools checkbox is not a firewall. API delegation inherits the parent's read-only and host-command restrictions, and host permission is rechecked before each command.

## Storage and transport

Account usernames and user-generated textual content use authenticated encryption. File content, skill bundles, and native session snapshots are encrypted. Keys are derived for distinct purposes from a random local master key. Passwords use Argon2, with a bound on simultaneous memory-intensive checks. Login lookup IDs and virtual path lookup IDs use keyed digests. UUID relationships, dates, sizes, statuses, permissions, MIME types, and other operational metadata are not all encrypted; the database is not whole-file encrypted.

The bootstrap configuration contains an encrypted temporary username/password and password hash. The reversible temporary password is removed after replacement. Provider tokens remain recoverable to the server because it must send them to the selected provider. Server-side encryption does not protect against an attacker with the master key, server process access, an administrator capable of resetting accounts, or unrestricted OS access.

Active host runs use plaintext workspaces protected by OS directory permissions. Process arguments, provider memory, operating-system swap, crash dumps, and external provider logs are outside the vault's encryption guarantee. Incomplete output collection retains plaintext in the protected runtime directory for recovery. Storage media are not securely erased after deletion. Use full-disk encryption where that threat matters.

TLS is required for non-loopback listeners. Cookies use secure/HttpOnly/SameSite controls, passwords are not placed in URLs, authenticated responses are not cached, and WebSockets validate the same-origin session and recheck revocation. The server does not trust forwarded proxy headers. Do not disable these controls to expose the service publicly.

## Implemented protections

- CSRF checks, explicit HTTP methods, session rotation and invalidation, first-use password changes, bounded login attempts, and password reauthentication for account administration.
- Exact API-origin allowlist, redirect rejection, URL validation, and request time/size limits. Users cannot choose arbitrary outbound API destinations through the web UI.
- Traversal-safe virtual paths, Windows reserved-name handling, Unicode normalization, case-insensitive collisions, file/folder conflict checks, opaque encrypted blobs, and revision-safe file replacement.
- ZIP entry, expansion, file-size, and file-count limits; symlink and junction exclusions; no automatic execution during skill import; attachment-only downloads for active content.
- No model/user HTML injection: UI strings are inserted through text nodes. A restrictive content security policy, no third-party frontend requests, frame denial, and content-type protections are enabled.
- File previews reuse owner-filtered downloads. PDF.js draws pages on canvases without executing PDF JavaScript, XFA or annotation actions. Markdown tokens become DOM elements through a fixed renderer; raw HTML remains text and external images are omitted. HTML/SVG source is not embedded as active content. 3D parsing runs in workers with timeouts; glTF buffers are limited to owned relative files or bounded embedded data, with textures/animations omitted. The CSP permits WebAssembly compilation, but not JavaScript `unsafe-eval`; workers have their own restrictive response policy. These third-party parsers still require maintenance and do not make arbitrary documents risk-free.
- The CAD dependency installer contacts one pinned upstream HTTPS URL, validates the byte count and Git blob hash, and installs atomically. It sends no private files. Failure leaves the rest of the app available and CAD previews unavailable until installation succeeds.
- Per-chat active-run uniqueness, owner queue limits, run/command deadlines, output caps, cancellation, process identity checks during crash recovery, private per-chat CLI homes, and bounded simultaneous large HTTP transfers.
- Explicit single-session desktop leases, permission checks, idle/lifetime limits, disconnect cleanup, and mouse-button release.
- A local server lock to avoid two schedulers on the same database and a fail-closed startup if private OS storage permissions cannot be applied.

## Operational requirements and limits

Use a private network/VPN, current Python/Django/provider tools, protected backups, sufficient disk space, and an interactive OS session for desktop control. Do not grant host access merely to let an untrusted user chat with a model; use an API provider for that case. Existing host Codex/Claude login tokens are copied only into trusted users' private CLI workspaces when no per-provider login is available. Provider login refresh state is stored encrypted for that user.

Agent outputs and imported skills can be malicious. Treat them as instructions/code you are choosing to execute, particularly when granting shell or network capability. Solar does not certify a skill, a model's output, a CLI sandbox, or a connected model server.

The application has bounded operations but is not hardened for hostile public-internet traffic, distributed denial of service, unlimited tenants, or unbounded histories. File quotas do not bound all database growth. SQLite, local file locking, and an in-process worker are intended for one host process. Retained accounts and archived chats remain stored; account removal is access revocation, not certified erasure.

Tests are described in [TESTING.md](TESTING.md). Passing tests do not prove absence of vulnerabilities. A current dependency audit and deployment/browser/live-provider acceptance remain necessary in the actual operating environment. No independent penetration test has been performed.

If you discover an issue, stop exposing the instance, preserve protected diagnostic evidence locally, and fix/test before restarting. Never put credentials, private paths, chat exports, or the master key in a public report.
