import { useEffect, useRef, useState } from "react";

const FIELDS = [["title","Title"],["artist","Artist"],["album","Album"],["album_artist","Album artist"],["year","Year"],["genre","Genre"],["track","Track (n/total)"]];
const STEPS = ["Source", "Details", "Review", "Export"];
const BLANK = { artist: "", title: "", album: "", year: "", notes: "" };
const imgSrc = (u) => (u && u.startsWith("http") ? `/api/img?url=${encodeURIComponent(u)}` : u);
const rid = () => (Math.random().toString(36).slice(2) + "abcdefghij").slice(0, 10);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function uploadFile(file, id, onFrac) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest(), fd = new FormData();
    fd.append("file", file); fd.append("job_id", id);
    x.open("POST", "/api/upload");
    x.upload.onprogress = (e) => e.lengthComputable && onFrac(e.loaded / e.total);
    x.onload = () => {
      let d = {}; try { d = JSON.parse(x.responseText); } catch {}
      x.status < 300 ? resolve(d) : reject(new Error(d.detail || x.statusText));
    };
    x.onerror = () => reject(new Error("Network error during upload"));
    x.send(fd);
  });
}

// Eases toward real progress, then creeps slowly toward `ceil` so the bar never looks frozen.
function Progress({ bar, t0 }) {
  const [d, setD] = useState(0);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const i = setInterval(() => {
      setNow(Date.now());
      setD((v) => {
        if (bar.pct >= 100) return 100;
        if (v < bar.pct) return Math.min(bar.pct, v + (bar.pct - v) * 0.3 + 0.05);
        if (v < bar.ceil - 0.3) return v + Math.max(0.03, (bar.ceil - v) * 0.02);
        return v;
      });
    }, 100);
    return () => clearInterval(i);
  }, [bar]);
  return (
    <div className="prog" role="progressbar" aria-valuenow={Math.floor(d)} aria-valuemin={0} aria-valuemax={100}>
      <div className="ptop">
        <span>{bar.msg}</span>
        <em>{Math.floor(d)}% · {Math.floor((now - t0) / 1000)}s</em>
      </div>
      <div className="bar"><div className="fill" style={{ width: `${d}%` }} /></div>
    </div>
  );
}

