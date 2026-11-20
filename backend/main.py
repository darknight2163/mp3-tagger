"""MP3 tagger service.

This module keeps the runtime behavior intact while organizing the code into
clear sections for configuration, metadata utilities, search/AI enrichment,
cover discovery, and API endpoints.
"""

import asyncio
import contextvars
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from mutagen.mp3 import MP3
from mutagen.id3 import APIC, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TRCK, ID3, ID3NoHeaderError
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

# -----------------------------------------------------------------------------
# Load environment configuration
# -----------------------------------------------------------------------------
load_dotenv(Path(__file__).parent / ".env")

USE_SHAZAM = os.getenv("USE_SHAZAM", "false").lower() in ("1", "true", "yes")
LLM_URL = os.getenv("LLM_URL", "https://text.pollinations.ai/openai")
LLM_MODEL = os.getenv("LLM_MODEL", "openai")
LLM_KEY = os.getenv("LLM_KEY", "")
YOUTUBE_COOKIES_FILE = os.getenv("YOUTUBE_COOKIES_FILE", "").strip()
YOUTUBE_COOKIES_FROM_BROWSER = os.getenv("YOUTUBE_COOKIES_FROM_BROWSER", "").strip()

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/120 Safari/537.36"
    )
}

# -----------------------------------------------------------------------------
# Application runtime state
# -----------------------------------------------------------------------------
WORK = Path(__file__).parent / "work"
WORK.mkdir(exist_ok=True)

app = FastAPI()
JOBS = {}  # job_id -> context (raw title, description, existing tags, ...)
FIELDS = ["title", "artist", "album", "album_artist", "year", "genre", "track"]

# ---------- progress tracking (polled by the UI at /api/progress/{job_id}) ----------
PROGRESS = {}
CUR_JOB = contextvars.ContextVar("cur_job", default=None)


def _job_id(raw=""):
    return raw if re.fullmatch(r"[A-Za-z0-9]{6,32}", raw or "") else uuid.uuid4().hex[:10]


def prog_start(job_id, msg):
    CUR_JOB.set(job_id)
    PROGRESS[job_id] = {"pct": 0, "ceil": 0, "msg": msg, "t0": time.time(), "t1": None, "done": False, "error": ""}


def prog(pct=None, msg=None, ceil=None, job_id=None):
    """pct = real progress floor; ceil = how far the UI may creep while waiting on a slow step."""
    p = PROGRESS.get(job_id or CUR_JOB.get())
    if not p:
        return
    if pct is not None:
        p["pct"] = max(p["pct"], min(pct, 99))
        p["ceil"] = min(ceil, 99) if ceil is not None else p["pct"]
    p["ceil"] = max(p["ceil"], p["pct"])
    if msg:
        p["msg"] = msg


def prog_end(msg="Done", error="", job_id=None):
    p = PROGRESS.get(job_id or CUR_JOB.get())
    if p:
        if not error:
            p["pct"] = p["ceil"] = 100
        p.update(msg=error or msg, done=True, error=error, t1=time.time())


@app.get("/api/progress/{job_id}")
def get_progress(job_id: str):
    p = PROGRESS.get(job_id)
    if not p:
        return {"pct": 0, "ceil": 0, "msg": "", "done": False, "error": "", "elapsed": 0}
    return {**p, "elapsed": round((p["t1"] or time.time()) - p["t0"], 1)}


MAX_DDGS_IMAGES_PER_QUERY = int(os.getenv("MAX_DDGS_IMAGES_PER_QUERY", 6))
FINAL_IMAGE_SNIPPET_CAP = int(os.getenv("FINAL_IMAGE_SNIPPET_CAP", 5))
MAX_DDGS_TEXT_PER_QUERY = int(os.getenv("MAX_DDGS_TEXT_PER_QUERY", 3))
FINAL_TEXT_SNIPPET_CAP = int(os.getenv("FINAL_TEXT_SNIPPET_CAP", 10))


