"""End-to-end through the HTTP API: journeys J2 and J4, plus the security properties that
must hold at the edge (CSRF, tenant isolation, roles, write-only credentials, upload attacks)."""

import hashlib
import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from catalogpilot.connectors import mock
from catalogpilot.engine import worker

PASSWORD = "correct horse battery staple"
CSV = "\n".join([
    "Handle,Title,Body (HTML),Vendor,Tags,Option1 Name,Option1 Value,Variant SKU,Variant Price,Cost per item,"
    "Variant Inventory Qty,Image Src,Status,Material",
    'tee,"Classic Tee - FREE SHIPPING","<p>Soft tee</p><script>alert(1)</script>",Acme,"summer, cotton",Size,S,'
    "TEE-S,19.99,7.50,10,https://cdn.example.com/tee.jpg,active,Cotton",
    "tee,,,,,Size,M,TEE-M,19.99,7.50,4,,,",
    'mug,"=HYPERLINK(""http://evil.example"",""Mug"")",<p>Mug</p>,Acme,,,,MUG-1,"1,299.50",3,0,,active,Ceramic',
    "bad,No price here,,,,,,BAD-1,abc,,,,active,",
    ",Missing key,,,,,,X-1,5,,,,active,",
]) + "\n"


@pytest.fixture()
def api(fresh, monkeypatch):
    monkeypatch.setenv("CP_WORKER", "0")
    from catalogpilot.api import deps
    from catalogpilot.api.app import create_app

    deps.limiter.reset()
    mock.reset()
    with TestClient(create_app()) as client:
        yield client


