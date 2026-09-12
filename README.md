# ReelForge — Local V1

Customer-facing AI reel creation app. Describe an idea, get a storyboard, edit
the scenes, render a real MP4.

## What V1 does

- Next.js customer UI: landing page, two-step create flow, workspace, project page
- Creation types, input sources, aspect ratio, and a duration control from 5 to 120s
- Duration-aware storyboard planner with a per-category beat sheet
- SQLite persistence for projects, scenes, and uploaded assets
- Image and audio uploads, per scene or per project
- Per-scene editing (title, prompt, caption, length) and prompt regeneration
- FFmpeg render pipeline with live progress, in-browser preview, and MP4 download
- Optional soundtrack muxed over the finished reel

## Requirements

- Node 20+ and Python 3.11+
- FFmpeg and ffprobe on PATH
- A TTF font for captions. Windows and most Linux desktops already have one;
  otherwise set `REELFORGE_FONT` to a `.ttf` path.

## Run the backend

From the project root:

```powershell
cd backend
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

Health check: http://localhost:8000/health — it reports whether FFmpeg was found.

State lives in `backend/data/` (SQLite database, uploads, renders). It is
gitignored; delete the folder to reset.

## Run the frontend

```powershell
cd frontend
npm install
npm run dev
```

Open http://localhost:3000. If the backend is not on `http://localhost:8000`,
copy `.env.local.example` to `.env.local` and set `NEXT_PUBLIC_API_URL`.

## Tests

```powershell
cd backend
python test_reelforge.py
```

Covers the duration maths and the FFmpeg command construction — the two places
a silent mistake produces a broken video rather than an error.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness plus FFmpeg availability |
| POST | `/api/storyboard` | Stateless storyboard preview |
| GET | `/api/projects` | List projects |
| POST | `/api/projects` | Create a project and plan its scenes |
| GET | `/api/projects/{id}` | Project with scenes and assets |
| DELETE | `/api/projects/{id}` | Delete a project and its files |
| PATCH | `/api/projects/{id}/scenes/{sceneId}` | Edit one scene |
| POST | `/api/projects/{id}/scenes/{sceneId}/regenerate` | New prompt for one scene |
| POST | `/api/projects/{id}/uploads` | Upload an image or audio file |
| POST | `/api/projects/{id}/render` | Start a render (background) |

Editing a scene or adding an asset resets the project to `draft` and clears the
rendered video, so the preview never shows a reel that no longer matches the
storyboard.

## Architecture

```
backend/app/
  main.py        FastAPI routes, CORS, static media, background render task
  db.py          sqlite3 schema and connection handling (no ORM)
  storyboard.py  beat sheets and the duration-aware planner
  render.py      FFmpeg pipeline and the generation adapter seam
frontend/
  lib/api.ts     the only place the UI talks to the backend
  app/           landing, create, dashboard, projects/[id]
```

## Swapping in a real video model

`render.generate_scene_clip` is the single seam. Today it has two strategies:
a Ken Burns push over an uploaded still, and a typographic colour card when a
scene has no still. To use LTX, Runway, or Veo, replace that function's body
with a call that writes an MP4 to `out` at the scene's duration and resolution.
Everything downstream — concat, captions, music, progress, status — is unchanged.

## Not in V1

- AI text-to-video generation (the adapter seam above is ready for it)
- Voice-over and text-to-speech
- Auto-generated captions from a script; captions are per-scene and manual
- Accounts and multi-user separation; the workspace is whoever has the URL