def cleanup_generated_artifacts():
    """Remove stale output files from prior runs without affecting active jobs."""
    JOBS.clear()
    now = time.time()
    for p in WORK.iterdir():
        if not p.is_file():
            continue
        if p.name.endswith((".art", ".clip.mp3", ".webm", ".m4a", ".part", ".mp3")):
            try:
                print("[cleanup] removing stale file:", p)
                p.unlink()
            except FileNotFoundError:
                pass
        elif p.stat().st_mtime < now - 86400:
            try:
                print("[cleanup] removing stale file:", p)
                p.unlink()
            except FileNotFoundError:
                pass


cleanup_generated_artifacts()


# -----------------------------------------------------------------------------
# File helpers
# -----------------------------------------------------------------------------
def mp3(job_id):
    return WORK / f"{job_id}.mp3"


def art(job_id):
    return WORK / f"{job_id}.art"


def _clip_text(value, limit=200):
    if value is None:
        return ""
    s = str(value).strip()
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "..."


def clean_title(s):
    """Remove common YouTube marketing noise from a track title."""
    s = re.sub(r"[\(\[][^\)\]]*(official|lyric|video|audio|visualizer|hd|4k|remaster|mv)[^\)\]]*[\)\]]",
               "", s or "", flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" -|")


def _safe_int(value, default=0):
    """Extract the first integer from a value while keeping a safe fallback."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            return int(match.group(0))
    return default


def parse_yt(info):
    """Parse YouTube metadata into the core artist/title hints used by the app."""
    artist = info.get("artist") or ""
    title = info.get("track") or ""
    if not (artist and title):
        t = clean_title(info.get("title", ""))
        if " - " in t:
            a, b = t.split(" - ", 1)
            artist, title = artist or a.strip(), title or b.strip()
        else:
            title = title or t
            up = re.sub(r"(\s*-\s*Topic|VEVO|\s*Official)$", "", info.get("uploader") or "", flags=re.I)
            artist = artist or up.strip()
    return {"artist": artist.split(",")[0].strip(), "title": title}


def read_tags(path):
    """Read existing MP3 tags into the app's normalized metadata dictionary."""
    out = {k: "" for k in FIELDS}
    try:
        t = ID3(path)
    except ID3NoHeaderError:
        return out, False
    g = lambda k: str(t[k].text[0]) if k in t and t[k].text else ""
    out.update(title=g("TIT2"), artist=g("TPE1"), album=g("TALB"), album_artist=g("TPE2"),
               year=g("TDRC")[:4], genre=g("TCON"), track=g("TRCK"))
    return out, bool(t.getall("APIC"))


def extract_art(path, job_id):
    """Persist the first embedded artwork for the job and return the API path."""
    pics = ID3(path).getall("APIC")
    if pics:
        art(job_id).write_bytes(pics[0].data)
        return f"/api/art/{job_id}"
    return ""


