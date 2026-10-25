#!/usr/bin/env python3
"""Serve the telemetry SQLite (hackathon_telemetry.db) as JSON for a frontend.

    python3 app.py [--db PATH] [--port 8010] [--host 127.0.0.1]

Stdlib only. The database (with its -wal/-shm) is copied and re-read only when those files change,
so the original is never touched and a live file updates on the next request. CORS is open (read-only API).

    GET /                                               the Preact chart page (static/, built from web/app.jsx)
    GET /api/meta                                       rows, time range, tags, db mtime
    GET /api/tags[?exclude=unknown,x]                   raw totals per tag exactly as sent
    GET /api/series?bucket=10[&tags=a,b][&top=6][&from=ms][&to=ms]
                                                        columnar time series per tag (default: top 6 by requests)
    GET /api/ranking[?merge=1][&hold=total|avg][&exclude=unknown]
                                                        judging score (35% hold, 30% ok, 20% bytes_out, 15% requests)
    GET /api/system/hosts[?hosts=a,b]                   per host summary: cores, RAM, avg/p95/max CPU/memory/stress, OOM kills, mood now
    GET /api/system/metrics                             the translation: every derived + raw metric with label, unit, plain-language meaning (en/id)
    GET /api/system/series?metric=cpu_pct&bucket=60[&hosts=a,b][&agg=mean|max][&from=ms][&to=ms]
    GET /api/system/story[?host=a][&severity=critical,high]   what happened, in plain language (CPU saturated, OOM, dropped connections, ...)
    GET /api/system/snapshot?host=a                     latest raw values of a host, each with its translation
    GET /api/raw?tag=x[&limit=500][&offset=0]           individual rows of one tag, newest first
"""
import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import system_metrics as sm  # system telemetry: derived metrics, translation, story
from scripts import process_telemetry as pt

DEFAULT_DB = (HERE / "hackathon_telemetry.db") if (HERE / "hackathon_telemetry.db").exists() else (HERE / "telemetry_db" / "hackathon_telemetry.db")
SYSTEM_DB = (HERE / "system_telemetry.db") if (HERE / "system_telemetry.db").exists() else (HERE / "telemetry_db" / "system_telemetry.db")
FIELDS = ("requests", "ok", "client_errors", "server_errors", "bytes_in", "bytes_out", "latency_us_sum")

pt.EXCLUDE = set()  # exclusion is applied per request (?exclude=), not through the module global