class User:
    def __init__(self, client: TestClient, email: str, org: str = "Org"):
        self.c, self.email = TestClient(client.app), email
        r = self.c.post("/v1/auth/register", json={"email": email, "password": PASSWORD, "organization": org})
        assert r.status_code == 202, r.text
        token = r.json().get("dev_verification_token")
        if token:
            assert self.c.post("/v1/auth/verify-email", json={"token": token}).status_code == 200
        self.login()

    def login(self):
        r = self.c.post("/v1/auth/login", json={"email": self.email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf_token"]

    def h(self, **extra):
        return {"X-CSRF-Token": self.csrf, **extra}

    def post(self, url, json=None, **kw):
        return self.c.post(url, json=json, headers=self.h(**kw.pop("headers", {})), **kw)

    def put(self, url, json=None, **kw):
        return self.c.put(url, json=json, headers=self.h(**kw.pop("headers", {})), **kw)

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def upload(self, catalog_id: str, name: str, data: bytes, chunk: int = 700):
        r = self.post("/v1/uploads", {"catalog_id": catalog_id, "files": [
            {"name": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]})
        assert r.status_code == 201, r.text
        up = r.json()
        url = up["files"][0]["upload_url"]
        for start in range(0, len(data), chunk):
            part = data[start:start + chunk]
            r = self.c.patch(url, content=part, headers=self.h(**{
                "Upload-Offset": str(start), "Upload-Checksum": "sha256 " + hashlib.sha256(part).hexdigest()}))
            assert r.status_code == 204, r.text
        return up["id"], up["files"][0]["id"]

    def file_state(self, upload_id):
        return self.get(f"/v1/uploads/{upload_id}").json()["files"][0]


def drain():
    for _ in range(200):
        if worker.run_once(limit=50) == 0:
            return


def test_journey_upload_map_import_publish_rollback(api):
    u = User(api, "owner@example.test")
    cat = u.post("/v1/catalogs", {"name": "Main"}).json()["id"]
    upload_id, file_id = u.upload(cat, "products.csv", CSV.encode())
    assert u.file_state(upload_id)["state"] == "clean"
    base = f"/v1/uploads/{upload_id}/files/{file_id}"
    pv = u.get(f"{base}/preview").json()
    sm = pv["suggested_mapping"]
    assert sm["key"] == "handle" and sm["columns"]["Variant SKU"] == "sku" and sm["columns"]["Material"] == "attribute:Material"
    assert u.put(f"{base}/mapping", {"mapping": {"columns": {"Title": "title"}, "key": "sku"}}).status_code == 422
    assert u.put(f"{base}/mapping", {"mapping": sm, "save_as": "shopify"}).status_code == 200
    result = u.post(f"{base}/import").json()
    assert result["products"] == {"created": 2, "updated": 0, "unchanged": 0, "error": 0}
    assert result["row_errors"] == 2 and result["total_rows"] == 5
    assert "import-errors" in u.get(f"{base}/errors.csv").headers["content-disposition"]
    # Re-importing the same file changes nothing (HUB-09).
    assert u.post(f"{base}/import").json()["products"]["unchanged"] == 2

    products = {p["external_key"]: p for p in u.get(f"/v1/catalogs/{cat}/products").json()["data"]}
    tee, mug = products["tee"], products["mug"]
    assert len(tee["variants"]) == 2 and tee["variants"][0]["price_minor"] == 1999
    assert tee["variants"][0]["cost_minor"] == 750  # owner may see cost
    assert "script" not in tee["description_html"]["en"] and mug["variants"][0]["price_minor"] == 129950
    assert tee["attributes"] == [{"key": "Material", "value": "Cotton", "source": "explicit"}]

    # Export neutralizes spreadsheet formulas (SEC-UPL-10).
    export = u.post(f"/v1/catalogs/{cat}/exports?format=csv").content.decode("utf-8-sig")
    assert "'=HYPERLINK" in export and "\n=HYPERLINK" not in export and ",=HYPERLINK" not in export

    # AI enhancement proposes a cleaner title; the promo text is gone.
    summary = u.post(f"/v1/catalogs/{cat}/enhance", {"tasks": ["title"]}).json()
    assert summary["proposals"] >= 1
    assert u.get(f"/v1/products/{tee['id']}").json()["title"]["en"] == "Classic Tee"

    # Connect a channel (write-only credential) and publish.
    ch = u.post("/v1/channels", {"platform": "mock", "name": "Sandbox"}).json()["id"]
    secret = "tok_canary_SECRET_0123456789"
    meta = u.put(f"/v1/channels/{ch}/credential", {"kind": "access_token", "secret": {"access_token": secret}})
    assert meta.status_code == 200 and meta.json()["hint"] == "…6789" and secret not in meta.text
    for url in (f"/v1/channels/{ch}/credential", "/v1/channels", f"/v1/channels/{ch}/credential/usage", "/v1/audit-events"):
        assert secret not in u.get(url).text
    job = u.post("/v1/jobs", {"kind": "publish", "catalog_id": cat, "channel_ids": [ch]},
                 headers={"Idempotency-Key": "k1"})
    assert job.status_code == 202
    replay = u.post("/v1/jobs", {"kind": "publish", "catalog_id": cat, "channel_ids": [ch]},
                    headers={"Idempotency-Key": "k1"})
    assert replay.json()["id"] == job.json()["id"] and replay.headers["idempotent-replay"] == "true"
    clash = u.post("/v1/jobs", {"kind": "publish", "catalog_id": cat, "channel_ids": [ch], "options": {"sandbox": True}},
                   headers={"Idempotency-Key": "k1"})
    assert clash.status_code == 422 and clash.json()["code"] == "idempotency_key_reuse"
    job_id = job.json()["id"]
    assert job.json()["state"] == "awaiting_approval" and job.json()["preview"]["writes"] == 2
    assert mock.store_for(ch).writes == 0  # a preview never writes (FR-33)
    items = u.get(f"/v1/jobs/{job_id}/items").json()["data"]
    assert {i["diff"][0]["after"] for i in items} == {"new listing"}
    assert u.post(f"/v1/jobs/{job_id}/approve", {}).json()["state"] == "running"
    drain()
    done = u.get(f"/v1/jobs/{job_id}").json()
    assert done["state"] == "completed" and done["counts"] == {"PUSHED": 2}
    assert len(mock.store_for(ch).products) == 2
    assert all("cost_minor" not in v for p in mock.store_for(ch).products.values() for v in p["variants"])
    assert {row["state"] for row in u.get(f"/v1/channels/{ch}/listings").json()["data"]} == {"live"}

    # J4: roll the job back; what it created is removed.
    rb = u.post(f"/v1/jobs/{job_id}/rollbacks", {}).json()
    assert rb["preview"]["delete"] == 2 and rb["state"] == "awaiting_approval"
    assert u.post(f"/v1/jobs/{rb['id']}/approve", {}).json()["state"] == "rolling_back"
    drain()
    assert u.get(f"/v1/jobs/{rb['id']}").json()["state"] == "rolled_back"
    assert mock.store_for(ch).products == {}

    # Version history and restore; audit chain intact.
    versions = u.get(f"/v1/catalogs/{cat}/versions").json()
    assert versions["head_version"] >= 2 and versions["data"][-1]["source"] == "upload"
    assert u.post(f"/v1/catalogs/{cat}/versions/1/restore").json()["changed"] is True
    assert u.get(f"/v1/products/{tee['id']}").json()["title"]["en"] == "Classic Tee - FREE SHIPPING"
    assert u.get("/v1/audit-events/verify").json()["ok"] is True
    usage = u.get(f"/v1/channels/{ch}/credential/usage").json()["data"]
    assert usage and all(row["result"] == "granted" for row in usage)


def test_csrf_and_authentication_are_enforced(api):
    assert api.get("/v1/catalogs").status_code == 401
    u = User(api, "owner@example.test")
    assert u.c.post("/v1/catalogs", json={"name": "x"}).status_code == 403  # no CSRF token
    assert u.c.post("/v1/catalogs", json={"name": "x"}, headers={"X-CSRF-Token": "wrong"}).json()["code"] == "csrf_failed"
    r = u.c.post("/v1/catalogs", json={"name": "x"}, headers=u.h(Origin="https://evil.example"))
    assert r.status_code == 403
    assert u.post("/v1/catalogs", {"name": "x"}).status_code == 201
    assert u.post("/v1/catalogs", {"name": "x", "is_admin": True}).status_code == 422  # unknown fields rejected
    # Login errors are uniform; repeated failures lock temporarily.
    a = api.post("/v1/auth/login", json={"email": "owner@example.test", "password": "wrong-password-1"})
    b = api.post("/v1/auth/login", json={"email": "nobody@example.test", "password": "wrong-password-1"})
    assert a.status_code == b.status_code == 401 and a.json()["detail"] == b.json()["detail"]
    r = api.post("/v1/auth/register", json={"email": "owner@example.test", "password": PASSWORD})
    assert r.status_code == 202 and "dev_verification_token" not in r.json()  # no account enumeration
    assert api.post("/v1/auth/register", json={"email": "x@example.test", "password": "short"}).status_code == 422
    assert u.post("/v1/auth/logout").status_code == 200
    assert u.get("/v1/catalogs").status_code == 401
    headers = api.get("/healthz").headers
    assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"


def test_cross_tenant_access_is_not_found(api):
    a, b = User(api, "a@example.test", "A"), User(api, "b@example.test", "B")
    cat = a.post("/v1/catalogs", {"name": "A"}).json()["id"]
    upload_id, file_id = a.upload(cat, "p.csv", b"sku,title,price\nS1,Thing,5\n")
    f = f"/v1/uploads/{upload_id}/files/{file_id}"
    a.put(f"{f}/mapping", {"mapping": {"columns": {"sku": "sku", "title": "title", "price": "price"}, "key": "sku"}})
    a.post(f"{f}/import")
    pid = a.get(f"/v1/catalogs/{cat}/products").json()["data"][0]["id"]
    ch = a.post("/v1/channels", {"platform": "mock", "name": "S"}).json()["id"]
    a.put(f"/v1/channels/{ch}/credential", {"kind": "access_token", "secret": {"access_token": "tok_aaaaaaaaaaaaaaaa"}})
    job = a.post("/v1/jobs", {"kind": "publish", "catalog_id": cat, "channel_ids": [ch], "options": {}}).json()
    assert job.get("code") == "nothing_to_publish"  # drafts are never published
    reads = [f"/v1/catalogs/{cat}/products", f"/v1/products/{pid}", f"/v1/catalogs/{cat}/versions",
             f"/v1/uploads/{upload_id}", f"{f}/preview", f"/v1/channels/{ch}/credential",
             f"/v1/channels/{ch}/listings", f"/v1/products/{pid}/readiness"]
    for url in reads:
        assert a.get(url).status_code == 200, url
        assert b.get(url).status_code == 404, url
    assert b.c.patch(f"/v1/products/{pid}", json={"title": "pwned"}, headers=b.h(**{"If-Match": "1"})).status_code == 404
    assert b.post(f"/v1/catalogs/{cat}/exports").status_code == 404
    assert b.post("/v1/jobs", {"kind": "publish", "catalog_id": cat, "channel_ids": [ch]}).status_code == 404
    assert b.post(f"{f}/import").status_code == 404
    assert b.c.delete(f"/v1/channels/{ch}", headers=b.h()).status_code == 404
    assert b.get("/v1/catalogs").json()["data"] == [] and b.get("/v1/channels").json()["data"] == []
    assert a.get(f"/v1/products/{pid}").json()["title"]["en"] == "Thing"


def test_roles_cost_visibility_and_step_up(api):
    owner = User(api, "owner@example.test")
    cat = owner.post("/v1/catalogs", {"name": "Main"}).json()["id"]
    up, fid = owner.upload(cat, "p.csv", b"sku,title,price,cost\nS1,Thing,5,2\n")
    f = f"/v1/uploads/{up}/files/{fid}"
    owner.put(f"{f}/mapping", {"mapping": {"columns": {"sku": "sku", "title": "title", "price": "price", "cost": "cost"}, "key": "sku"}})
    owner.post(f"{f}/import")
    inv = owner.post("/v1/org/invitations", {"email": "ed@example.test", "role": "editor"}).json()
    assert owner.post("/v1/org/invitations", {"email": "x@example.test", "role": "superuser"}).status_code == 403
    editor = User(api, "ed@example.test", "Own org")
    assert editor.post("/v1/invitations/accept", {"token": inv["dev_invitation_token"]}).status_code == 200
    assert editor.post("/v1/invitations/accept", {"token": inv["dev_invitation_token"]}).status_code == 400  # single use
    p = editor.get(f"/v1/catalogs/{cat}/products").json()["data"][0]
    assert "cost_minor" not in p["variants"][0]  # property-level authorization
    assert "cost" not in editor.post(f"/v1/catalogs/{cat}/exports").text.splitlines()[0]
    # An editor cannot smuggle a cost change in, publish, manage channels, members or read audit.
    variant = {**p["variants"][0], "cost_minor": 1, "price_minor": 600}
    r = editor.c.patch(f"/v1/products/{p['id']}", json={"variants": [variant]}, headers=editor.h(**{"If-Match": str(p["version"])}))
    assert r.status_code == 200
    assert owner.get(f"/v1/products/{p['id']}").json()["variants"][0] | {} == {**variant, "cost_minor": 200}
    stale = editor.c.patch(f"/v1/products/{p['id']}", json={"title": "Late"}, headers=editor.h(**{"If-Match": str(p["version"])}))
    assert stale.status_code == 412  # optimistic concurrency
    for method, url, body in [("post", "/v1/channels", {"platform": "mock", "name": "x"}),
                              ("post", "/v1/jobs", {"kind": "publish", "catalog_id": cat, "channel_ids": ["x"]}),
                              ("get", "/v1/org/members", None), ("get", "/v1/audit-events", None),
                              ("post", "/v1/org/freeze", None)]:
        r = editor.post(url, body) if method == "post" else editor.get(url)
        assert r.status_code == 403, url
    # Step-up: sensitive actions need recent authentication.
    from datetime import timedelta

    from sqlalchemy import update

    from catalogpilot import db
    from catalogpilot.ids import utcnow
    from catalogpilot.models import UserSession
    with db.system_scope(), db.session_scope() as s:
        s.execute(update(UserSession).values(step_up_at=utcnow() - timedelta(minutes=30)))
    ch = owner.post("/v1/channels", {"platform": "mock", "name": "S"}).json()["id"]
    cred = {"kind": "access_token", "secret": {"access_token": "tok_aaaaaaaaaaaaaaaa"}}
    assert owner.put(f"/v1/channels/{ch}/credential", cred).json()["code"] == "step_up_required"
    assert owner.post("/v1/auth/step-up", {"password": "wrong-password-xx"}).status_code == 403
    assert owner.post("/v1/auth/step-up", {"password": PASSWORD}).status_code == 200
    assert owner.put(f"/v1/channels/{ch}/credential", cred).status_code == 200
    refused = owner.put(f"/v1/channels/{ch}/credential", {"kind": "access_token", "secret": {"password": "hunter2hunter2"}})
    assert refused.json()["code"] == "credential_type_refused"
    # API keys: scoped, never able to manage credentials, usable without CSRF.
    key = owner.post("/v1/org/api-keys", {"name": "ci", "scopes": ["catalog:read", "channels:admin"]}).json()["key"]
    bearer = {"Authorization": f"Bearer {key}"}
    assert api.get("/v1/catalogs", headers=bearer).status_code == 200
    assert api.post("/v1/catalogs", json={"name": "x"}, headers=bearer).status_code == 403
    assert api.put(f"/v1/channels/{ch}/credential", json=cred, headers=bearer).status_code == 403
    assert api.get("/v1/catalogs", headers={"Authorization": "Bearer cpk_live_nope.nope"}).status_code == 401
    # Removing the member ends their access immediately; the last owner cannot be removed.
    members = owner.get("/v1/org/members").json()["data"]
    ed_id = next(m["user_id"] for m in members if m["email"] == "ed@example.test")
    own_id = next(m["user_id"] for m in members if m["role"] == "owner")
    assert owner.c.delete(f"/v1/org/members/{own_id}", headers=owner.h()).status_code == 409
    assert owner.c.delete(f"/v1/org/members/{ed_id}", headers=owner.h()).status_code == 200
    assert editor.get(f"/v1/catalogs/{cat}/products").status_code in (401, 403, 404)


def test_mfa_login_and_freeze(api):
    import pyotp

    u = User(api, "owner@example.test")
    secret = u.post("/v1/auth/mfa/setup").json()["secret"]
    code = pyotp.TOTP(secret).now()
    assert u.post("/v1/auth/mfa/enable", {"code": "000000"}).status_code == 400
    codes = u.post("/v1/auth/mfa/enable", {"code": code}).json()["recovery_codes"]
    assert len(codes) == 10
    r = u.c.post("/v1/auth/login", json={"email": u.email, "password": PASSWORD})
    u.csrf = r.json()["csrf_token"]
    assert r.json()["mfa_required"] is True
    assert u.get("/v1/catalogs").json()["code"] == "mfa_required"  # password alone is not a session
    assert u.post("/v1/auth/mfa/verify", {"code": code}).status_code == 400  # a TOTP code works once
    assert u.post("/v1/auth/mfa/verify", {"code": codes[0]}).status_code == 200
    assert u.get("/v1/catalogs").status_code == 200
    r = u.c.post("/v1/auth/login", json={"email": u.email, "password": PASSWORD})
    u.csrf = r.json()["csrf_token"]
    assert u.post("/v1/auth/mfa/verify", {"code": codes[0]}).status_code == 400  # recovery codes are single-use
    assert u.post("/v1/auth/mfa/verify", {"code": codes[1]}).status_code == 200
    assert u.post("/v1/org/freeze").json() == {"frozen": True}
    assert u.get("/v1/me").json()["organizations"][0]["frozen"] is True


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _png(width=8, height=8) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, "PNG", pnginfo=None)
    return buf.getvalue()


def test_upload_attack_corpus(api):
    """TEST-14. Every hostile file must end in quarantine; nothing hostile is ever parsed."""
    import struct

    u = User(api, "owner@example.test")
    cat = u.post("/v1/catalogs", {"name": "Main"}).json()["id"]
    eicar = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    bomb_png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 60000, 60000) + b"\x08\x02\x00\x00\x00" + b"\x00" * 64
    xlsm = _zip({"[Content_Types].xml": b"<Types/>", "xl/workbook.xml": b"<workbook/>", "xl/vbaProject.bin": b"macro"})
    hostile = {
        "eicar.csv": b"sku,title\n1," + eicar + b"\n",
        "tool.csv": b"MZ\x90\x00" + b"\x00" * 200,                       # executable with a .csv name
        "page.csv": b"<html><script>alert(1)</script></html>",            # HTML
        "shell.csv": b"#!/bin/sh\nrm -rf /\n",
        "data.csv": _png(),                                               # extension/content mismatch
        "slip.zip": _zip({"../../etc/passwd": b"root"}),
        "abs.zip": _zip({"/etc/passwd": b"root"}),
        "nested.zip": _zip({"inner.zip": _zip({"a.png": _png()})}),
        "bomb.zip": _zip({"zeros.bin": b"\x00" * (120 * 1024 * 1024)}),
        "macro.xlsx": xlsm,
        "bomb.png": bomb_png,
        "poly.png": _png() + b"<script>alert(1)</script>",
        "xxe.xml": b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r><i><t>&x;</t></i><i><t>a</t></i></r>',
        "old.xls": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100,
    }
    for name, data in hostile.items():
        upload_id, file_id = u.upload(cat, name, data, chunk=4 * 1024 * 1024)
        state = u.file_state(upload_id)
        base = f"/v1/uploads/{upload_id}/files/{file_id}"
        if name == "xxe.xml":
            assert state["state"] == "clean"  # well-formed XML passes scanning ...
            assert u.get(f"{base}/preview").status_code == 422  # ... and the DTD is refused at parse time
            continue
        assert state["state"] == "quarantined", f"{name}: {state}"
        assert state["rejected"], name
        assert u.get(f"{base}/preview").status_code == 409, name  # never parsed or shown (HUB-04)
        assert u.post(f"{base}/import").status_code == 409, name

    # Integrity: wrong chunk checksum, wrong offset, too many bytes, wrong whole-file hash.
    good = b"sku,title,price\nS1,Thing,5\n"
    r = u.post("/v1/uploads", {"catalog_id": cat, "files": [{"name": "p.csv", "size": len(good), "sha256": "0" * 64}]}).json()
    url = r["files"][0]["upload_url"]
    bad_sum = u.c.patch(url, content=good[:5], headers=u.h(**{"Upload-Offset": "0", "Upload-Checksum": "sha256 " + "f" * 64}))
    assert bad_sum.status_code == 460
    assert u.c.patch(url, content=good[:5], headers=u.h(**{"Upload-Offset": "3"})).status_code == 409
    assert u.c.patch(url, content=good + b"extra", headers=u.h(**{"Upload-Offset": "0"})).status_code == 413
    assert u.c.patch(url, content=good, headers=u.h(**{"Upload-Offset": "0"})).status_code == 460  # hash mismatch
    assert u.c.head(url).headers["upload-offset"] == "0"  # reset, resumable from the start

    # A resumed upload continues from the stored offset (HUB-02).
    r = u.post("/v1/uploads", {"catalog_id": cat, "files": [
        {"name": "p.csv", "size": len(good), "sha256": hashlib.sha256(good).hexdigest()}]}).json()
    url = r["files"][0]["upload_url"]
    u.c.patch(url, content=good[:10], headers=u.h(**{"Upload-Offset": "0"}))
    offset = int(u.c.head(url).headers["upload-offset"])
    assert offset == 10
    assert u.c.patch(url, content=good[offset:], headers=u.h(**{"Upload-Offset": str(offset)})).headers["upload-complete"] == "1"
    assert u.file_state(r["id"])["state"] == "clean"

    # A clean media ZIP: images are re-encoded, the stray text file is skipped.
    media = _zip({"S1.png": _png(), "notes.txt": b"hello", "S1-2.png": _png(4, 4)})
    upload_id, _ = u.upload(cat, "images.zip", media, chunk=4 * 1024 * 1024)
    state = u.file_state(upload_id)
    assert state["state"] == "clean"
    assert sorted(m["status"] for m in state["media"]) == ["skipped", "stored", "stored"]
    assert not os.environ.get("CP_CLAMD_HOST")