async def fetch_img(url):
    """Fetch a remote image while tolerating common content-type mismatches."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers=UA) as c:
            r = await c.get(url)
        ct = (r.headers.get("content-type") or "").split(";")[0].lower()
        if r.status_code != 200:
            return None

        if ct.startswith("image/"):
            return r.content, ct

        # Some image hosts serve generic or missing content types despite returning an actual image.
        # Accept common image signatures before rejecting the URL.
        body = r.content[:16]
        if body.startswith(b"\x89PNG"):
            return r.content, "image/png"
        if body.startswith(b"\xff\xd8\xff"):
            return r.content, "image/jpeg"
        if body.startswith((b"GIF87a", b"GIF89a")):
            return r.content, "image/gif"
        if body.startswith(b"RIFF") and b"WEBP" in body[:12]:
            return r.content, "image/webp"
        if body.startswith(b"\x00\x00\x00\x0cftyp"):
            return r.content, "image/avif"

        # Fallback: if the URL itself clearly points to an image file, keep it even without a content-type.
        path = urlparse(url).path.lower()
        if any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")):
            return r.content, "image/octet-stream"
    except Exception as e:
        print("img fetch failed:", url, e)
    return None


# ---------- optional Shazam (USE_SHAZAM=true; pip install shazamio) ----------
async def shazam(path):
    from shazamio import Shazam
    s = Shazam()
    clip = path.with_suffix(".clip.mp3")
    try:
        for ss in ("30", "75", "0"):
            clip.unlink(missing_ok=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", ss, "-t", "15", "-i", str(path),
                            "-vn", "-ac", "1", "-ar", "44100", str(clip)])
            if not (clip.exists() and clip.stat().st_size > 2000):
                continue
            try:
                res = await (s.recognize(str(clip)) if hasattr(s, "recognize") else s.recognize_song(str(clip)))
            except Exception as e:
                print("shazam failed:", e)
                continue
            tr = res.get("track")
            if tr:
                meta = {}
                for sec in tr.get("sections", []):
                    for m in sec.get("metadata", []) or []:
                        meta[m.get("title")] = m.get("text")
                return {"title": tr.get("title", ""), "artist": tr.get("subtitle", ""),
                        "album": meta.get("Album", ""), "year": (meta.get("Released") or "")[-4:],
                        "genre": (tr.get("genres") or {}).get("primary", "")}
        return {}
    except Exception as e:
        print("shazam error:", e)
        return {}
    finally:
        clip.unlink(missing_ok=True)


# ---------- search + LLM ----------
def _ddg_text(q):
    try:
        from ddgs import DDGS
        return DDGS().text(q, max_results=MAX_DDGS_TEXT_PER_QUERY)
    except Exception as e:
        print("search failed:", q, e)
        return []


def _ddg_images(q):
    try:
        from ddgs import DDGS
        return DDGS().images(q, max_results=MAX_DDGS_IMAGES_PER_QUERY)
    except Exception as e:
        print("image search failed:", q, e)
        return []


async def llm_json(system, user):
    try:
        headers = {"Authorization": f"Bearer {LLM_KEY}"} if LLM_KEY else {}
        body = {"model": LLM_MODEL, "temperature": 0, "max_tokens": 1500,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if "groq.com" in LLM_URL:
            body["response_format"] = {"type": "json_object"}   # your list shows json_mode support
            if "gpt-oss" in LLM_MODEL:
                body["reasoning_effort"] = "low"                # less thinking, faster, fewer tokens
        
        async with httpx.AsyncClient(timeout=70) as c:
            r = await c.post(LLM_URL, headers=headers, json=body)
            if r.status_code in (402, 429):
                m = re.search(r"[Rr]etry after (\d+)", r.text)
                wait = int(m.group(1)) + 1 if m else 5
                if wait <= 50:
                    print(f"[llm] rate limited, waiting {wait}s then retrying once")
                    for left in range(wait, 0, -1):
                        prog(msg=f"AI provider is rate-limiting free usage · retrying in {left}s")
                        await asyncio.sleep(1)
                    r = await c.post(LLM_URL, headers=headers, json=body)
        try:
            payload = r.json()
        except ValueError:
            print(f"[llm] non-json response status={r.status_code} body={r.text[:800]}")
            return {}
        if r.status_code >= 400:
            print(f"[llm] http error status={r.status_code} body={json.dumps(payload)[:800]}")
            return {}
        if not payload.get("choices"):
            print(f"[llm] no choices in response status={r.status_code} payload={payload}")
            return {}
        text = payload["choices"][0]["message"]["content"]
        print(f"[llm] raw response: {text[:600]}")
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            print(f"[llm] no JSON object found in text: {text[:800]}")
            return {}
        return json.loads(match.group(0))
    except Exception as e:
        print("llm failed:", e)
        return {}


SYSTEM = """You are a meticulous music metadata researcher. Identify exactly ONE song and return accurate, verifiable tags for the specific recording the user has.

