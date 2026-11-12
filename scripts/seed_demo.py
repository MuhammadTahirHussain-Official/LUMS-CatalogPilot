"""Fill a running local CatalogPilot with demo data, using only the public API.

    .venv/Scripts/python scripts/seed_demo.py

Signs in with the account in var/dev-login.txt (creating it if needed), creates a catalog,
uploads samples/products.csv, imports it, asks for AI suggestions and connects the sandbox
store. After it finishes, open http://localhost:8000 and try Publishing.
"""

from __future__ import annotations

import hashlib
import pathlib
import secrets
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
LOGIN_FILE = ROOT / "var" / "dev-login.txt"


def credentials() -> tuple[str, str]:
    if not LOGIN_FILE.exists():
        LOGIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOGIN_FILE.write_text("Local demo account (this machine only)\nemail: demo@catalogpilot.test\n"
                              f"password: local-{secrets.token_urlsafe(12)}\n", encoding="utf-8")
    lines = dict(line.split(": ", 1) for line in LOGIN_FILE.read_text(encoding="utf-8").splitlines() if ": " in line)
    return lines["email"], lines["password"]


def main() -> int:
    email, password = credentials()
    with httpx.Client(base_url=BASE, timeout=60) as c:
        r = c.post("/v1/auth/register", json={"email": email, "password": password, "organization": "Demo Store"})
        token = r.json().get("dev_verification_token")
        if token:
            c.post("/v1/auth/verify-email", json={"token": token})
        r = c.post("/v1/auth/login", json={"email": email, "password": password})
        if r.status_code != 200:
            print("Could not sign in:", r.json().get("detail"))
            return 1
        h = {"X-CSRF-Token": r.json()["csrf_token"]}

        def call(method: str, path: str, **kw):
            response = c.request(method, f"/v1{path}", headers={**h, **kw.pop("headers", {})}, **kw)
            if response.status_code >= 400:
                raise SystemExit(f"{method} {path} failed: {response.text}")
            return response

        catalogs = call("GET", "/catalogs").json()["data"]
        catalog = catalogs[0]["id"] if catalogs else call("POST", "/catalogs", json={"name": "Main catalog"}).json()["id"]
        data = (ROOT / "samples" / "products.csv").read_bytes()
        upload = call("POST", "/uploads", json={"catalog_id": catalog, "files": [
            {"name": "products.csv", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}).json()
        url = upload["files"][0]["upload_url"].removeprefix("/v1")
        call("PATCH", url, content=data, headers={"Upload-Offset": "0"})
        base = f"/uploads/{upload['id']}/files/{upload['files'][0]['id']}"
        for _ in range(30):
            if call("GET", f"/uploads/{upload['id']}").json()["files"][0]["state"] == "clean":
                break
        mapping = call("GET", f"{base}/preview").json()["suggested_mapping"]
        call("PUT", f"{base}/mapping", json={"mapping": mapping, "save_as": "Shopify export"})
        result = call("POST", f"{base}/import").json()
        print("import:", result["products"], "row errors:", result["row_errors"])
        ai = call("POST", f"/catalogs/{catalog}/enhance", json={}).json()
        print(f"AI: {ai['proposals']} suggestions, {ai['auto_approved']} applied, {ai['pending']} waiting for review")
        channels = call("GET", "/channels").json()["data"]
        if not any(ch["platform"] == "mock" for ch in channels):
            ch = call("POST", "/channels", json={"platform": "mock", "name": "Sandbox store"}).json()
            call("PUT", f"/channels/{ch['id']}/credential",
                 json={"kind": "access_token", "secret": {"access_token": f"sandbox-{secrets.token_hex(8)}"}})
            call("POST", "/channels", json={"platform": "csv_feed", "name": "Product feed (CSV)"})
            print("channels: sandbox store connected, CSV feed added")
    print(f"done. Open {BASE} and sign in with the account in {LOGIN_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
