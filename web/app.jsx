import { createContext, render } from 'preact';
import { useContext, useEffect, useMemo, useRef, useState } from 'preact/hooks';

const PALETTE = ['#89ceff', '#ffb77a', '#7ee0a0', '#e6a0ff', '#ffd966', '#ff8f8f', '#6fe3e3', '#b4b8ff', '#c4e07a', '#ff9ad5'];
const nf = new Intl.NumberFormat('id-ID');
const fmt = (n) => (n == null ? '-' : nf.format(Math.round(n)));
const compact = (n) => (n >= 1e12 ? (n / 1e12).toFixed(1) + 'T' : n >= 1e9 ? (n / 1e9).toFixed(1) + 'B' : n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : String(Math.round(n)));
const bytes = (n) => (n >= 1e12 ? (n / 1e12).toFixed(2) + ' TB' : n >= 1e9 ? (n / 1e9).toFixed(2) + ' GB' : n >= 1e6 ? (n / 1e6).toFixed(1) + ' MB' : n >= 1e3 ? (n / 1e3).toFixed(1) + ' KB' : n + ' B');
// ---- time zone: API times are UTC epoch ms; the page shows them in the offset chosen in the menu (kept in localStorage)
const NAMED = { 420: 'WIB', 480: 'WITA', 540: 'WIT' };
const OFFSETS = [-720, -660, -600, -570, -540, -480, -420, -360, -300, -240, -210, -180, -120, -60, 0, 60, 120, 180, 210, 240, 270, 300, 330, 345, 360, 390, 420, 480, 525, 540, 570, 600, 630, 660, 720, 765, 780, 840];
const pad = (n) => String(n).padStart(2, '0');
const offLabel = (m) => m === 0 ? 'UTC' : `UTC${m < 0 ? '−' : '+'}${pad(Math.floor(Math.abs(m) / 60))}:${pad(Math.abs(m) % 60)}`;
const loadTz = () => { try { const v = localStorage.getItem('tz'); return v === 'local' || (v !== null && v !== '' && OFFSETS.includes(+v)) ? (v === 'local' ? v : +v) : 'local'; } catch { return 'local'; } };
const offsetAt = (tz, ms) => (tz === 'local' ? -new Date(ms).getTimezoneOffset() : tz);
/** formatters bound to one tz setting; `local` follows the browser (and its daylight saving) */
const makeTz = (tz) => {
  const shift = (ms) => new Date(ms + offsetAt(tz, ms) * 60000).toISOString();
  return {
    tz, clock: (ms) => shift(ms).slice(11, 16), time: (ms) => shift(ms).slice(11, 19), stamp: (ms) => shift(ms).slice(5, 16).replace('T', ' '),
    label: tz === 'local' ? `${offLabel(offsetAt('local', Date.now()))}, local` : offLabel(tz) + (NAMED[tz] ? ` ${NAMED[tz]}` : ''),
  };
};
const TzCtx = createContext(makeTz('local'));
const useTz = () => useContext(TzCtx);
const api = (path) => fetch('/api' + path, { cache: 'no-store' }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(r.status))));

function useApi(path, deps, every = 0) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    let alive = true;
    const load = () => api(path).then((d) => alive && (setData(d), setErr(null))).catch((e) => alive && setErr(String(e.message || e)));
    load();
    const id = every ? setInterval(load, every) : 0;
    return () => { alive = false; clearInterval(id); };
  }, deps);
  return [data, err];
}