RULES
1. Ground every field in the evidence provided (user fields, YouTube/file metadata, web search results). Never invent. If a field is not supported by evidence, return "".
2. USER-CONFIRMED fields are authoritative. Do not change them; use them to disambiguate and fill in the rest.
3. Title: strip marketing noise such as (Official Video), [Lyrics], HD, 4K, "Full Song". Keep qualifiers that define the recording: (feat. X), Remix, Live, Acoustic, Instrumental, Sped Up.
4. Artist: the main performing artist(s), comma-separated. album_artist: the album's primary artist ("Various Artists" only for a genuine compilation).
5. Album: the ORIGINAL album/EP/single this recording was first released on. Avoid "Greatest Hits", "Best of", playlists and later compilations unless the track exists nowhere else. If it is a standalone single, use the single's title. For film songs (Bollywood, Tollywood, K-drama, etc.) the album is the film/series title.
6. Year: 4-digit year of ORIGINAL release of this recording, not a remaster, reissue or compilation year.
7. Genre: one concise standard genre (e.g. Pop, Hip-Hop/Rap, Rock, R&B, Electronic, Bollywood, Classical, Indie).
8. track: "n/total" only if a source states it, else "".
9. Source quality: strong = YouTube auto-generated descriptions ("Provided to YouTube by ... Artist · Title ... Album ... ℗ year"), Wikipedia, Discogs, MusicBrainz, Genius, AllMusic, Apple Music, Spotify, Bandcamp, official artist/label pages. Weak = fan uploads, lyric-spam sites, SEO pages, comments.
10. Songs can be in any language. Use the commonly published Latin transliteration for titles/artists unless the user wrote them otherwise.
11. Covers, remixes, live and fan versions: identify the version the evidence points to, not automatically the famous original.
12. If sources conflict, prefer the one agreed by most strong sources and mention the conflict in "evidence".

confidence: "high" = two or more independent strong sources agree (or a YouTube auto-generated block); "medium" = a single decent source; "low" = weak or inferred.

