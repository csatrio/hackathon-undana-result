#!/usr/bin/env python3
"""
Pengolahan telemetri FINAL (web server, kind 2) dari SQLite lokal -> total mentah per tag -> peringkat juri.

Sumber : backend/telemetries/telemetry_db/hackathon_telemetry.db  (tabel network_telemetry: ts_ms, team_id, data JSON)
Keluar : backend/telemetries/output/
  raw_totals_by_tag.json            total mentah tiap tag (team_id apa adanya), tanpa pemetaan dan tanpa skor
  ranking_merged_levenshtein.json   peringkat setelah tag mirip digabung (varian A: waktu tahan = total latency_us_sum)
  ranking_hold_variants.json        skor varian A vs B (B: waktu tahan = rata-rata latency_us per request)

Kriteria juri : waktu koneksi ditahan 35% | koneksi sukses (ok) 30% | total byte tertulis (bytes_out) 20% | total request 15%
Tiap kriteria dinormalisasi terhadap yang terbaik (= 100) lalu dibobot.
ASUMSI: tidak ada field waktu-tahan-koneksi; latency_us_sum dipakai sebagai proksi.
Penggabungan nama: jarak Levenshtein (tanpa beda huruf besar/kecil) <= 3, kedua nama >= 5 karakter, dan tidak hanya beda angka
(mis. team3 vs team5 sengaja tidak digabung). Tag di EXCLUDE tidak dihitung.

Pakai (membaca salinan DB agar file asli, termasuk -wal, tidak tersentuh):  python3 process_telemetry.py [path.db]
"""
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB = ROOT / "hackathon_telemetry.db"
OUT = ROOT / "output"
EXCLUDE = {"unknown"}  # lalu lintas tanpa tag
SUM_FIELDS = ("requests", "ok", "client_errors", "server_errors", "bytes_in", "bytes_out", "latency_us_sum")
WEIGHTS = {"hold": 0.35, "ok": 0.30, "bytes_out": 0.20, "requests": 0.15}
MAX_DIST = 3
MIN_LEN = 5


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def lev(a, b):
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def read_rows(db=None):
    db = Path(db) if db else DB
    with tempfile.TemporaryDirectory() as tmp:  # salin db + -wal/-shm; buka salinan
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                shutil.copy(src, Path(tmp) / (db.name + suffix))
        con = sqlite3.connect(Path(tmp) / db.name)
        rows = con.execute("SELECT ts_ms, team_id, data FROM network_telemetry ORDER BY id").fetchall()
        con.close()
    return rows


def raw_totals(rows):
    acc = {}
    for ts, tag, data in rows:
        v = json.loads(data)
        a = acc.setdefault(tag, {**{k: 0 for k in SUM_FIELDS}, "latency_us_max": 0, "samples": 0, "first": ts, "last": ts})
        for k in SUM_FIELDS:
            a[k] += v.get(k, 0)
        a["latency_us_max"] = max(a["latency_us_max"], v.get("latency_us_max", 0))
        a["samples"] += 1
        a["first"], a["last"] = min(a["first"], ts), max(a["last"], ts)
    return acc


def merge_groups(acc):
    names = sorted((n for n in acc if n not in EXCLUDE), key=lambda n: -acc[n]["requests"])
    parent = {n: n for n in names}

    def root(n):
        while parent[n] != n:
            n = parent[n]
        return n

    pairs = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if len(a) < MIN_LEN or len(b) < MIN_LEN:
                continue
            if a.lower() != b.lower() and re.sub(r"\d", "", a.lower()) == re.sub(r"\d", "", b.lower()):
                continue  # beda angka saja = mesin/tim berbeda
            d = lev(a.lower(), b.lower())
            if d <= MAX_DIST:
                pairs.append({"a": a, "b": b, "distance": d})
                ra, rb = root(a), root(b)
                if ra != rb:
                    big, small = (ra, rb) if acc[ra]["requests"] >= acc[rb]["requests"] else (rb, ra)
                    parent[small] = big
    groups = {}
    for n in names:
        g = groups.setdefault(root(n), {"group": root(n), "tags": [], "requests": 0, "ok": 0, "bytes_out": 0, "latency_us_sum": 0})
        g["tags"].append(n)
        for k in ("requests", "ok", "bytes_out", "latency_us_sum"):
            g[k] += acc[n][k]
    return list(groups.values()), pairs


def rank(groups, hold):
    metrics = {"hold": hold, "ok": lambda g: g["ok"], "bytes_out": lambda g: g["bytes_out"], "requests": lambda g: g["requests"]}
    best = {k: max(f(g) for g in groups) or 1 for k, f in metrics.items()}
    out = []
    for g in groups:
        pts = {k: 100 * f(g) / best[k] for k, f in metrics.items()}
        out.append((sum(pts[k] * w for k, w in WEIGHTS.items()), g, pts))
    out.sort(key=lambda z: -z[0])
    return out


def main():
    global DB
    if len(sys.argv) > 1:
        DB = Path(sys.argv[1])
    rows = read_rows()
    acc = raw_totals(rows)
    OUT.mkdir(exist_ok=True)

    tags = [{"tag": t, **{**a, "first": iso(a["first"]), "last": iso(a["last"])}} for t, a in sorted(acc.items(), key=lambda x: -x[1]["requests"])]
    raw = {
        "note": "Total mentah per tag (team_id apa adanya) dari telemetry_db; tanpa pemetaan tim dan tanpa skor.",
        "source": str(DB.relative_to(ROOT.parent.parent)) if DB.is_relative_to(ROOT.parent.parent) else str(DB),
        "rows": len(rows), "from": iso(min(r[0] for r in rows)), "to": iso(max(r[0] for r in rows)),
        "totals_all_tags": {k: sum(a[k] for a in acc.values()) for k in SUM_FIELDS}, "tags": tags,
    }
    (OUT / "raw_totals_by_tag.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")

    groups, pairs = merge_groups(acc)
    a_rank = rank(groups, lambda g: g["latency_us_sum"])
    b_rank = rank(groups, lambda g: g["latency_us_sum"] / g["requests"] if g["requests"] else 0)
    (OUT / "ranking_merged_levenshtein.json").write_text(json.dumps({
        "excluded_tags": sorted(EXCLUDE), "merge_rule": f"levenshtein(case-insensitive) <= {MAX_DIST}, panjang >= {MIN_LEN}, bukan beda angka saja",
        "merged_pairs": pairs, "hold_time": "SUM(latency_us_sum) - ASUMSI proksi", "weights": WEIGHTS,
        "ranking": [{"rank": i, "group": g["group"], "score": round(s, 4), "tags": g["tags"], "points": {k: round(v, 4) for k, v in p.items()},
                     "totals": {k: g[k] for k in ("requests", "ok", "bytes_out", "latency_us_sum")}} for i, (s, g, p) in enumerate(a_rank, 1)],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    (OUT / "ranking_hold_variants.json").write_text(json.dumps({
        "A_total_hold": {g["group"]: round(s, 4) for s, g, _ in a_rank},
        "B_avg_hold_per_request": {g["group"]: round(s, 4) for s, g, _ in b_rank},
    }, indent=1, ensure_ascii=False), encoding="utf-8")

    print(f"{len(rows)} baris, {len(acc)} tag -> {OUT}")
    print(f"{'#':>2} {'group':12}{'A score':>9}{'requests':>20}  tags")
    for i, (s, g, _) in enumerate(a_rank[:8], 1):
        print(f"{i:>2} {g['group']:12}{s:9.2f}{g['requests']:20,}  {g['tags']}")


if __name__ == "__main__":
    main()
