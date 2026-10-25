#!/usr/bin/env python3
"""
Tarik telemetri SISTEM (kind 1: gatotkaca + mesin tim) dari API produksi ke SQLite lokal, agar bisa di-query seperti telemetry_db.

Keluar : telemetry_db/system_telemetry.db
  system_telemetry(id, ts_ms, recv_ms, sender_id, alias, hostname, data)   id = id baris di server; data = JSON mentah apa adanya
Inkremental: hanya mengambil yang lebih baru dari baris terakhir; aman dijalankan berulang.

Pakai:  SCORE_USER=admin SCORE_PASS=... python3 pull_system_telemetry.py [--full]
(butuh akun superadmin; curl dipakai karena server produksi menolak handshake TLS sebagian klien)
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

BASE = os.environ.get("SCORE_BASE", "https://registration.hackathonilkomundana.id/api/v1")
DB = Path(__file__).resolve().parent.parent / "system_telemetry.db"
KIND_ID = 1


def curl(path, token=None, body=None):
    cfg = [f'url = "{BASE}{path}"', 'header = "Accept: application/json"']
    if token:
        cfg.append(f'header = "Authorization: Bearer {token}"')
    if body is not None:
        cfg += ['request = "POST"', 'header = "Content-Type: application/json"', f"data = {json.dumps(json.dumps(body))}"]
    r = subprocess.run(["curl", "-sS", "--max-time", "120", "-K", "-"], input="\n".join(cfg), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"curl gagal: {r.stderr.strip()}")
    return json.loads(r.stdout)


def main():
    user, pw = os.environ.get("SCORE_USER"), os.environ.get("SCORE_PASS")
    if not user or not pw:
        raise SystemExit("Set SCORE_USER dan SCORE_PASS.")
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS system_telemetry (
            id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, recv_ms INTEGER NOT NULL,
            sender_id INTEGER NOT NULL, alias TEXT NOT NULL, hostname TEXT, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS ix_system_alias_ts ON system_telemetry (alias, ts_ms);
        CREATE INDEX IF NOT EXISTS ix_system_ts ON system_telemetry (ts_ms);""")
    last = 0 if "--full" in sys.argv else (con.execute("SELECT COALESCE(MAX(recv_ms), 0) FROM system_telemetry").fetchone()[0])
    token = curl("/auth/login", body={"username": user, "password": pw})["access_token"]

    added, offset = 0, 0
    while True:
        # `from` inklusif: ambil dari recv_ms terakhir lalu buang duplikat lewat PRIMARY KEY
        q = f"/telemetry?kind_id={KIND_ID}&order=asc&limit=1000&offset={offset}" + (f"&from={last}" if last else "")
        page = curl(q, token)
        rows = []
        for r in page["records"]:
            p = r.get("payload_json")
            if not isinstance(p, dict) or "ts_ms" not in p:
                continue
            rows.append((r["id"], int(p["ts_ms"]), r["request_date"], r["sender_id"], r["alias"] or f"sender{r['sender_id']}", p.get("hostname"), r["payload"]))
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO system_telemetry VALUES (?,?,?,?,?,?,?)", rows)
        con.commit()
        added += con.total_changes - before
        print(f"  halaman offset={offset}: {len(rows)} baris, baru {con.total_changes - before}", flush=True)
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    n, lo, hi = con.execute("SELECT COUNT(*), MIN(ts_ms), MAX(ts_ms) FROM system_telemetry").fetchone()
    print(f"selesai: +{added} baris, total {n}, {lo} .. {hi} -> {DB}")
    for alias, c in con.execute("SELECT alias, COUNT(*) FROM system_telemetry GROUP BY alias ORDER BY alias"):
        print(f"  {alias:10} {c}")


if __name__ == "__main__":
    main()
