"""Integration against tests/serve_demo.py. Uses only synthetic data and the local mock API."""
import io
import time
import uuid
import zipfile

import httpx

BASE = "http://127.0.0.1:5558"


def session(username, password):
    client = httpx.Client(base_url=BASE, trust_env=False, timeout=20)
    token = client.get("/api/me/").json()["csrf"]
    client.headers.update({"X-CSRFToken": token, "Origin": BASE})
    result = client.post("/api/login/", json={"username":username,"password":password})
    assert result.status_code == 200, result.text
    client.headers["X-CSRFToken"] = result.json()["csrf"]
    return client


def require(result, code=200):
    assert result.status_code == code, f"{result.status_code}: {result.text[:1000]}"
    return result.json()


def main():
    client = session("test-operator", "Browser-test-passphrase-42!")
    for asset in ("/", "/static/app.js", "/static/app.css", "/static/vendor/xterm.js", "/static/vendor/xterm.css"):
        result = client.get(asset)
        assert result.status_code == 200, asset
        assert result.headers.get("x-content-type-options") == "nosniff", asset
    provider = require(client.get("/api/providers/"))["providers"][0]
    assert "solar-test" in require(client.get(f"/api/providers/{provider['id']}/models/"))["models"]
    title = "HTTP integration " + uuid.uuid4().hex[:8]
    chat = require(client.post("/api/chats/", json={"title":title,"provider_id":provider["id"],"model":"solar-test","permission":"workspace-write"}),201)
    chat_id = chat["id"]
    uploaded = require(client.post(f"/api/chats/{chat_id}/files/", files=[("files",("input.txt",b"test input","text/plain"))], data={"paths":"project/input.txt"}))
    assert uploaded["files"][0]["path"] == "user/files/project/input.txt"
    run = require(client.post(f"/api/chats/{chat_id}/messages/", json={"message":"Create a result file"}),202)["run"]
    deadline = time.monotonic()+30
    while time.monotonic()<deadline:
        events = require(client.get(f"/api/chats/{chat_id}/events/"))
        if not events["active_run"]:
            break
        time.sleep(.2)
    else:
        raise AssertionError("Worker did not finish in 30 seconds")
    assert any(e["role"]=="assistant" and e["kind"]=="message" and "task is complete" in e["content"] for e in events["events"]),events
    files = require(client.get(f"/api/chats/{chat_id}/files/"))["files"]
    output = next(f for f in files if f["path"]=="codex/files/result.txt")
    downloaded = client.get(f"/api/files/{output['id']}/")
    assert downloaded.status_code == 200 and b"test deliverable" in downloaded.content
    exported = client.get(f"/api/chats/{chat_id}/export/")
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert "user/files/project/input.txt" in archive.namelist()
        assert "codex/files/result.txt" in archive.namelist()
        assert "chat_history.txt" in archive.namelist()
    schedule = require(client.post("/api/schedules/",json={"name":"One-time smoke task","prompt":"Create output","provider_id":provider["id"],"model":"solar-test","permission":"workspace-write","kind":"once","max_runs":1}),201)
    deadline = time.monotonic()+10
    while time.monotonic()<deadline:
        item = next(s for s in require(client.get("/api/schedules/"))["schedules"] if s["id"]==schedule["id"])
        if item["run_count"]:
            break
        time.sleep(.2)
    assert item["run_count"]==1 and not item["enabled"],item
    username="member-"+uuid.uuid4().hex[:10]
    require(client.post("/api/users/",json={"username":username,"password":"Member-test-password-42!","current_password":"Browser-test-passphrase-42!"}),201)
    member = session(username,"Member-test-password-42!")
    assert member.get("/api/chats/").status_code==403
    changed=require(member.post("/api/password/",json={"current_password":"Member-test-password-42!","password":"Changed-member-test-99!"}))
    member.headers["X-CSRFToken"]=changed["csrf"]
    assert require(member.get("/api/chats/"))["chats"]==[]
    assert member.get(f"/api/chats/{chat_id}/").status_code==404
    assert member.get(f"/api/files/{output['id']}/").status_code==404
    assert member.post(f"/api/runs/{run['id']}/stop/",json={}).status_code==404
    assert member.get("/api/users/").status_code==403
    assert member.post("/api/desktop/",json={}).status_code==403
    malicious=client.post(f"/api/chats/{chat_id}/files/",files=[("files",("bad.txt",b"bad"))],data={"paths":"../escape"})
    assert malicious.status_code==400
    wrong_origin=client.post(f"/api/chats/{chat_id}/messages/",json={"message":"no"},headers={"Origin":"http://evil.invalid"})
    assert wrong_origin.status_code==403
    anonymous=httpx.post(BASE+"/api/skills/",files={"file":("SKILL.md",b"test")},trust_env=False)
    assert anonymous.status_code in (401,403)
    oversized = client.post("/api/chats/", content=iter([b"x" * 700_000, b"y" * 700_000]),
                            headers={"Content-Type": "application/json"})
    assert oversized.status_code == 413, oversized.text[:200]
    print("HTTP smoke passed: assets, login/CSRF, model discovery, chat, folder upload, real worker + mock model, encrypted artifact download, ZIP export, scheduling, user creation/password change, owner isolation, traversal, desktop denial.")
    member.close()
    client.close()


if __name__ == "__main__":
    main()