/** Line chart: hover shows every series at that time, legend click hides a series, optional log scale (team volumes differ by 10^5). */
function Chart({ times, series, valueFmt = compact, log = false, height = 300 }) {
  const { clock } = useTz();
  const ref = useRef(null);
  const [w, setW] = useState(700);
  const [hover, setHover] = useState(null);
  const [hidden, setHidden] = useState(new Set());
  useEffect(() => {
    const ro = new ResizeObserver(() => setW(ref.current.clientWidth || 700));
    ro.observe(ref.current); setW(ref.current.clientWidth || 700);
    return () => ro.disconnect();
  }, []);
  const m = { l: 52, r: 12, t: 10, b: 24 }, iw = Math.max(60, w - m.l - m.r), ih = height - m.t - m.b;
  const vis = series.filter((s) => !hidden.has(s.name));
  const max = Math.max(1e-9, ...vis.flatMap((s) => s.data.map((v) => v || 0)));
  const segs = (data) => { const out = []; let cur = []; data.forEach((v, i) => { if (v == null) { if (cur.length) out.push(cur), (cur = []); } else cur.push([i, v]); }); if (cur.length) out.push(cur); return out; };
  const sc = (v) => (log ? Math.log10(1 + v) / Math.log10(1 + max) : v / max);
  const x = (i) => m.l + (times.length > 1 ? (i / (times.length - 1)) * iw : iw / 2);
  const y = (v) => m.t + ih - sc(v || 0) * ih;
  const inv = (f) => (log ? Math.pow(1 + max, f) - 1 : max * f);
  const onMove = (e) => {
    const r = e.currentTarget.getBoundingClientRect();
    setHover(Math.max(0, Math.min(times.length - 1, Math.round((((e.clientX - r.left - m.l) / iw)) * (times.length - 1)))));
  };
  return (
    <div ref={ref}>
      <div class="legend">
        {series.map((s) => (
          <button class={hidden.has(s.name) ? 'off' : ''} onClick={() => setHidden((h) => { const n = new Set(h); n.has(s.name) ? n.delete(s.name) : n.add(s.name); return n; })}>
            <i style={{ background: s.color }} />{s.name}
          </button>
        ))}
      </div>
      <div style="position:relative">
        <svg width={w} height={height} onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
          {[0, 0.25, 0.5, 0.75, 1].map((f) => (
            <g key={f}>
              <line x1={m.l} x2={w - m.r} y1={m.t + ih - f * ih} y2={m.t + ih - f * ih} class="grid" />
              <text x={m.l - 6} y={m.t + ih - f * ih + 3} text-anchor="end" class="ax">{valueFmt(inv(f))}</text>
            </g>
          ))}
          {times.length > 0 && [0, Math.floor((times.length - 1) / 2), times.length - 1].map((i) => (
            <text x={x(i)} y={height - 6} class="ax" text-anchor={i === 0 ? 'start' : i === times.length - 1 ? 'end' : 'middle'}>{clock(times[i])}</text>
          ))}
          {vis.flatMap((s) => segs(s.data).map((sg) => (sg.length === 1
            ? <circle cx={x(sg[0][0])} cy={y(sg[0][1])} r="1.8" fill={s.color} />
            : <polyline fill="none" stroke={s.color} stroke-width="1.6" stroke-linejoin="round" points={sg.map(([i, v]) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')} />)))}
          {hover != null && <line x1={x(hover)} x2={x(hover)} y1={m.t} y2={m.t + ih} class="cursor" />}
          {hover != null && vis.filter((s) => s.data[hover] != null).map((s) => <circle cx={x(hover)} cy={y(s.data[hover])} r="3" fill={s.color} />)}
        </svg>
        {hover != null && (
          <div class="tip" style={x(hover) > w / 2 ? { right: w - x(hover) + 10 } : { left: x(hover) + 10 }}>
            <b>{clock(times[hover])}</b>
            {[...vis].filter((s) => s.data[hover] != null).sort((a, b) => b.data[hover] - a.data[hover]).map((s) => (
              <div><span style={{ color: s.color }}>{s.name}</span><span>{valueFmt(s.data[hover])}</span></div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

const METRICS = {
  requests: { label: 'Requests', fmt: compact, get: (s) => s.requests },
  bytes_out: { label: 'Bytes written', fmt: bytes, get: (s) => s.bytes_out },
  ok: { label: 'Successful', fmt: compact, get: (s) => s.ok },
  hold: { label: 'Hold time (latency us)', fmt: compact, get: (s) => s.latency_us_sum },
  avg: { label: 'Avg latency us / request', fmt: (v) => v.toFixed(2), get: (s) => s.requests.map((r, i) => (r ? s.latency_us_sum[i] / r : 0)) },
};

function Series({ top, exclude }) {
  const { label } = useTz();
  const [bucket, setBucket] = useState(60);
  const [metric, setMetric] = useState('requests');
  const [log, setLog] = useState(true);
  const [data, err] = useApi(`/series?bucket=${bucket}&top=${top}`, [bucket, top], 10000);
  const M = METRICS[metric];
  const series = useMemo(() => (data ? data.tags.filter((t) => !exclude.has(t)).map((t, i) => ({ name: t, color: PALETTE[i % PALETTE.length], data: M.get(data.series[t]) })) : []), [data, metric, exclude]);
  return (
    <section>
      <header>
        <h2>Time series ({label})</h2>
        <div class="seg">
          {Object.entries(METRICS).map(([k, v]) => <button class={metric === k ? 'on' : ''} onClick={() => setMetric(k)}>{v.label}</button>)}
        </div>
        <div class="seg">
          {[10, 30, 60, 300].map((b) => <button class={bucket === b ? 'on' : ''} onClick={() => setBucket(b)}>{b < 60 ? b + 's' : b / 60 + 'm'}</button>)}
          <button class={log ? 'on' : ''} onClick={() => setLog(!log)} title="Volumes differ by 10^5; log keeps small tags visible">log</button>
        </div>
      </header>
      {err ? <p class="bad">{err}</p> : data ? <Chart times={data.t} series={series} valueFmt={M.fmt} log={log} /> : <p>loading…</p>}
    </section>
  );
}


// ---------------------------------------------------------------- team labels: fixed names for tags that were not sent with the valid team name
const TEAM_LABELS = {
  RR_VN: 'Tim Mau nama apa? RR_VN',
  7: 'Bebas Aja',
  team5: 'cyber',
};
// a merged group shows the label of the group name, else of the first of its tags that has one
const labelOf = (g) => TEAM_LABELS[g.group] ?? g.tags.map((t) => TEAM_LABELS[t]).find(Boolean) ?? '';
try { localStorage.removeItem('group_labels'); } catch {} // labels used to be editable and saved in the browser; no longer

// ---------------------------------------------------------------- judging criteria
const CRITERIA = [
  { key: 'hold', w: 35, field: 'latency_us_sum', en: 'Hold open connection time', id: 'Lama koneksi ditahan',
    desc_en: 'How long the server was kept busy holding your connections open: the total time of all your requests (microseconds). The longer you keep connections open, the more you score.',
    desc_id: 'Berapa lama server tertahan memegang koneksi Anda: total waktu seluruh request Anda (mikrodetik). Makin lama koneksi Anda terbuka, makin tinggi skor.' },
  { key: 'ok', w: 30, field: 'ok', en: 'Successful connections', id: 'Koneksi sukses',
    desc_en: 'Number of your requests the server answered successfully (no 4xx/5xx error, not aborted).', desc_id: 'Jumlah request Anda yang berhasil dijawab server (bukan error 4xx/5xx, tidak dibatalkan).' },
  { key: 'bytes_out', w: 20, field: 'bytes_out', en: 'Total bytes written', id: 'Total byte tertulis',
    desc_en: 'Total bytes the server wrote back to you in responses.', desc_id: 'Total byte yang ditulis server kembali kepada Anda sebagai respons.' },
  { key: 'requests', w: 15, field: 'requests', en: 'Total request count', id: 'Total jumlah request',
    desc_en: 'Total number of requests you sent to the server.', desc_id: 'Total jumlah request yang Anda kirim ke server.' },
];
const T = {
  title: { en: 'How you are judged', id: 'Cara penilaian' },
  how: { en: 'Each criterion is scored against the best team in that criterion: the best gets 100 points, everyone else gets value ÷ best × 100. The four results are multiplied by their weights and added up. A team that is best at everything scores exactly 100.',
         id: 'Tiap kriteria dinilai terhadap tim terbaik di kriteria itu: yang terbaik mendapat 100 poin, yang lain mendapat nilai ÷ terbaik × 100. Keempat hasil dikalikan bobotnya lalu dijumlahkan. Tim yang terbaik di semua kriteria mendapat tepat 100.' },
  formula: { en: 'score = 0.35 × hold + 0.30 × successful + 0.20 × bytes + 0.15 × requests   (each as points 0–100)', id: 'skor = 0,35 × waktu tahan + 0,30 × sukses + 0,20 × byte + 0,15 × request   (tiap suku berupa poin 0–100)' },
  example: { en: 'Worked example with live numbers', id: 'Contoh perhitungan dengan angka langsung' },
  rules: { en: 'Rules and assumptions', id: 'Aturan dan asumsi' },
};

function CriteriaModal({ data, hold, merge, meta, onClose }) {
  const [lang, setLang] = useLang();
  const { stamp, label } = useTz();
  const [pick, setPick] = useState(0);
  useEffect(() => {
    const k = (e) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', k);
    document.body.style.overflow = 'hidden';
    return () => { window.removeEventListener('keydown', k); document.body.style.overflow = ''; };
  }, []);
  const rk = data?.ranking || [];
  const val = (g, c) => (c.key === 'hold' && hold === 'avg' ? (g.totals.requests ? g.totals.latency_us_sum / g.totals.requests : 0) : g.totals[c.field]);
  const best = Object.fromEntries(CRITERIA.map((c) => [c.key, Math.max(1e-12, ...rk.map((g) => val(g, c)))]));
  const g = rk[Math.min(pick, rk.length - 1)];
  const fmtv = (c, v) => (c.key === 'bytes_out' ? bytes(v) : c.key === 'hold' && hold === 'avg' ? v.toFixed(3) + ' µs' : fmt(v));
  const rules = [
    hold === 'avg'
      ? { en: 'Hold time is currently shown as the AVERAGE time per request (total ÷ requests), so it rewards connections that stay open longer each time.', id: 'Waktu tahan saat ini ditampilkan sebagai RATA-RATA waktu per request (total ÷ request), sehingga menguntungkan koneksi yang terbuka lebih lama tiap kalinya.' }
      : { en: 'Hold time is the TOTAL time of all your requests. The server has no separate "connection held open" counter, so request latency is used as the measure.', id: 'Waktu tahan adalah TOTAL waktu seluruh request Anda. Server tidak punya penghitung khusus "koneksi ditahan", jadi latensi request dipakai sebagai ukurannya.' },
    { en: `Traffic is counted per tag, the team label sent with each request. ${merge ? 'Tags whose names are almost the same (case-insensitive Levenshtein distance ≤ 3, at least 5 characters, not differing only by a digit) are merged into one team.' : 'Tags are NOT merged: every spelling counts as its own team.'}`,
      id: `Lalu lintas dihitung per tag, label tim yang dikirim bersama tiap request. ${merge ? 'Tag dengan nama hampir sama (jarak Levenshtein tanpa beda huruf besar/kecil ≤ 3, minimal 5 karakter, tidak hanya beda satu angka) digabung menjadi satu tim.' : 'Tag TIDAK digabung: setiap penulisan dihitung sebagai tim sendiri.'}` },
    { en: `Excluded from scoring: ${data?.excluded?.length ? data.excluded.join(', ') : 'nothing'}${data?.excluded?.includes('unknown') ? ' (unknown = requests that carried no team tag)' : ''}.`, id: `Tidak dihitung: ${data?.excluded?.length ? data.excluded.join(', ') : 'tidak ada'}${data?.excluded?.includes('unknown') ? ' (unknown = request tanpa tag tim)' : ''}.` },
    { en: `Source: the server's request telemetry${meta ? `, ${fmt(meta.rows)} samples from ${stamp(meta.first_ms)} to ${stamp(meta.last_ms)} (${label})` : ''}. Anything after the last sample is not counted.`, id: `Sumber: telemetri request server${meta ? `, ${fmt(meta.rows)} sampel dari ${stamp(meta.first_ms)} sampai ${stamp(meta.last_ms)} (${label})` : ''}. Yang terjadi setelah sampel terakhir tidak dihitung.` },
    { en: 'This table is a live preview from the data above; the final result is announced by the organisers.', id: 'Tabel ini pratinjau langsung dari data di atas; hasil akhir diumumkan oleh panitia.' },
  ];
  return (
    <div class="backdrop" onClick={onClose}>
      <div class="modal" role="dialog" aria-modal="true" aria-label={T.title[lang]} onClick={(e) => e.stopPropagation()}>
        <header>
          <h2 style="font-size:15px;color:var(--fg)">{T.title[lang]}</h2>
          <div class="seg"><button class={lang === 'en' ? 'on' : ''} onClick={() => setLang('en')}>EN</button><button class={lang === 'id' ? 'on' : ''} onClick={() => setLang('id')}>ID</button>
            <button onClick={onClose} aria-label="close">✕</button></div>
        </header>
        <p>{T.how[lang]}</p>
        <table class="crit">
          <thead><tr><th>{lang === 'en' ? 'Criterion' : 'Kriteria'}</th><th class="r">{lang === 'en' ? 'Weight' : 'Bobot'}</th><th>{lang === 'en' ? 'What counts' : 'Yang dihitung'}</th></tr></thead>
          <tbody>{CRITERIA.map((c, i) => (
            <tr><td><i class="dot" style={{ background: PALETTE[i] }} /><b>{c['' + lang]}</b></td><td class="r"><b>{c.w}%</b></td><td class="wrap">{c['desc_' + lang]}</td></tr>
          ))}</tbody>
        </table>
        <div class="formula">{T.formula[lang]}</div>
        {g && (
          <div>
            <h3>{T.example[lang]}{' — '}<select value={pick} onChange={(e) => setPick(+e.target.value)}>{rk.slice(0, 12).map((x, i) => <option value={i}>#{x.rank} {x.group}{labelOf(x) ? ` — ${labelOf(x)}` : ''}</option>)}</select></h3>
            <table>
              <thead><tr><th>{lang === 'en' ? 'Criterion' : 'Kriteria'}</th><th class="r">{lang === 'en' ? 'Your value' : 'Nilai tim'}</th><th class="r">{lang === 'en' ? 'Best value' : 'Nilai terbaik'}</th><th class="r">{lang === 'en' ? 'Points' : 'Poin'}</th><th class="r">{lang === 'en' ? 'Weight' : 'Bobot'}</th><th class="r">{lang === 'en' ? 'Adds' : 'Menambah'}</th></tr></thead>
              <tbody>
                {CRITERIA.map((c, i) => {
                  const v = val(g, c), pts = (100 * v) / best[c.key];
                  return <tr><td><i class="dot" style={{ background: PALETTE[i] }} />{c['' + lang]}</td><td class="r">{fmtv(c, v)}</td><td class="r dim">{fmtv(c, best[c.key])}</td><td class="r">{pts.toFixed(2)}</td><td class="r dim">× {(c.w / 100).toFixed(2)}</td><td class="r"><b>{((pts * c.w) / 100).toFixed(2)}</b></td></tr>;
                })}
                <tr class="sum"><td colSpan="5" class="r"><b>{lang === 'en' ? 'Total score' : 'Total skor'}</b></td><td class="r"><b>{g.score.toFixed(2)}</b></td></tr>
              </tbody>
            </table>
          </div>
        )}
        <h3>{T.rules[lang]}</h3>
        <ul>{rules.map((r) => <li>{r[lang]}</li>)}</ul>
      </div>
    </div>
  );
}

function Ranking({ exclude, meta }) {
  const [hold, setHold] = useState('total');
  const [merge, setMerge] = useState(true);
  const [data, err] = useApi(`/ranking?hold=${hold}&merge=${merge ? 1 : 0}&exclude=${[...exclude].join(',')}`, [hold, merge, [...exclude].join()], 10000);
  const [info, setInfo] = useState(false);
  const medals = ['🥇', '🥈', '🥉'];
  return (
    <section>
      <header>
        <h2>Judging score</h2>
        <div class="seg">
          <button class="crit-btn" onClick={() => setInfo(true)}>ℹ️ Judging criteria</button>
          <button class={hold === 'total' ? 'on' : ''} onClick={() => setHold('total')} title="hold time = total latency µs">hold: total</button>
          <button class={hold === 'avg' ? 'on' : ''} onClick={() => setHold('avg')} title="hold time = avg latency µs per request">hold: avg</button>
          <button class={merge ? 'on' : ''} onClick={() => setMerge(!merge)} title="merge similar names (Levenshtein ≤ 3)">merge names</button>
        </div>
      </header>
      {err ? <p class="bad">{err}</p> : !data ? <p>loading…</p> : (
        <table>
          <thead><tr><th>#</th><th>Group</th><th>Label</th><th class="r">Score</th><th>Breakdown (35 · 30 · 20 · 15)</th><th class="r">Requests</th><th class="r">Bytes out</th><th>Tags</th></tr></thead>
          <tbody>
            {data.ranking.slice(0, 12).map((r) => (
              <tr class={r.rank <= 3 ? 'podium' : ''}>
                <td>{medals[r.rank - 1] || r.rank}</td><td><b>{r.group}</b></td>
                <td class={labelOf(r) ? 'lbl' : 'lbl dim'}>{labelOf(r) || '–'}</td>
                <td class="r"><b>{r.score.toFixed(2)}</b></td>
                <td>
                  <div class="bars" title={Object.entries(r.points).map(([k, v]) => `${k} ${v.toFixed(2)}`).join(' · ')}>
                    {['hold', 'ok', 'bytes_out', 'requests'].map((k, i) => <span style={{ width: `${Math.max(1, r.points[k] / 4)}%`, background: PALETTE[i] }} />)}
                  </div>
                </td>
                <td class="r">{compact(r.totals.requests)}</td><td class="r">{bytes(r.totals.bytes_out)}</td>
                <td class="dim">{r.tags.join(', ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {info && <CriteriaModal data={data} hold={hold} merge={merge} meta={meta} onClose={() => setInfo(false)} />}
      {data && <p class="note">hold time = {data.hold}. Tags merged: {data.merged_pairs.length ? data.merged_pairs.map((p) => `${p.a}≈${p.b}`).join(', ') : 'none'}.</p>}
    </section>
  );
}

function Tags({ exclude, toggle }) {
  const { time } = useTz();
  const [data, err] = useApi('/tags', [], 10000);
  const [q, setQ] = useState('');
  if (err) return <section><p class="bad">{err}</p></section>;
  const rows = (data?.tags || []).filter((t) => t.tag.toLowerCase().includes(q.toLowerCase()));
  const max = Math.max(1, ...(data?.tags || []).map((t) => t.requests));
  return (
    <section>
      <header><h2>All tags (raw, as sent)</h2><input placeholder="filter…" value={q} onInput={(e) => setQ(e.target.value)} /></header>
      <table>
        <thead><tr><th></th><th>Tag</th><th class="r">Samples</th><th class="r">Requests</th><th>Share</th><th class="r">OK</th><th class="r">Bytes out</th><th class="r">Hold (us)</th><th class="r">Avg (us)</th><th>Last seen</th></tr></thead>
        <tbody>
          {rows.map((t) => (
            <tr class={exclude.has(t.tag) ? 'muted' : ''}>
              <td><input type="checkbox" title="exclude from chart and ranking" checked={exclude.has(t.tag)} onChange={() => toggle(t.tag)} /></td>
              <td><b>{t.tag}</b></td><td class="r">{fmt(t.samples)}</td><td class="r">{fmt(t.requests)}</td>
              <td><div class="bars"><span style={{ width: `${Math.max(0.5, (t.requests / max) * 100)}%`, background: PALETTE[0] }} /></div></td>
              <td class="r">{fmt(t.ok)}</td><td class="r">{bytes(t.bytes_out)}</td><td class="r">{fmt(t.latency_us_sum)}</td><td class="r">{t.avg_us.toFixed(3)}</td>
              <td class="dim">{time(t.last)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}


// ---------------------------------------------------------------- system health tab
const SEV = { critical: '#ff6b6b', high: '#ffa05a', medium: '#ffd966', info: '#8aa0b4' };
const fmtUnit = (v, unit) => {
  if (v == null) return '-';
  if (typeof v === 'string') return v;
  if (unit === 'bytes') return bytes(v);
  if (unit === '%' || unit === 'x') return (Math.round(v * 100) / 100).toString();
  return Math.abs(v) >= 1e6 ? compact(v) : fmt(v);
};
const mfmt = (unit) => (v) => (v == null ? '-' : unit === '%' ? v.toFixed(v < 10 ? 1 : 0) + '%' : unit === 'MB' ? fmt(v) + ' MB' : unit === 'GB' ? v.toFixed(1) + ' GB' : Math.abs(v) >= 1000 ? compact(v) : v.toFixed(v < 10 ? 2 : 0));
const dur = (s) => (s >= 3600 ? (s / 3600).toFixed(1) + ' h' : s >= 60 ? Math.round(s / 60) + ' min' : s + ' s');
const useLang = () => {
  const [lang, setLang] = useState(() => { try { return localStorage.getItem('lang') || 'en'; } catch { return 'en'; } });
  return [lang, (l) => { setLang(l); try { localStorage.setItem('lang', l); } catch {} }];
};

function HostCards({ hosts, focus, setFocus, lang }) {
  return (
    <div class="cards">
      {hosts.map((h, i) => {
        const m = h.mood_now;
        return (
          <button class={'card' + (focus === h.host ? ' on' : '')} onClick={() => setFocus(focus === h.host ? null : h.host)}>
            <div class="row"><span class="emo">{m.emoji}</span><b style={{ color: PALETTE[i % PALETTE.length] }}>{h.stress.now.toFixed(0)}</b></div>
            <div class="name">{h.host === 'gatotkaca' ? '⚡ gatotkaca' : h.host}</div>
            <div class="dim">{m[lang]} · {h.cores} cores · {h.mem_total_gb} GB</div>
            <div class="dim">CPU avg {h.cpu_pct.avg.toFixed(0)}% · p95 {h.cpu_pct.p95.toFixed(0)}%</div>
            <div class="dim">mem max {h.mem_used_pct.max.toFixed(0)}%{h.oom_kills ? <span class="bad"> · 💀 {h.oom_kills} OOM</span> : ''}{h.overflows_total ? <span class="bad"> · 🚫 {compact(h.overflows_total)} dropped</span> : ''}</div>
            <div class="share">{Object.entries(h.mood_share).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([e, v]) => <span title={`${v}% of the time`}>{e}{Math.round(v)}%</span>)}</div>
          </button>
        );
      })}
    </div>
  );
}

function SystemChart({ hosts, metrics, lang }) {
  const { label } = useTz();
  const [metric, setMetric] = useState('stress');
  const [bucket, setBucket] = useState(120);
  const [agg, setAgg] = useState('mean');
  const [data, err] = useApi(`/system/series?metric=${metric}&bucket=${bucket}&agg=${agg}`, [metric, bucket, agg], 30000);
  const d = metrics.derived[metric];
  const series = data ? data.hosts.map((h) => ({ name: h, color: PALETTE[hosts.indexOf(h) % PALETTE.length], data: data.series[h] })) : [];
  return (
    <section>
      <header>
        <h2>Metric over time ({label})</h2>
        <select value={metric} onChange={(e) => setMetric(e.target.value)}>
          {Object.entries(metrics.groups).map(([g, ms]) => <optgroup label={g}>{ms.map((m) => <option value={m}>{metrics.derived[m]['label_' + lang]} ({metrics.derived[m].unit})</option>)}</optgroup>)}
        </select>
        <div class="seg">
          {[30, 120, 300, 900].map((b) => <button class={bucket === b ? 'on' : ''} onClick={() => setBucket(b)}>{b < 60 ? b + 's' : b / 60 + 'm'}</button>)}
          <button class={agg === 'max' ? 'on' : ''} onClick={() => setAgg(agg === 'max' ? 'mean' : 'max')} title="peak in each bucket instead of the average">peak</button>
        </div>
      </header>
      <p class="explain"><b>{d['label_' + lang]}</b> — {d['desc_' + lang]}</p>
      {err ? <p class="bad">{err}</p> : data ? <Chart times={data.t} series={series} valueFmt={mfmt(d.unit)} /> : <p>loading…</p>}
    </section>
  );
}

function Story({ focus, lang }) {
  const { stamp } = useTz();
  const [sev, setSev] = useState('');
  const [data, err] = useApi(`/system/story?${focus ? 'host=' + encodeURIComponent(focus) + '&' : ''}severity=${sev}&limit=300`, [focus, sev], 30000);
  return (
    <section>
      <header>
        <h2>What happened{focus ? ` — ${focus}` : ' — all hosts'}</h2>
        <div class="seg">
          {['', 'critical', 'high', 'medium', 'info'].map((v) => <button class={sev === v ? 'on' : ''} onClick={() => setSev(v)}>{v || 'all'}</button>)}
        </div>
      </header>
      {err ? <p class="bad">{err}</p> : !data ? <p>loading…</p> : (
        <div>
          <p class="note">{data.total} events · {Object.entries(data.counts).map(([k, v]) => `${k.replace(/_/g, ' ')} ${v}`).join(' · ')}{data.total > data.events.length ? ` · showing latest ${data.events.length}` : ''}</p>
          <div class="story">
            {[...data.events].reverse().map((e) => (
              <div class="ev" style={{ borderLeftColor: SEV[e.severity] }}>
                <span class="when">{stamp(e.start_ms)} · {dur(e.duration_s)}</span>
                <b>{e.host}</b> <span class="chip" style={{ background: SEV[e.severity] }}>{e.kind.replace(/_/g, ' ')}</span>
                <span>{e['text_' + lang]}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function Glossary({ hosts, focus, metrics, lang }) {
  const host = focus || 'gatotkaca';
  const [snap, err] = useApi(`/system/snapshot?host=${encodeURIComponent(host)}`, [host], 30000);
  const [q, setQ] = useState('');
  const [mode, setMode] = useState('raw');
  const rows = snap ? snap.rows.filter((r) => (r.key + r[lang]).toLowerCase().includes(q.toLowerCase())) : [];
  const derived = Object.entries(metrics.derived).filter(([k, v]) => (k + v['label_' + lang] + v['desc_' + lang]).toLowerCase().includes(q.toLowerCase()));
  return (
    <section>
      <header>
        <h2>Translation — {mode === 'raw' ? `raw values of ${host} (latest)` : 'derived metrics'}</h2>
        <div class="seg"><button class={mode === 'raw' ? 'on' : ''} onClick={() => setMode('raw')}>raw keys</button><button class={mode === 'derived' ? 'on' : ''} onClick={() => setMode('derived')}>derived</button></div>
        <input placeholder="filter…" value={q} onInput={(e) => setQ(e.target.value)} />
      </header>
      {err ? <p class="bad">{err}</p> : mode === 'raw' ? (
        <table>
          <thead><tr><th>Key</th><th class="r">Value</th><th>Unit</th><th>Kind</th><th>Meaning</th></tr></thead>
          <tbody>{rows.map((r) => <tr><td><b>{r.key}</b></td><td class="r">{Array.isArray(r.value) ? r.value.length + ' items' : fmtUnit(r.value, r.unit)}</td><td class="dim">{r.unit}</td><td class="dim">{r.kind}</td><td class="wrap">{r[lang]}</td></tr>)}</tbody>
        </table>
      ) : (
        <table>
          <thead><tr><th>Metric</th><th>Group</th><th>Unit</th><th>Meaning</th></tr></thead>
          <tbody>{derived.map(([k, v]) => <tr><td><b>{v['label_' + lang]}</b><div class="dim">{k}</div></td><td class="dim">{v.group}</td><td class="dim">{v.unit}</td><td class="wrap">{v['desc_' + lang]}</td></tr>)}</tbody>
        </table>
      )}
    </section>
  );
}

function System() {
  const { stamp, label } = useTz();
  const [lang, setLang] = useLang();
  const [focus, setFocus] = useState(null);
  const [hostsData, err] = useApi('/system/hosts', [], 30000);
  const [metrics, merr] = useApi('/system/metrics', []);
  if (err || merr) return <section><p class="bad">{err || merr} — run scripts/pull_system_telemetry.py to create system_telemetry.db</p></section>;
  if (!hostsData || !metrics) return <section><p>loading…</p></section>;
  const names = hostsData.hosts.map((h) => h.host);
  const stamp0 = hostsData.hosts.reduce((a, h) => Math.min(a, h.first_ms), Infinity), stamp1 = hostsData.hosts.reduce((a, h) => Math.max(a, h.last_ms), 0);
  return (
    <div>
      <p class="dim">{names.length} hosts · {stamp(stamp0)} → {stamp(stamp1)} {label} · snapshot every ~5 s · stress = worst of CPU, memory, load/core, kernel pressure
        {' · '}<span class="seg inline"><button class={lang === 'en' ? 'on' : ''} onClick={() => setLang('en')}>EN</button><button class={lang === 'id' ? 'on' : ''} onClick={() => setLang('id')}>ID</button></span>
        {' '}<span title="mood bands">{metrics.moods.map((m) => m.emoji + '<' + Math.min(100, m.max)).join('  ')}</span></p>
      <HostCards hosts={hostsData.hosts} focus={focus} setFocus={setFocus} lang={lang} />
      <SystemChart hosts={names} metrics={metrics} lang={lang} />
      <Story focus={focus} lang={lang} />
      <Glossary hosts={names} focus={focus} metrics={metrics} lang={lang} />
    </div>
  );
}

function App() {
  const [tz, setTzState] = useState(loadTz);
  const tzf = useMemo(() => makeTz(tz), [tz]);
  const setTz = (v) => { const n = v === 'local' ? 'local' : +v; setTzState(n); try { localStorage.setItem('tz', String(n)); } catch {} };
  const [meta] = useApi('/meta', [], 10000);
  const [exclude, setExclude] = useState(new Set(['unknown']));
  const [top, setTop] = useState(6);
  const [tab, setTab] = useState(() => { try { return location.hash === '#system' ? 'system' : 'web'; } catch { return 'web'; } });
  const go = (t) => { setTab(t); try { history.replaceState(null, '', t === 'system' ? '#system' : '#'); } catch {} };
  const toggle = (t) => setExclude((s) => { const n = new Set(s); n.has(t) ? n.delete(t) : n.add(t); return n; });
  return (
    <main>
      <TzCtx.Provider value={tzf}>
      <div class="pagetop"><h1>FINAL · telemetry</h1>
        <label class="tzmenu" title="Time zone for every time on this page. Saved in this browser.">🕒 <select value={String(tz)} onChange={(e) => setTz(e.target.value)}>
          <option value="local">Browser local ({offLabel(offsetAt('local', Date.now()))})</option>
          {OFFSETS.map((m) => <option value={String(m)}>{offLabel(m)}{NAMED[m] ? ` · ${NAMED[m]}` : ''}</option>)}
        </select></label></div>
      <div class="seg tabs"><button class={tab === 'web' ? 'on' : ''} onClick={() => go('web')}>🌐 Web traffic</button><button class={tab === 'system' ? 'on' : ''} onClick={() => go('system')}>🖥️ System health</button></div>
      {tab === 'system' ? <System /> : <div>
      <p class="dim">{meta ? `${fmt(meta.rows)} rows · ${meta.tags} tags · ${tzf.stamp(meta.first_ms)} → ${tzf.stamp(meta.last_ms)} ${tzf.label} · ${meta.db}` : '…'}
        {' · '}top <select value={top} onChange={(e) => setTop(+e.target.value)}>{[3, 6, 8, 10, 15].map((n) => <option>{n}</option>)}</select> tags in chart</p>
      <Ranking exclude={exclude} meta={meta} />
      <Series top={top} exclude={exclude} />
      <Tags exclude={exclude} toggle={toggle} />
      </div>}
      </TzCtx.Provider>
    </main>
  );
}

render(<App />, document.getElementById('app'));