class Store:
    """Rows of network_telemetry, reloaded when the db/-wal/-shm files change."""

    def __init__(self, db):
        self.db, self.lock, self.sig, self.rows = Path(db), threading.Lock(), None, []

    def _signature(self):
        sig = []
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db) + suffix)
            sig.append((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None)
        return tuple(sig)

    def get(self):
        with self.lock:
            sig = self._signature()
            if sig != self.sig:
                self.rows = [(ts, tag, json.loads(d)) for ts, tag, d in pt.read_rows(self.db)]
                self.sig = sig
            return self.rows

    def mtime(self):
        return self.db.stat().st_mtime


def iso(ms):
    return pt.iso(ms)


def csv(q, key):
    return [x for x in q.get(key, [""])[0].split(",") if x]


def totals(rows):
    acc = {}
    for ts, tag, v in rows:
        a = acc.setdefault(tag, {**{k: 0 for k in FIELDS}, "latency_us_max": 0, "samples": 0, "first": ts, "last": ts})
        for k in FIELDS:
            a[k] += v.get(k, 0)
        a["latency_us_max"] = max(a["latency_us_max"], v.get("latency_us_max", 0))
        a["samples"] += 1
        a["first"], a["last"] = min(a["first"], ts), max(a["last"], ts)
    return acc


def api_meta(store, q):
    rows = store.get()
    ts = [r[0] for r in rows]
    return dict(rows=len(rows), tags=len({r[1] for r in rows}), first_ms=min(ts, default=None), last_ms=max(ts, default=None),
                from_iso=iso(min(ts)) if ts else None, to_iso=iso(max(ts)) if ts else None, db_mtime=store.mtime(), db=str(store.db.name))


def api_tags(store, q):
    skip = set(csv(q, "exclude"))
    out = []
    for tag, a in sorted(totals(store.get()).items(), key=lambda x: -x[1]["requests"]):
        if tag in skip:
            continue
        out.append(dict(tag=tag, **a, first_iso=iso(a["first"]), last_iso=iso(a["last"]),
                        avg_us=a["latency_us_sum"] / a["requests"] if a["requests"] else 0,
                        ok_pct=100 * a["ok"] / a["requests"] if a["requests"] else 0))
    return dict(tags=out)


def api_series(store, q):
    size = max(1, int(q.get("bucket", ["10"])[0])) * 1000
    lo = int(q["from"][0]) if "from" in q else None
    hi = int(q["to"][0]) if "to" in q else None
    rows = [r for r in store.get() if (lo is None or r[0] >= lo) and (hi is None or r[0] <= hi)]
    tags = csv(q, "tags")
    if not tags:
        tot = totals(rows)
        tags = sorted(tot, key=lambda t: -tot[t]["requests"])[:int(q.get("top", ["6"])[0])]
    keep = set(tags)
    cells = {}
    for ts, tag, v in rows:
        if tag in keep:
            c = cells.setdefault(ts // size * size, {}).setdefault(tag, dict(requests=0, ok=0, bytes_out=0, latency_us_sum=0))
            for k in c:
                c[k] += v.get(k, 0)
    times = sorted(cells)
    series = {t: {k: [cells[x].get(t, {}).get(k, 0) for x in times] for k in ("requests", "ok", "bytes_out", "latency_us_sum")} for t in tags}
    return dict(bucket_s=size // 1000, t=times, tags=tags, series=series)


def api_ranking(store, q):
    skip = set(csv(q, "exclude")) if "exclude" in q else {"unknown"}
    acc = {t: a for t, a in totals(store.get()).items() if t not in skip}
    if q.get("merge", ["1"])[0] == "0":
        groups = [dict(group=n, tags=[n], **{k: a[k] for k in ("requests", "ok", "bytes_out", "latency_us_sum")}) for n, a in acc.items()]
        pairs = []
    else:
        groups, pairs = pt.merge_groups(acc)
    avg = q.get("hold", ["total"])[0] == "avg"
    hold = (lambda g: g["latency_us_sum"] / g["requests"] if g["requests"] else 0) if avg else (lambda g: g["latency_us_sum"])
    ranked = pt.rank(groups, hold)
    return dict(hold="avg_per_request" if avg else "total_latency_us_sum (ASUMSI proksi)", weights=pt.WEIGHTS, excluded=sorted(skip),
                merged_pairs=pairs, ranking=[dict(rank=i, group=g["group"], score=round(s, 4), tags=g["tags"],
                points={k: round(v, 4) for k, v in p.items()}, totals={k: g[k] for k in ("requests", "ok", "bytes_out", "latency_us_sum")})
                for i, (s, g, p) in enumerate(ranked, 1)])


def api_raw(store, q):
    tag = q.get("tag", [None])[0]
    if tag is None:
        raise ValueError("tag wajib diisi")
    limit, offset = min(int(q.get("limit", ["500"])[0]), 5000), int(q.get("offset", ["0"])[0])
    rows = [r for r in store.get() if r[1] == tag][::-1]
    return dict(tag=tag, total=len(rows), rows=[dict(ts_ms=ts, time=iso(ts), **v) for ts, _, v in rows[offset:offset + limit]])


STATIC = HERE / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


def api_index(store, q):
    return dict(endpoints=sorted(ROUTES), db=str(store.db))


class SystemStore:
    """SystemData, rebuilt when system_telemetry.db changes (refresh it with scripts/pull_system_telemetry.py)."""

    def __init__(self, db):
        self.db, self.sig, self.data, self.lock = Path(db), None, None, threading.Lock()

    def get(self):
        with self.lock:
            if not self.db.exists():
                raise ValueError(f"{self.db.name} belum ada; jalankan scripts/pull_system_telemetry.py")
            st = self.db.stat()
            sig = (st.st_mtime_ns, st.st_size)
            if sig != self.sig:
                self.data, self.sig = sm.SystemData(self.db), sig
            return self.data


SYSTEM = SystemStore(SYSTEM_DB)


def _hosts(q):
    sd = SYSTEM.get()
    want = csv(q, "hosts") or csv(q, "host")
    bad = [h for h in want if h not in sd.hosts]
    if bad:
        raise ValueError(f"host tidak dikenal: {bad}")
    return sd, want or sorted(sd.hosts, key=lambda h: (h != "gatotkaca", h))


def api_system_hosts(store, q):
    sd, hosts = _hosts(q)
    return dict(hosts=[sm.summary(sd, h) for h in hosts])


def api_system_metrics(store, q):
    where = {m: g for g, ms in sm.DERIVED_GROUPS.items() for m in ms}
    return dict(groups=sm.DERIVED_GROUPS, moods=[dict(max=hi, emoji=e, en=en, id=idn) for hi, e, en, idn in sm.MOODS],
                derived={k: dict(label_en=v[0], label_id=v[1], unit=v[2], desc_en=v[3], desc_id=v[4], group=where.get(k)) for k, v in sm.DERIVED.items()},
                raw={k: dict(unit=v[0], kind=v[1], en=v[2], id=v[3]) for k, v in sm.RAW.items()})


def api_system_series(store, q):
    sd, hosts = _hosts(q)
    metric = q.get("metric", ["stress"])[0]
    if metric not in sm.DERIVED:
        raise ValueError(f"metric tidak dikenal: {metric}")
    bucket = max(5, int(q.get("bucket", ["60"])[0]))
    agg = "max" if q.get("agg", ["mean"])[0] == "max" else "mean"
    lo = int(q["from"][0]) if "from" in q else None
    hi = int(q["to"][0]) if "to" in q else None
    cells = {h: sd.series(h, metric, bucket, agg, lo, hi) for h in hosts}
    times = sorted({t for c in cells.values() for t in c})
    return dict(metric=metric, bucket_s=bucket, agg=agg, t=times, hosts=hosts, series={h: [cells[h].get(t) for t in times] for h in hosts})


def api_system_story(store, q):
    sd, hosts = _hosts(q)
    sev = set(csv(q, "severity"))
    ev = [e for h in hosts for e in sm.story(sd, h) if not sev or e["severity"] in sev]
    ev.sort(key=lambda e: e["start_ms"])
    limit = min(int(q.get("limit", ["500"])[0]), 5000)
    counts = {}
    for e in ev:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    return dict(total=len(ev), counts=counts, events=ev[-limit:])


def api_system_snapshot(store, q):
    sd, hosts = _hosts(q)
    if len(hosts) != 1:
        raise ValueError("host wajib diisi (satu)")
    return dict(host=hosts[0], ts_ms=sd.hosts[hosts[0]]["last"], rows=sm.snapshot(sd, hosts[0]))


ROUTES = {"/api/system/hosts": api_system_hosts, "/api/system/metrics": api_system_metrics, "/api/system/series": api_system_series,
          "/api/system/story": api_system_story, "/api/system/snapshot": api_system_snapshot, "/api": api_index, "/api/meta": api_meta, "/api/tags": api_tags, "/api/series": api_series, "/api/ranking": api_ranking, "/api/raw": api_raw}


def make_handler(store):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.end_headers()

        def do_GET(self):
            u = urlparse(self.path)
            name = "index.html" if u.path == "/" else u.path.lstrip("/")
            f = (STATIC / name).resolve()
            if f.parent == STATIC.resolve() and f.suffix in TYPES and f.is_file():  # flat static dir only
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", TYPES[f.suffix])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(body)
            fn = ROUTES.get(u.path)
            if not fn:
                return self._send(404, dict(error="not found"))
            try:
                self._send(200, fn(store, parse_qs(u.query, keep_blank_values=True)))
            except (ValueError, KeyError) as e:
                self._send(400, dict(error=f"parameter tidak valid: {e}"))
            except Exception as e:  # keep the server alive on a bad db read
                self._send(500, dict(error=str(e)))

        def log_message(self, fmt, *a):
            pass

    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    store = Store(a.db)
    n = len(store.get())
    print(f"{n} rows from {a.db}\nhttp://{a.host}:{a.port}/  (page)  {', '.join(sorted(ROUTES))}")
    ThreadingHTTPServer((a.host, a.port), make_handler(store)).serve_forever()


if __name__ == "__main__":
    main()
