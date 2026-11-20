<p align="center">
  <img src="frontend/public/logo_for_light_bg.png" alt="MP3 Tagger" width="240" />
</p>

<p align="center">
  <b>Fetch audio, identify the track, fix the tags and artwork, export a clean MP3.</b><br/>
  A local-first proof of concept built with FastAPI, React and an LLM + web-search pipeline.
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white" />
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white" />
  <img alt="React" src="https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black" />
  <img alt="Status" src="https://img.shields.io/badge/status-proof%20of%20concept-lightgrey" />
  <img alt="License" src="https://img.shields.io/badge/license-MIT-green" />
</p>

---

## Demo

<!-- Drag your end-to-end screen recording (.mp4) into the GitHub editor, then paste the generated URL on its own line below. GitHub renders it as an inline player. -->

> 🎬 **Demo video:** <video width="100%" controls preload="metadata">
  <source src="docs/walkthrough/demo.mp4" type="video/mp4">
  Your browser does not support HTML5 video.
</video>

## Features

- **Two complete workflows:** paste a YouTube URL, or upload an existing MP3 and repair its tags.
- **AI-assisted identification:** targeted web searches feed an LLM with an evidence-based prompt. It returns tags plus a **confidence level** and the sources it relied on.
- **You stay in control:** confirm what you know first (artist, title, film, lyrics hint), then review and edit every field before export.
- **Cover art search:** image search finds candidates, an LLM picks the most likely official cover, and you can swap it or paste your own URL.
- **Live progress:** real percentages for download, upload and encoding, stage-based progress for search and AI steps, with elapsed time.
- **Two export modes:** standard MP3 (192 kbps) or high quality (320 kbps with loudness normalization via ffmpeg).
- **Clean ID3v2.3 tags** and embedded artwork written with Mutagen.

## How it works

```mermaid
flowchart LR
  A["YouTube URL or MP3 upload"] --> B["Download audio / read existing tags"]
  B --> C["Confirm details and notes"]
  C --> D["Web search + LLM metadata"]
  D --> E["Cover search + LLM pick"]
  E --> F["Review and edit tags and artwork"]
  F --> G["Embed ID3 tags and cover"]
  G --> H["Download MP3 (standard or 320 kbps)"]
```

## Tech stack

| Layer | Tools |
|---|---|
| Backend | FastAPI, yt-dlp, Mutagen, ffmpeg, httpx |
| Search | `ddgs` (DuckDuckGo text and image search, no API key) |
| AI | Any OpenAI-compatible chat endpoint (Groq or Pollinations tested) |
| Frontend | React + Vite, plain CSS (no UI libraries) |

## Quick start

**Prerequisites:** Python 3.10+, Node 18+, and [ffmpeg](https://ffmpeg.org/) on your `PATH`
(`brew install ffmpeg`, `sudo apt install ffmpeg`, or `winget install ffmpeg`).

```bash
# 1. Backend
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then pick an LLM option below
uvicorn main:app --reload --port 8000

# 2. Frontend (new terminal)
cd frontend
npm install
npm run dev                        # open http://localhost:5173
```

### Choose an LLM (edit `backend/.env`)

<details open>
<summary><b>Option A: Groq (free API key, recommended)</b></summary>

Create a key at [console.groq.com](https://console.groq.com), then:

```env
LLM_URL=https://api.groq.com/openai/v1/chat/completions
LLM_MODEL=openai/gpt-oss-20b
LLM_KEY=your_groq_key
```

Model availability changes. If you get a "model not found" error, check Groq's model list and update `LLM_MODEL`.
</details>

<details>
<summary><b>Option B: Pollinations (no key)</b></summary>

```env
LLM_URL=https://text.pollinations.ai/openai
LLM_MODEL=openai
LLM_KEY=
```

Anonymous use is rate-limited (about one request per 45 seconds per IP), so identification can take around a minute.
</details>

| Variable | Default | Purpose |
|---|---|---|
| `LLM_URL` / `LLM_MODEL` / `LLM_KEY` | Pollinations | Chat-completions endpoint, model and key |
| `USE_SHAZAM` | `false` | Optional audio fingerprinting (`pip install shazamio` first) |
| `YOUTUBE_COOKIES_FROM_BROWSER` | _(unset)_ | Optional browser name for yt-dlp to read YouTube cookies from (for example, `chrome` or `firefox`) |
| `YOUTUBE_COOKIES_FILE` | _(unset)_ | Optional path to a private Netscape-format cookies file; relative paths are from `backend/` |

If YouTube asks you to sign in, set **one** of the cookie options in `backend/.env`, then restart the backend. For example, use `YOUTUBE_COOKIES_FROM_BROWSER=chrome` with Chrome signed in to YouTube, or set `YOUTUBE_COOKIES_FILE` to a cookies file exported as described in the [yt-dlp cookie guide](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp). Treat exported cookies like a password: keep the file private and never commit or share it. Browser cookie access can fail while the browser is running; if so, close it or use a cookie file.

## Usage

1. **Source:** Paste a YouTube URL or upload/drop an MP3.  
   > **Recommendation:** For the best identification results, use the song's original audio link rather than its music video link.  
   > Example: [Song audio](https://youtu.be/mGgcVdCeow8?si=opBtXd9b3kcLKUsa) vs. [Music video](https://youtu.be/AUQ_tgQZ_Os?si=LvHQqM042AHe6jkC).
2. **Details:** confirm artist and title, add hints (film name, lyric line, remix or live), then **Identify**.
3. **Review:** check the confidence note, edit tags, pick a cover.
4. **Export:** download the standard MP3 or the high-quality version.

## Project structure

```
backend/    FastAPI app (main.py), requirements.txt, .env.example
frontend/   React + Vite app (src/, public/ brand and icon assets)
docs/       Screenshots and demo assets
```

## Limitations

- **Proof of concept:** single user, no authentication, no rate limiting. Run it locally and do not expose it publicly.
- **AI results are suggestions:** always check the tags and cover before exporting. Confidence is the model's own estimate.
- **High quality is not magic:** it re-encodes from the original stream and levels loudness, but cannot add detail missing from the source.
- Search and free LLM tiers are unofficial or rate-limited and may change.

## Disclaimer

This project is a proof of concept for **personal and educational use**. Downloading content may violate YouTube's Terms of Service or copyright law in your jurisdiction. Use it only with content you own or have permission to use. Cover art found through search is typically copyrighted. This project is not affiliated with or endorsed by YouTube, Google, Groq or Pollinations, and nothing here is legal advice.

## License

MIT, see [LICENSE](LICENSE).

---

<p align="center">Built by <a href="https://github.com/darknight2163">Shivam Kumar</a></p>