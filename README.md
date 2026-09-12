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
- Optional soundtrack with volume and fade-out, muxed over the finished reel

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
python -m pip install -r requirements-dev.txt
python test_reelforge.py
python test_api.py
python check_schema_order.py
```

- `test_reelforge.py` — duration maths and FFmpeg command construction, the two
  places a silent mistake yields a broken video rather than an error.
- `test_api.py` — every route, status code, and validation rule, plus the
  storage and provider abstractions. Runs against a temporary data directory
  with the job runner set to `external`, so nothing renders.
- `check_schema_order.py` — fails if a table references one declared later.
  SQLite accepts that; PostgreSQL does not.

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
| PATCH | `/api/projects/{id}/audio` | Soundtrack volume and fade |
| POST | `/api/projects/{id}/render` | Enqueue a render job |
| GET | `/api/projects/{id}/jobs/{jobId}` | Render job state |

Editing a scene or adding an asset resets the project to `draft` and clears the
rendered video, so the preview never shows a reel that no longer matches the
storyboard.

## Architecture

```
backend/app/
  main.py        HTTP only: validation, transactions, enqueueing
  config.py      every environment-dependent setting
  db.py          connections, schema, and the SQL dialect seam
  repo.py        every SQL statement in the application
  storage.py     Storage protocol, LocalStorage, S3 boundary
  jobs.py        render jobs, decoupled from the request layer
  providers.py   VideoGenerator protocol, mock generator, LTX boundary
  render.py      assembly: provider clips -> concat -> audio -> reel
  ffmpeg.py      command construction and execution
  storyboard.py  beat sheets and the duration-aware planner
frontend/
  lib/api.ts     the only place the UI talks to the backend
  app/           landing, create, dashboard, projects/[id]
```

Layering runs one way: `main` calls `repo` and `jobs`; `jobs` calls `render`;
`render` calls a provider and `ffmpeg`. Nothing lower reaches back up, so each
layer is testable without the one above it.

## Configuration

Defaults are chosen so the app runs locally with no configuration at all.

| Variable | Default | Purpose |
| --- | --- | --- |
| `REELFORGE_DB_DIALECT` | `sqlite` | `sqlite` or `postgres` |
| `REELFORGE_DATA_DIR` | `backend/data` | Local database and media root |
| `REELFORGE_STORAGE` | `local` | `local` or `s3` |
| `REELFORGE_VIDEO_PROVIDER` | `mock` | `mock` or `ltx` |
| `REELFORGE_JOB_RUNNER` | `thread` | `thread` or `external` |
| `REELFORGE_LTX_ENDPOINT` | unset | Enables the LTX provider |
| `REELFORGE_FONT` | autodetected | Caption font path |
| `REELFORGE_CORS_ORIGINS` | localhost:3000 | Comma-separated origins |

`/health` reports which of these are active and which providers can run.

## The four swap points

Each is a boundary with the local implementation behind it, not a rewrite.

**SQLite to PostgreSQL.** All SQL lives in `repo.py`, written with `?`
placeholders and a `{now}` token that `db.sql()` rewrites per dialect. Adding
PostgreSQL means adding a connection factory in `db.connect()`. No query and no
API response changes, so the frontend contract is untouched. The schema already
declares tables in dependency order and uses no SQLite-only syntax;
`check_schema_order.py` keeps it that way.

**Local disk to S3.** The database stores an opaque storage key such as
`uploads/proj_abc/asset_def.jpg`, never an absolute path. `S3Storage` in
`storage.py` documents the five methods to implement. Because `url_for` already
returns absolute URLs correctly through the frontend's `mediaUrl`, no stored row
and no UI code changes.

**Mock renderer to LTX.** `providers.py` defines the `VideoGenerator` protocol.
`MockGenerator` is the working local renderer and stays the default;
`LTXGenerator` is declared and selectable but reports itself unavailable until
`REELFORGE_LTX_ENDPOINT` is set, so an unconfigured render is rejected at the
API with a 503 rather than failing halfway through. Implementing `generate` is
the only change needed. No GPU infrastructure is required to run or test today.

**Background thread to a real queue.** A render request only writes a
`render_jobs` row and returns its id; execution happens outside the request.
Replacing `jobs.submit()` with a broker publish and running `jobs.run_job()` in
a worker is the whole migration. `REELFORGE_JOB_RUNNER=external` makes the API
enqueue only, which is how you verify the split before adding a broker.

FFmpeg remains the assembly layer in all cases: providers produce per-scene
clips, and `render.py` joins them.

## Operational notes

- In-process jobs do not survive a restart, so on boot the app fails any
  project left in `rendering` rather than leaving it spinning forever.
- Starting a render uses a conditional update, so two concurrent requests
  cannot both start a pipeline writing the same output file.
- `duration` is always the sum of the scene durations. Editing a scene resyncs
  it and validates the resulting total against the 5 to 120 second range.
- Any storyboard, asset, or audio change clears the rendered video, so the
  preview can never show a reel that no longer matches the storyboard.

## Not in V1

- AI text-to-video generation. The LTX provider is a declared boundary, not an
  implementation.
- Voice-over and text-to-speech.
- Auto-generated captions from a script; captions are per-scene and manual.
- Accounts and multi-user separation; the workspace is whoever has the URL.
- A real broker, and the S3 storage backend. Both are boundaries today.
