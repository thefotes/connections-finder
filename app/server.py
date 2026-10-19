"""HTTP server for the local outreach board.

Read path:  data/06-final.json held in memory, reloaded when its mtime changes.
Write path: data/outreach.db. Stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pipeline.common import DATA, ROOT

from . import db

STATIC = Path(__file__).parent / "static"
FINAL_JSON = DATA / "06-final.json"
PORT = 8765


class DataStore:
    """Holds 06-final.json in memory; reloads when the file mtime changes."""

    def __init__(self) -> None:
        self._mtime: float | None = None
        self._data: dict | None = None

    def get(self) -> dict:
        mtime = FINAL_JSON.stat().st_mtime
        if self._data is None or mtime != self._mtime:
            self._data = json.loads(FINAL_JSON.read_text(encoding="utf-8"))
            self._mtime = mtime
        return self._data


STORE = DataStore()


def badge_for(role: dict, outreach_by_company: dict, role_owner: dict) -> str | None:
    """Compute the outreach badge for a role from the DB (C's outreach_status
    field is not assumed to exist yet)."""
    url = role.get("url")
    if url in role_owner:
        return outreach_by_company[role_owner[url]]["status"]
    own = outreach_by_company.get(role.get("company_normalized"))
    if own:
        return f"company:{own['status']}"
    return None


def bootstrap() -> dict:
    data = STORE.get()
    with db.connect() as conn:
        outreaches = db.list_outreaches(conn)
    outreach_by_company = {o["company_normalized"]: o for o in outreaches}
    role_owner = {}
    for o in outreaches:
        for url in o["role_urls"]:
            role_owner[url] = o["company_normalized"]

    roles = []
    for r in data.get("roles", []):
        r = dict(r)
        r["outreach_status"] = badge_for(r, outreach_by_company, role_owner)
        r.setdefault("ml_deboost_applied", False)
        r.setdefault("ml_deboost_reason", None)
        r.setdefault("pre_deboost_score", r.get("final_score"))
        roles.append(r)

    recruiters = []
    rec_marks = {
        o["company_normalized"].removeprefix("recruiter:"): o["status"]
        for o in outreaches
        if o["company_normalized"].startswith("recruiter:")
    }
    for r in data.get("recruiters", []):
        r = dict(r)
        r["outreach_status"] = rec_marks.get(r.get("profile_url", ""))
        recruiters.append(r)

    return {
        "meta": data.get("meta", {}),
        "stats": data.get("stats", {}),
        "tracks": data.get("tracks", []),
        "roles": roles,
        "recruiters": recruiters,
        "outreaches": outreaches,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

    def _static(self, name: str, ctype: str) -> None:
        path = (STATIC / name).resolve()
        if not str(path).startswith(str(STATIC)) or not path.is_file():
            self._json(404, {"error": "not found"})
            return
        self._send(200, path.read_bytes(), ctype)

    _ALLOWED_ORIGINS = (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}")

    def _same_origin(self) -> bool:
        """No auth by design, so a foreign page must not be able to drive the write API.
        A browser always sends Origin on cross-site POST/PATCH/DELETE; curl and the app's
        own fetch() from the served page are both fine."""
        origin = self.headers.get("Origin")
        if origin and origin not in self._ALLOWED_ORIGINS:
            return False
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1", "")

    def _guard(self) -> bool:
        if self._same_origin():
            return True
        self._json(403, {"error": "cross-origin request refused"})
        return False

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._static("index.html", "text/html; charset=utf-8")
        elif path == "/style.css":
            self._static("style.css", "text/css; charset=utf-8")
        elif path == "/app.js":
            self._static("app.js", "text/javascript; charset=utf-8")
        elif path == "/api/bootstrap":
            try:
                self._json(200, bootstrap())
            except FileNotFoundError:
                self._json(503, {"error": "data/06-final.json not found — run stage 6 first"})
        else:
            self._json(404, {"error": "not found"})

    # -- writes ------------------------------------------------------------
    def do_POST(self):
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        try:
            body = self._body()
            with db.connect() as conn:
                if path == "/api/outreach":
                    self._create(conn, body)
                elif path == "/api/outreach/attach-roles":
                    self._attach_roles(conn, body)
                elif path == "/api/outreach/detach-role":
                    self._detach_role(conn, body)
                else:
                    self._json(404, {"error": "not found"})
        except (sqlite3.Error, KeyError, ValueError) as e:
            self._json(400, {"error": str(e)})

    def do_DELETE(self):
        if not self._guard():
            return
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        try:
            with db.connect() as conn:
                if len(parts) == 3 and parts[:2] == ["api", "outreach"]:
                    cur = conn.execute("DELETE FROM outreach WHERE id = ?", (int(parts[2]),))
                    self._json(200, {"ok": True, "deleted": cur.rowcount})
                else:
                    self._json(404, {"error": "not found"})
        except (sqlite3.Error, ValueError) as e:
            self._json(400, {"error": str(e)})

    def do_PATCH(self):
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        parts = path.strip("/").split("/")
        try:
            body = self._body()
            with db.connect() as conn:
                if len(parts) == 3 and parts[:2] == ["api", "outreach"]:
                    self._update(conn, int(parts[2]), body)
                else:
                    self._json(404, {"error": "not found"})
        except (sqlite3.Error, KeyError, ValueError) as e:
            self._json(400, {"error": str(e)})

    # -- handlers ----------------------------------------------------------
    def _create(self, conn: sqlite3.Connection, body: dict):
        company_norm = (body.get("company_normalized") or "").strip()
        company_display = (body.get("company_display") or "").strip()
        status = body.get("status") or "contacted"
        if status not in db.VALID_STATUSES:
            self._json(400, {"error": f"status must be one of {db.VALID_STATUSES}"})
            return
        if not company_norm or not company_display:
            self._json(400, {"error": "company_normalized and company_display required"})
            return
        existing = conn.execute(
            "SELECT id FROM outreach WHERE company_normalized = ?", (company_norm,)
        ).fetchone()
        if existing:
            self._json(409, {"error": "company already has an outreach",
                             "outreach_id": existing["id"]})
            return
        role_urls = body.get("role_urls") or []
        # roles already owned by another outreach are dropped, not errored
        taken = set()
        if role_urls:
            q = ",".join("?" * len(role_urls))
            taken = {r["role_url"] for r in conn.execute(
                f"SELECT role_url FROM outreach_role WHERE role_url IN ({q})", role_urls)}
        role_urls = [u for u in role_urls if u not in taken]
        cur = conn.execute(
            "INSERT INTO outreach (company_normalized, company_display, person_profile_url,"
            " person_name, status, contacted_at, updated_at, notes)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (company_norm, company_display, body.get("person_profile_url"),
             body.get("person_name"), status, body.get("contacted_at") or db.now_iso(),
             db.now_iso(), body.get("notes")),
        )
        oid = cur.lastrowid
        conn.executemany(
            "INSERT INTO outreach_role (outreach_id, role_url) VALUES (?,?)",
            [(oid, u) for u in role_urls],
        )
        self._json(201, {"ok": True, "outreach_id": oid, "attached_roles": len(role_urls)})

    def _update(self, conn: sqlite3.Connection, oid: int, body: dict):
        row = conn.execute("SELECT * FROM outreach WHERE id = ?", (oid,)).fetchone()
        if not row:
            self._json(404, {"error": "no such outreach"})
            return
        fields, vals = [], []
        for key in ("status", "notes", "person_name", "person_profile_url", "contacted_at"):
            if key in body:
                if key == "status" and body[key] not in db.VALID_STATUSES:
                    self._json(400, {"error": f"status must be one of {db.VALID_STATUSES}"})
                    return
                fields.append(f"{key} = ?")
                vals.append(body[key])
        if fields:
            fields.append("updated_at = ?")
            vals.append(db.now_iso())
            vals.append(oid)
            conn.execute(f"UPDATE outreach SET {', '.join(fields)} WHERE id = ?", vals)
        self._json(200, {"ok": True})

    def _attach_roles(self, conn: sqlite3.Connection, body: dict):
        oid = int(body["outreach_id"])
        if not conn.execute("SELECT 1 FROM outreach WHERE id = ?", (oid,)).fetchone():
            self._json(404, {"error": "no such outreach"})
            return
        attached = skipped = 0
        for url in body.get("role_urls") or []:
            try:
                conn.execute(
                    "INSERT INTO outreach_role (outreach_id, role_url) VALUES (?,?)",
                    (oid, url))
                attached += 1
            except sqlite3.IntegrityError:
                skipped += 1
        conn.execute("UPDATE outreach SET updated_at = ? WHERE id = ?",
                     (db.now_iso(), oid))
        self._json(200, {"ok": True, "attached": attached, "skipped": skipped})

    def _detach_role(self, conn: sqlite3.Connection, body: dict):
        cur = conn.execute("DELETE FROM outreach_role WHERE role_url = ?",
                           (body["role_url"],))
        self._json(200, {"ok": True, "removed": cur.rowcount})


def main() -> None:
    db.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Connections Finder board → http://localhost:{PORT}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