export default function App() {
  const [mode, setMode] = useState("youtube");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState(null);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(null); // {phase, t0}
  const [bar, setBar] = useState({ pct: 0, ceil: 0, msg: "" });
  const [err, setErr] = useState("");
  const [ok, setOk] = useState("");
  const [job, setJob] = useState(null);
  const [hints, setHints] = useState(BLANK);
  const [confirmedHints, setConfirmedHints] = useState([]);
  const [res, setRes] = useState(null);
  const timer = useRef(null);
  useEffect(() => () => clearInterval(timer.current), []);

  const run = async (phase, id, msg, fn) => {
    setErr(""); setOk("");
    setBar({ pct: 0, ceil: 5, msg }); setBusy({ phase, t0: Date.now() });
    clearInterval(timer.current);
    timer.current = setInterval(async () => {
      try {
        const p = await (await fetch(`/api/progress/${id}`)).json();
        setBar((b) => ({ pct: Math.max(b.pct, p.pct || 0), ceil: Math.max(p.ceil || 0, b.pct), msg: p.msg || b.msg }));
      } catch {}
    }, 400);
    try {
      await fn();
      setBar((b) => ({ ...b, pct: 100, ceil: 100, msg: "Done" }));
      await sleep(400);
    } catch (e) { setErr(String(e.message || e)); }
    clearInterval(timer.current); setBusy(null);
  };

  const clearAll = () => { setJob(null); setRes(null); setHints(BLANK); setConfirmedHints([]); };
  const switchMode = (m) => { setMode(m); clearAll(); setErr(""); setOk(""); };

  const start = () => {
    clearAll();
    const id = rid();
    return run("source", id, mode === "youtube" ? "Contacting YouTube…" : "Uploading…", async () => {
      let d;
      if (mode === "youtube") d = await (await post("/api/youtube", { url, job_id: id })).json();
      else d = await uploadFile(file, id, (f) => setBar((b) => ({ pct: Math.max(b.pct, f * 88), ceil: 88, msg: `Uploading… ${Math.round(f * 100)}%` })));
      setJob({ id: d.id, context: d.context });
      setHints({ ...d.hints, notes: "" });
    });
  };

  const identify = () => {
    setRes(null);
    return run("identify", job.id, "Preparing search…", async () => {
      setRes(await (await post("/api/identify", { id: job.id, ...hints, confirmed_fields: confirmedHints })).json());
    });
  };

  const finalize = (quality) => run("export", job.id, quality === "high" ? "Preparing high-quality export…" : "Preparing export…", async () => {
    const r = await post("/api/finalize", { id: job.id, tags: res.tags, cover_url: res.cover_url, cover_thumb: res.cover_thumb, quality });
    const m = (r.headers.get("Content-Disposition") || "").match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)["']?/i);
    const name = m ? decodeURIComponent(m[1]) : "track.mp3";
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name; a.click();
    setOk(`Saved “${name}” · ${(blob.size / 1048576).toFixed(1)} MB`);
  });

  const pickFile = (f) => {
    if (!f) return;
    if (!/\.mp3$/i.test(f.name)) return setErr("Please choose an .mp3 file");
    setErr(""); setFile(f);
  };
  const setTag = (k, v) => setRes({ ...res, tags: { ...res.tags, [k]: v } });
  const setHint = (k, v) => {
    setHints({ ...hints, [k]: v });
    if (k !== "notes") setConfirmedHints((fields) => fields.includes(k) ? fields : [...fields, k]);
  };
  const at = !job ? 0 : !res ? 1 : ok ? 4 : 2;
  const b = !!busy;
  const bp = (phase) => busy?.phase === phase && <Progress bar={bar} t0={busy.t0} />;
  const conf = ((res?.note || "").match(/confidence:\s*(\w+)/i) || [])[1]?.toLowerCase();

  return (
    <div className="wrap">
      <header className="top">
        <h1><img className="logo" src="/logo_for_light_bg.png" alt="MP3 Tagger" /></h1>
        <p>Fetch audio, identify the track, edit tags and artwork, export.</p>
      </header>
      <ol className="stepper">{STEPS.map((s, i) => <li key={s} className={i < at ? "done" : i === at ? "on" : ""}><i>{i < at ? "✓" : i + 1}</i><span>{s}</span></li>)}</ol>
      <main className="panel">
      <section className="step">
        <h3><span className="num">1</span>Source</h3>
        <div className="seg">
          {[["youtube", "YouTube URL"], ["upload", "Upload MP3"]].map(([m, l]) => (
            <button key={m} className={mode === m ? "on" : ""} disabled={b} onClick={() => switchMode(m)}>{l}</button>
          ))}
        </div>
        {mode === "youtube"
          ? <input className="in" style={{ marginBottom: 12 }} placeholder="https://www.youtube.com/watch?v=…" value={url} disabled={b} onChange={(e) => setUrl(e.target.value)} />
          : <label className={`drop${drag ? " over" : ""}${b ? " off" : ""}`}
                   onDragOver={(e) => { e.preventDefault(); if (!b) setDrag(true); }} onDragLeave={() => setDrag(false)}
                   onDrop={(e) => { e.preventDefault(); setDrag(false); if (!b) pickFile(e.dataTransfer.files[0]); }}>
              <input type="file" accept=".mp3,audio/mpeg" disabled={b} onChange={(e) => pickFile(e.target.files[0])} />
              <span className="dz-title">{file ? file.name : "Choose an MP3 or drop it here"}</span>
              <span className="dz-sub">{file ? `${(file.size / 1048576).toFixed(1)} MB · click to change` : "MP3 files only"}</span>
            </label>}
        <button className="btn" onClick={start} disabled={b || (mode === "youtube" ? !url : !file)}>
          {mode === "youtube" ? "Download audio" : "Upload & read tags"}
        </button>
        {bp("source")}
        {err && !job && <div className="alert">{err}</div>}
      </section>

      {job && (
        <section className="step">
          <h3><span className="num">2</span>Details</h3>
          <p className="sub" style={{ marginTop: -6, fontSize: 13 }}>Source: {job.context}. Filled fields are treated as fact; blanks get researched.</p>
          <div className="grid2">
            {[["artist", "Artist"], ["title", "Title"], ["album", "Album (optional)"], ["year", "Year (optional)"]].map(([k, l]) => (
              <div key={k}><label>{l}</label><input className="in" value={hints[k]} disabled={b} onChange={(e) => setHint(k, e.target.value)} /></div>
            ))}
            <div className="span2"><label>Extra notes (film name, lyrics line, remix/live/cover, language…)</label>
              <textarea className="in" value={hints.notes} disabled={b} onChange={(e) => setHint("notes", e.target.value)} /></div>
          </div>
          <button className="btn" onClick={identify} disabled={b}>{res ? "Re-identify" : "Identify with AI"}</button>
          {bp("identify")}
          {err && !res && <div className="alert">{err}</div>}
        </section>
      )}

      {res && (
        <>
          <section className="step">
            <h3><span className="num">3</span>Review</h3>
            {res.note && <div className={`chip ${conf || ""}`}>{res.note}</div>}
            <div className="review">
              <div>
                {res.cover_url
                  ? <img className="cover" src={imgSrc(res.cover_url)} alt="cover" referrerPolicy="no-referrer" />
                  : <div className="cover">No cover found</div>}
              </div>
              <div className="grid2" style={{ marginBottom: 0 }}>
                {FIELDS.map(([k, l]) => (
                  <div key={k} className={k === "title" ? "span2" : ""}><label>{l}</label>
                    <input className="in" value={res.tags[k] || ""} disabled={b} onChange={(e) => setTag(k, e.target.value)} /></div>
                ))}
              </div>
            </div>
            {res.candidates.length > 1 && (
              <div className="cands">
                {res.candidates.map((c, i) => (
                  <img key={i} src={c.thumb || c.url} title={c.label} referrerPolicy="no-referrer" className={res.cover_url === c.url ? "sel" : ""}
                       onClick={() => setRes({ ...res, cover_url: c.url, cover_thumb: c.thumb })} />
                ))}
              </div>
            )}
            <label>Cover URL (paste your own to override)</label>
            <input className="in" value={res.cover_url} disabled={b} onChange={(e) => setRes({ ...res, cover_url: e.target.value, cover_thumb: "" })} />
          </section>

          <section className="step">
            <h3><span className="num">4</span>Export</h3>
            <div className="row">
              <button className="btn" disabled={b} onClick={() => finalize("normal")}>Download MP3</button>
              <button className="btn ghost" disabled={b} onClick={() => finalize("high")}>Download high quality (320 kbps)</button>
            </div>
            <p className="hint">High quality re-encodes from the original audio with loudness leveling and takes a few extra seconds.</p>
            {bp("export")}
            {ok && <div className="ok">{ok}</div>}
            {err && <div className="alert">{err}</div>}
          </section>
        </>
      )}
      </main>
    </div>
  );
}