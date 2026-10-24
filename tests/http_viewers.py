"""Authenticated file delivery and real PowerShell WebSocket integration on the demo server."""
import json
from pathlib import Path
import socket
import time
import uuid

from wsproto import ConnectionType, WSConnection
from wsproto.events import AcceptConnection, CloseConnection, RejectConnection, Request, TextMessage

from http_smoke import BASE, require, session


def terminal_roundtrip(client, terminal_id, marker, reject=False):
    connection = WSConnection(ConnectionType.CLIENT)
    cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
    accepted, output = False, ""
    with socket.create_connection(("127.0.0.1", 5558), timeout=10) as transport:
        transport.settimeout(.5)
        transport.sendall(connection.send(Request(host="127.0.0.1:5558", target=f"/ws/admin-terminal/{terminal_id}/",
                                                 extra_headers=[(b"origin", BASE.encode()), (b"cookie", cookie.encode())])))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                raw = transport.recv(65536)
            except TimeoutError:
                continue
            connection.receive_data(raw or None)
            for event in connection.events():
                if isinstance(event, RejectConnection):
                    assert reject and event.status_code == 403
                    return
                if isinstance(event, AcceptConnection):
                    assert not reject
                    accepted = True
                    transport.sendall(connection.send(TextMessage(data=json.dumps({"type": "resize", "cols": 100, "rows": 25}))))
                    split = len(marker) // 2
                    command = f"Write-Output ('{marker[:split]}' + '{marker[split:]}')\r"
                    transport.sendall(connection.send(TextMessage(data=json.dumps({"type": "input", "data": command}))))
                if isinstance(event, TextMessage):
                    message = json.loads(event.data)
                    if message.get("type") == "output":
                        output += message["data"]
                        if marker in output:
                            transport.sendall(connection.send(CloseConnection(code=1000)))
                            return
                if isinstance(event, CloseConnection):
                    raise AssertionError("Terminal closed before the command returned")
            if not raw:
                break
    raise AssertionError(f"Terminal roundtrip failed (accepted={accepted})")


def main():
    client = session("test-operator", "Browser-test-passphrase-42!")
    for asset in ("file-viewer.js", "file-source.js", "model-viewer.js", "model-worker.js", "cad-worker.js", "admin-terminal.js", "viewers.css",
                  "vendor/pdf.min.mjs", "vendor/pdf.worker.min.mjs", "vendor/three.core.js", "vendor/occt-import-js.js",
                  "vendor/pdfjs/standard_fonts/LiberationSans-Regular.ttf", "vendor/pdfjs/cmaps/UniJIS-UTF16-H.bcmap", "vendor/pdfjs/wasm/openjpeg.wasm"):
        result = client.get("/static/" + asset)
        assert result.status_code == 200, asset
        assert result.headers["x-content-type-options"] == "nosniff"
        if asset.endswith((".mjs", ".js")):
            assert result.headers["content-type"].startswith("text/javascript")
        if asset.endswith(".wasm"):
            assert result.headers["content-type"] == "application/wasm"
    assert require(client.get("/api/admin-terminal/"))["terminal"] is None
    provider = require(client.get("/api/providers/"))["providers"][0]
    chat = require(client.post("/api/chats/", json={"title": "Viewer HTTP acceptance", "provider_id": provider["id"]}), 201)
    fixtures = Path(__file__).with_name("fixtures")
    for name in ("preview.pdf", "preview.md", "triangle.stl", "cube.step"):
        original = fixtures.joinpath(name).read_bytes()
        result = require(client.post(f"/api/chats/{chat['id']}/files/", files={"files": (name, original)}))
        file_id = next(item["id"] for item in result["files"] if item["path"].endswith("/" + name))
        downloaded = client.get(f"/api/files/{file_id}/")
        assert downloaded.content == original
        assert "attachment" in downloaded.headers["content-disposition"]
        assert downloaded.headers["cache-control"] == "no-store"
    username, password = "viewer-member-" + uuid.uuid4().hex[:8], "Viewer-test-passphrase-42!"
    require(client.post("/api/users/", json={"username": username, "password": password, "current_password": "Browser-test-passphrase-42!"}), 201)
    member = session(username, password)
    changed = require(member.post("/api/password/", json={"current_password": password, "password": "Changed-viewer-test-passphrase-42!"}))
    member.headers["X-CSRFToken"] = changed["csrf"]
    assert member.get(f"/api/files/{file_id}/").status_code == 404
    assert member.post("/api/admin-terminal/").status_code == 403
    terminal = require(client.post("/api/admin-terminal/"))["terminal"]
    try:
        terminal_roundtrip(member, terminal["id"], "", reject=True)
        terminal_roundtrip(client, terminal["id"], "SOLAR_HTTP_TERMINAL_ONE")
        assert require(client.get("/api/admin-terminal/"))["terminal"]["running"]
        terminal_roundtrip(client, terminal["id"], "SOLAR_HTTP_TERMINAL_TWO")
        require(client.post("/api/logout/"))
    finally:
        client.delete("/api/admin-terminal/")
        client.close()
        member.close()
    print("HTTP viewer/terminal acceptance passed: local assets, authenticated byte-exact file delivery, member denial, real PowerShell over WebSocket, resize, reconnect and logout.")


if __name__ == "__main__":
    main()