OUTPUT: ONLY one JSON object, no prose, no markdown:
{"artist":"","title":"","album":"","album_artist":"","year":"","genre":"","track":"","confidence":"high|medium|low","evidence":"one short sentence naming the sources used"}"""


def _queries(hints, notes, raw):
    a, t = hints.get("artist", ""), hints.get("title", "")
    if a and t:
        qs = [f'{a} "{t}" song', f"{a} {t} album release year", f"{a} {t} discogs wikipedia genius"]
    else:
        base = " ".join(x for x in (a, t) if x) or raw
        qs = [f"{base} song", f"{base} song album release year"]
    if notes:
        qs.append(f"{notes} {a} {t} song".strip())
        if len(notes.split()) >= 5:
            qs.append(f'"{notes}" lyrics')
    return qs


async def gather_snippets(qs):
    done = 0

    async def one(q):
        nonlocal done
        r = await run_in_threadpool(_ddg_text, q)
        done += 1
        prog(8 + 27 * done / len(qs), f"Searching the web… ({done}/{len(qs)} queries)", ceil=36)
        return r

    lists = await asyncio.gather(*[one(q) for q in qs])
    seen, out = set(), []
    for lst in lists:
        for r in lst:
            href = r.get("href", "")
            if href and href not in seen:
                seen.add(href)
                out.append(f"[{len(out)+1}] {urlparse(href).netloc} | {_clip_text(r.get('title',''), 80)} | {_clip_text(r.get('body','')[:300], 180)}")
    return out[:FINAL_TEXT_SNIPPET_CAP]


async def llm_resolve(job, hints, confirmed_fields, notes, fp):
    queries = _queries(hints, notes, job.get("raw", ""))
    print(f"[identify] search queries: {queries}")
    snippets = await gather_snippets(queries)
    parts = []
    confirmed = [f"- {k}: {_clip_text(hints.get(k, ''), 80)}" for k in confirmed_fields if hints.get(k)]
    parts.append("USER-CONFIRMED FIELDS (authoritative):\n" + ("\n".join(confirmed) or "- none"))
    source_hints = [f"- {k}: {_clip_text(v, 80)}" for k, v in hints.items() if v and k not in confirmed_fields]
    if source_hints:
        parts.append("AUTO-FILLED DETAILS (may be inaccurate; use only as clues):\n" + "\n".join(source_hints))
    if notes:
        parts.append(f"USER NOTES (extra context, may include lyrics, film name, year, etc.):\n{_clip_text(notes, 500)}")
    raw = _clip_text(job.get("raw", ""), 200)
    if raw:
        parts.append(f"SOURCE TITLE / FILENAME:\n{raw}")
    description = _clip_text(job.get("description", ""), 500)
    if description:
        parts.append(f"YOUTUBE DESCRIPTION (first 500 chars):\n{description}")
    ex = [f"- {k}: {_clip_text(v, 80)}" for k, v in job.get("existing", {}).items() if v]
    if ex:
        parts.append("EXISTING FILE TAGS:\n" + "\n".join(ex[:8]))
    if fp:
        parts.append("AUDIO FINGERPRINT MATCH (Shazam):\n" + _clip_text(json.dumps(fp, ensure_ascii=False), 500))
    parts.append("WEB SEARCH RESULTS:\n" + ("\n".join(snippets) or "(none found)"))
    parts.append("Return the JSON now.")
    print(f"[identify] snippet count={len(snippets)}; first snippets={snippets[:2]}")
    prog(38, "Asking the AI to analyse the results…", ceil=62)
    res = await llm_json(SYSTEM, "\n\n".join(parts))
    print(f"[identify] llm result={res}")
    prog(64, "Metadata ready")
    return {k: str(v).strip() for k, v in res.items() if v is not None}


def _cover_search_queries(tags):
    artist = (tags.get("artist") or "").strip()
    album = (tags.get("album") or "").strip()
    title = (tags.get("title") or "").strip()

    queries = []
    if artist and title:
        queries.extend([
            f'{artist} "{title}" official cover art',
            f'{artist} "{title}" single cover art',
            f'{artist} "{title}" track cover art',
            f'{artist} "{title}" poster art',
        ])
        if album:
            queries.extend([
                f'{artist} {album} album cover art',
                f'{artist} "{title}" {album} cover art',
            ])
        queries.extend([
            f'{artist} {title} cover art',
            f'{artist} {title} music artwork',
        ])
    elif artist:
        queries.extend([
            f'{artist} song cover art',
            f'{artist} album cover art',
        ])
    elif title:
        queries.extend([
            f'"{title}" cover art',
            f'"{title}" album art',
        ])

    unique = []
    seen = set()
    for q in queries:
        q = " ".join(q.split())
        if q and q not in seen:
            seen.add(q)
            unique.append(q)
    return unique[:FINAL_IMAGE_SNIPPET_CAP]


async def find_covers(tags):
    artist, album, title = tags.get("artist", ""), tags.get("album", ""), tags.get("title", "")
    queries = _cover_search_queries(tags)
    print(f"[cover] search queries={queries}")
    done = 0

    async def one(q):
        nonlocal done
        r = await run_in_threadpool(_ddg_images, q)
        done += 1
        prog(66 + 20 * done / len(queries), f"Searching cover art… ({done}/{len(queries)})", ceil=87)
        return r

    batches = await asyncio.gather(*(one(q) for q in queries))
    cands, seen = [], set()
    for q_index, res in enumerate(batches):
        q_rank = len(queries) - q_index
        query_text = queries[q_index].lower()
        is_song_query = bool(title and title.lower() in query_text)
        is_album_query = bool(album and album.lower() in query_text)
        for idx, r in enumerate(res or []):
            url = (r.get("image") or "").strip()
            raw_w = r.get("width")
            raw_h = r.get("height")
            w = _safe_int(raw_w)
            h = _safe_int(raw_h)
            if idx < 3:
                print(f"[cover] query[{q_index}] item {idx}: url={url[:120]} width={raw_w!r} height={raw_h!r} parsed=({w}, {h})")
            if not url or url in seen:
                continue
            if w < 400 or h < 400:
                print(f"[cover] skipped small image: {url} ({w}x{h})")
                continue
            if h == 0 or w / h < 0.9 or w / h > 1.1:
                print(f"[cover] skipped bad aspect ratio: {url} ({w}x{h})")
                continue
            title_l = (r.get("title") or "").lower()
            artist_l = artist.lower()
            album_l = album.lower()
            score = q_rank * 10
            if title and title.lower() in title_l:
                score += 40
            if artist_l and artist_l in title_l:
                score += 20
            if album_l and album_l in title_l:
                score += 10
            if is_song_query:
                score += 25
            if is_album_query:
                score -= 10
            if "song" in (query_text + " " + title_l):
                score += 15
            seen.add(url)
            cands.append({"url": url, "thumb": r.get("thumbnail") or url,
                          "label": f"{(r.get('title') or '')[:80]} | {urlparse(url).netloc} | {w}x{h}",
                          "_score": score, "_kind": "song" if is_song_query else "album"})
    cands.sort(key=lambda c: (c["_kind"] != "song", -c["_score"]), reverse=False)
    cands = [{k: v for k, v in c.items() if k not in ("_score", "_kind")} for c in cands[:FINAL_IMAGE_SNIPPET_CAP]]
    print(f"[cover] accepted candidates before LLM={len(cands)}")
    if len(cands) > 1:
        first_kind = "song" if any("song" in (queries[i].lower() + " " + (tags.get("title") or "").lower()) for i in range(len(queries))) else "album"
        if first_kind == "song":
            print("[cover] track-specific cover detected; preferring it over album art")
            # Keep the best song-specific candidate at the top and skip a lower-quality album override.
            cands = cands[:1] + [c for c in cands[1:] if c]
        else:
            prog(88, "Choosing the best cover…", ceil=97)
            lines = "\n".join(f"{i}: {c['label']}" for i, c in enumerate(cands))
            pick = await llm_json(
                "You choose album artwork. Reply ONLY JSON: {\"index\": <int>}.",
                f"Pick the image most likely to be the official cover for the specific track \"{artist} - {title}\". "
                f"If a track-specific cover is available, prefer it over album art. If no track cover exists, use the official album cover. "
                f"Avoid fan art, merch, vinyl/CD photos, screenshots, concert photos, other artists, and wrong albums. "
                f"Prefer official or reputable sources (Wikipedia, Apple Music, Spotify, Discogs, label sites).\n{lines}")
            try:
                # i = int(pick.get("index", 0))
                i = int(pick["index"])
                if 0 <= i < len(cands):
                    cands.insert(0, cands.pop(i))
                    print(f"[cover] LLM picked index={i}; final ordering={[c['url'] for c in cands]}")
            except Exception as e:
                print(f"[cover] LLM pick failed: {e}")
                pass
    print(f"[cover] final candidates={[c['url'] for c in cands]}")
    return cands


# ---------- optional high-quality export ----------
def source_file(job_id):
    """Return the original source stream for a job when available."""
    for f in WORK.glob(f"{job_id}.*"):
        if f.suffix.lower() not in (".mp3", ".art", ".part", ".ytdl"):
            return f
    return None


def make_hq(job_id):
    """Fast 320 kbps MP3 export without expensive loudness processing."""
    src = source_file(job_id) or mp3(job_id)
    out = WORK / f"{job_id}.hq.mp3"
    try:
        dur = MP3(mp3(job_id)).info.length
    except Exception:
        dur = 0

    # Keep the encode fast: no loudnorm pass, no second lossy pass when the source is already mp3.
    if src.suffix.lower() == ".mp3" and src != out:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
               "-i", str(src), "-vn", "-map_metadata", "-1",
               "-c:a", "libmp3lame", "-b:a", "320k", "-ar", "44100", str(out)]
    else:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
               "-i", str(src), "-vn", "-map_metadata", "-1",
               "-c:a", "libmp3lame", "-b:a", "320k", "-ar", "44100", str(out)]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    known = re.compile(r"^(frame|fps|stream_\S+|bitrate|total_size|out_time\w*|dup_frames|drop_frames|speed|progress)=")
    err = []
    for line in proc.stdout:
        line = line.strip()
        if line.startswith(("out_time_us=", "out_time_ms=")) and dur:
            try:
                t = int(line.split("=")[1]) / 1e6
            except ValueError:
                continue
            f = min(t / dur, 1.0)
            prog(6 + 60 * f, f"Encoding 320 kbps… {int(f * 100)}%", ceil=66, job_id=job_id)
        elif line and not known.match(line):
            err.append(line)
    proc.wait()
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(("\n".join(err) or "ffmpeg failed")[-300:])
    return out


# ---------- endpoints ----------
class YT(BaseModel):
    url: str
    job_id: str = ""


def _download(url, job_id):
    def on_dl(d):
        if d["status"] == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            got = d.get("downloaded_bytes") or 0
            if tot:
                spd, eta = d.get("speed"), d.get("eta")
                extra = (f" · {spd / 1e6:.1f} MB/s" if spd else "") + (f" · {int(eta)}s left" if eta is not None else "")
                prog(6 + 66 * got / tot, f"Downloading audio… {int(100 * got / tot)}%{extra}", ceil=72, job_id=job_id)
        elif d["status"] == "finished":
            prog(72, "Download complete", job_id=job_id)

    def on_pp(d):
        if d.get("postprocessor") == "ExtractAudio":
            if d["status"] == "started":
                prog(76, "Converting to MP3…", ceil=93, job_id=job_id)
            elif d["status"] == "finished":
                prog(94, "Conversion finished", job_id=job_id)

    prog(2, "Fetching video info…", ceil=6, job_id=job_id)
    opts = {"format": "bestaudio/best", "noplaylist": True, "quiet": True,
            "outtmpl": str(WORK / f"{job_id}.%(ext)s"),
            "progress_hooks": [on_dl], "postprocessor_hooks": [on_pp],
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}
    if YOUTUBE_COOKIES_FILE and YOUTUBE_COOKIES_FROM_BROWSER:
        raise ValueError("Set only one of YOUTUBE_COOKIES_FILE or YOUTUBE_COOKIES_FROM_BROWSER.")
    if YOUTUBE_COOKIES_FILE:
        cookie_file = Path(YOUTUBE_COOKIES_FILE).expanduser()
        if not cookie_file.is_absolute():
            cookie_file = Path(__file__).parent / cookie_file
        opts["cookiefile"] = str(cookie_file)
    elif YOUTUBE_COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (YOUTUBE_COOKIES_FROM_BROWSER, None, None, None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


@app.post("/api/youtube")
async def youtube(body: YT):
    job_id = _job_id(body.job_id)
    prog_start(job_id, "Starting…")
    try:
        info = await run_in_threadpool(_download, body.url, job_id)
    except Exception as e:
        prog_end(error="Download failed", job_id=job_id)
        message = str(e)
        if "sign in to confirm" in message.lower():
            message = (
                "YouTube requires authentication for this video. Set "
                "YOUTUBE_COOKIES_FROM_BROWSER or YOUTUBE_COOKIES_FILE in backend/.env "
                "and restart the backend. "
                f"yt-dlp error: {message}"
            )
        raise HTTPException(400, f"Download failed: {message}")
    prog(96, "Reading track details…")
    h = parse_yt(info)
    h["album"] = info.get("album") or ""
    h["year"] = str(info.get("release_year") or "")
    raw = f"{info.get('title','')} | {info.get('uploader','')}"
    JOBS[job_id] = {"raw": raw, "description": (info.get("description") or "")[:800],
                    "existing": {}, "local_cover": ""}
    prog_end("Audio ready")
    return {"id": job_id, "hints": h, "context": raw}


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), job_id: str = Form("")):
    job_id = _job_id(job_id)
    prog_start(job_id, "Saving file…")
    with open(mp3(job_id), "wb") as f:
        shutil.copyfileobj(file.file, f)
    prog(40, "Reading existing tags…")
    existing, has_art = read_tags(mp3(job_id))
    prog(70, "Extracting embedded cover…")
    local = extract_art(mp3(job_id), job_id) if has_art else ""
    stem = clean_title(Path(file.filename).stem)
    JOBS[job_id] = {"raw": file.filename, "description": "", "existing": existing, "local_cover": local}
    h = {"artist": existing["artist"], "title": existing["title"] or stem,
         "album": existing["album"], "year": existing["year"]}
    prog_end("Tags read")
    return {"id": job_id, "hints": h, "context": file.filename}


class Ident(BaseModel):
    id: str
    artist: str = ""
    title: str = ""
    album: str = ""
    year: str = ""
    notes: str = ""
    confirmed_fields: list[str] = Field(default_factory=list)


@app.post("/api/identify")
async def identify(b: Ident):
    job = JOBS.get(b.id)
    if not job:
        raise HTTPException(404, "Unknown job")
    hints = {k: getattr(b, k).strip() for k in ("artist", "title", "album", "year")}
    print(f"[identify] job_id={b.id} hints={hints} notes={b.notes.strip()!r} use_shazam={USE_SHAZAM}")
    prog_start(b.id, "Preparing search…")
    prog(3, "Preparing search…", ceil=8)
    if USE_SHAZAM:
        prog(4, "Fingerprinting audio…", ceil=8)
    fp = await shazam(mp3(b.id)) if USE_SHAZAM else {}
    print(f"[identify] shazam result={fp}")
    confirmed_fields = {
        k for k in b.confirmed_fields
        if k in ("artist", "title", "album", "year") and hints.get(k)
    }
    lm = await llm_resolve(job, hints, confirmed_fields, b.notes.strip(), fp)
    tags = {
        k: (
            (hints.get(k) if k in confirmed_fields else "")
            or job["existing"].get(k)
            or lm.get(k)
            or fp.get(k)
            or ""
        )
        for k in FIELDS
    }
    print(f"[identify] final tags before cover search={tags}")
    prog(65, "Looking for cover art…", ceil=66)
    cands = await find_covers(tags)
    if job["local_cover"]:
        cands.insert(0, {"url": job["local_cover"], "thumb": job["local_cover"], "label": "Existing embedded cover"})
    sel = cands[0] if cands else {"url": "", "thumb": ""}
    print(f"[identify] cover candidates={len(cands)} selected={sel.get('url')}")
    if not lm:
        note = "LLM/search failed (check backend logs); showing only your hints. Edit manually."
    else:
        note = f"Confidence: {lm.get('confidence','?')}. {lm.get('evidence','')}".strip()
        if lm.get("confidence") == "low":
            note += " Please verify carefully or add more notes and re-run."
    prog_end("Done")
    return {"tags": tags, "cover_url": sel["url"], "cover_thumb": sel["thumb"],
            "candidates": cands, "note": note}


@app.get("/api/art/{job_id}")
def get_art(job_id: str):
    p = art(job_id)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")


@app.get("/api/img")
async def img_proxy(url: str):
    if not url.startswith("http"):
        raise HTTPException(400)
    got = await fetch_img(url)
    if not got:
        raise HTTPException(404, "Could not fetch image")
    return Response(content=got[0], media_type=got[1])


class Final(BaseModel):
    id: str
    tags: dict
    cover_url: str = ""
    cover_thumb: str = ""
    quality: str = "normal"  # "normal" or "high"


@app.post("/api/finalize")
async def finalize(body: Final):
    p = mp3(body.id)
    if not p.exists():
        raise HTTPException(404, "Unknown job")
    high = body.quality == "high"
    prog_start(body.id, "Preparing export…")
    prog(3, "Preparing export…", ceil=6)
    if high:
        try:
            p = await run_in_threadpool(make_hq, body.id)
        except Exception as e:
            prog_end(error="High-quality encode failed", job_id=body.id)
            raise HTTPException(500, f"High-quality encode failed: {e}")
    c0 = 68 if high else 10
    prog(c0, "Downloading cover art…", ceil=c0 + 14)
    img = None
    if body.cover_url.startswith("/api/art/"):
        if art(body.id).exists():
            img = (art(body.id).read_bytes(), "image/jpeg")
    else:
        for u in (body.cover_url, body.cover_thumb):
            if u.startswith("http"):
                img = await fetch_img(u)
                if img:
                    break
        if not img and body.cover_url.startswith("http"):
            prog_end(error="Cover download failed", job_id=body.id)
            raise HTTPException(400, "Could not download that cover image. Pick another candidate.")
    prog(88, "Writing tags and artwork…", ceil=97)
    try:
        t = ID3(p)
    except ID3NoHeaderError:
        t = ID3()
    t.delete()
    g = body.tags
    t.add(TIT2(encoding=3, text=g.get("title", "")))
    t.add(TPE1(encoding=3, text=g.get("artist", "")))
    if g.get("album"): t.add(TALB(encoding=3, text=g["album"]))
    if g.get("album_artist"): t.add(TPE2(encoding=3, text=g["album_artist"]))
    if g.get("year"): t.add(TDRC(encoding=3, text=g["year"]))
    if g.get("genre"): t.add(TCON(encoding=3, text=g["genre"]))
    if g.get("track"): t.add(TRCK(encoding=3, text=g["track"]))
    if img:
        t.add(APIC(encoding=3, mime=img[1], type=3, desc="Cover", data=img[0]))
    t.save(p, v2_version=3)
    title = (g.get("title") or "track").strip()
    safe_title = re.sub(r'[\\/:*?"<>|]', "", title).strip() or "track"
    prog_end("Ready")
    return FileResponse(p, media_type="audio/mpeg", filename=f"{safe_title}.mp3")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, log_level="info")