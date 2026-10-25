#!/usr/bin/env python3
"""
Skor FINAL (telemetri kind 2 = web server), seluruh data dari awal.

Kriteria (bobot): lama koneksi ditahan 35% | koneksi sukses 30% | total byte tertulis 20% | total jumlah request 15%
  - lama koneksi ditahan : SUM(latency_us_sum)   (ASUMSI: tidak ada field khusus; latensi layanan dipakai sebagai proksi)
  - koneksi sukses       : SUM(ok)
  - total byte tertulis  : SUM(bytes_out)
  - total request        : SUM(requests)
Tiap kriteria dinormalisasi terhadap tim terbaik (terbaik = 100), lalu dibobot. ID telemetri dipetakan ke tim terdaftar
(kode NX-xxxxx, nomor squad, atau nama); ID yang tidak cocok TIDAK dihitung tetapi dilaporkan.

Pakai:  SCORE_USER=admin SCORE_PASS=... python3 backend/telemetries/scripts/score_from_api.py [keluaran.json]
"""
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

BASE = os.environ.get("SCORE_BASE", "https://registration.hackathonilkomundana.id/api/v1")
OUT = sys.argv[1] if len(sys.argv) > 1 else "api_scores_registered_teams.json"
WEIGHTS = {"hold_time": 0.35, "success": 0.30, "bytes_out": 0.20, "requests": 0.15}
STAFF_SQUADS = {"SUPERADMIN"}


def curl(path, token=None, body=None):
    # curl dipakai (bukan urllib/Node) karena server produksi menolak handshake TLS sebagian klien
    cfg = [f'url = "{BASE}{path}"', 'header = "Accept: application/json"']
    if token:
        cfg.append(f'header = "Authorization: Bearer {token}"')
    if body is not None:
        cfg += ['request = "POST"', 'header = "Content-Type: application/json"', f"data = {json.dumps(json.dumps(body))}"]
    r = subprocess.run(["curl", "-sS", "--max-time", "60", "-K", "-"], input="\n".join(cfg), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"curl gagal: {r.stderr.strip()}")
    return json.loads(r.stdout)


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def main():
    user, pw = os.environ.get("SCORE_USER"), os.environ.get("SCORE_PASS")
    if not user or not pw:
        raise SystemExit("Set SCORE_USER dan SCORE_PASS.")
    token = curl("/auth/login", body={"username": user, "password": pw})["access_token"]

    records, offset = [], 0
    while True:
        page = curl(f"/telemetry?kind_id=2&order=asc&limit=1000&offset={offset}", token)
        records += page["records"]
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    squads = [s for s in curl("/teams/all", token)["teams"] if s["name"] not in STAFF_SQUADS]

    # ---- total mentah per ID telemetri
    raw = defaultdict(lambda: defaultdict(int))
    first = {}
    last = {}
    for r in records:
        p = r["payload_json"] or {}
        t = p.get("ts_ms") or r["request_date"]
        for tid, v in (p.get("teams") or {}).items():
            a = raw[tid]
            for k in ("requests", "ok", "client_errors", "server_errors", "bytes_in", "bytes_out", "latency_us_sum"):
                a[k] += v.get(k, 0)
            a["latency_us_max"] = max(a["latency_us_max"], v.get("latency_us_max", 0))
            a["intervals"] += 1
            first.setdefault(tid, t)
            last[tid] = t

    # ---- pemetaan ID -> tim terdaftar
    by_code = {s["code"].casefold(): s for s in squads}
    by_id = {str(s["id"]): s for s in squads}
    by_name = defaultdict(list)
    for s in squads:
        by_name[norm(s["name"])].append(s)

    def match(tid):
        if tid.casefold() in by_code:
            return by_code[tid.casefold()], "kode tim"
        if tid in by_id:
            return by_id[tid], "nomor squad"
        n = norm(tid)
        if n and len(by_name.get(n, [])) == 1:
            return by_name[n][0], "nama sama"
        if len(n) >= 4:
            hits = [s for s in squads if n in norm(s["name"])]
            if len(hits) == 1:
                return hits[0], "nama mengandung"
        return None, None

    teams = {}
    unmatched = {}
    mapping = {}
    for tid, a in raw.items():
        s, how = match(tid)
        row = {k: a[k] for k in ("requests", "ok", "client_errors", "server_errors", "bytes_in", "bytes_out", "latency_us_sum", "intervals")}
        row["latency_us_max"] = a["latency_us_max"]
        if s is None:
            unmatched[tid] = row
            continue
        mapping[tid] = {"team": s["name"], "code": s["code"], "cara": how}
        t = teams.setdefault(s["code"], {"code": s["code"], "name": s["name"], "ids": [], "totals": defaultdict(int)})
        t["ids"].append(tid)
        for k, v in row.items():
            if k == "latency_us_max":
                t["totals"][k] = max(t["totals"][k], v)
            else:
                t["totals"][k] += v

    def rank(pool):
        metric = {"hold_time": "latency_us_sum", "success": "ok", "bytes_out": "bytes_out", "requests": "requests"}
        best = {c: max((t["totals"][m] for t in pool), default=0) or 1 for c, m in metric.items()}
        out = []
        for t in pool:
            parts = {c: round(100 * t["totals"][m] / best[c], 4) for c, m in metric.items()}
            total = sum(parts[c] * w for c, w in WEIGHTS.items())
            out.append({"name": t["name"], "code": t["code"], "ids": sorted(t["ids"]), "score": round(total, 4), "points": parts,
                        "raw": {c: t["totals"][m] for c, m in metric.items()}, "totals": dict(t["totals"])})
        out.sort(key=lambda x: -x["score"])
        for i, x in enumerate(out, 1):
            x["rank"] = i
        return out

    scored = rank(list(teams.values()))
    # Pembanding: andai ID tak dikenal (team3/team5/unknown/...) diperlakukan sebagai tim sendiri
    extra = [{"code": f"?{k}", "name": k, "ids": [k], "totals": defaultdict(int, v)} for k, v in unmatched.items() if v["requests"] >= 100_000]
    with_unmatched = rank(list(teams.values()) + extra)

    ts = [r["payload_json"].get("ts_ms") or r["request_date"] for r in records]
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data": {"kind_id": 2, "records": len(records), "from": datetime.fromtimestamp(min(ts) / 1000, timezone.utc).isoformat(),
                 "to": datetime.fromtimestamp(max(ts) / 1000, timezone.utc).isoformat(), "interval_s": 10},
        "criteria": {"weights": WEIGHTS, "normalisasi": "nilai / nilai terbaik * 100", "hold_time": "SUM(latency_us_sum) - ASUMSI proksi"},
        "ranking": scored,
        "id_mapping": mapping,
        "unmatched_ids_not_scored": unmatched,
        "reference_ranking_if_large_unmatched_ids_counted": [{"rank": x["rank"], "name": x["name"], "score": x["score"]} for x in with_unmatched],
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"{len(records)} record -> {OUT}")
    for x in scored[:10]:
        print(f"{x['rank']:>2}. {x['name'][:28]:28} {x['score']:7.2f}  {x['points']}")


if __name__ == "__main__":
    main()
