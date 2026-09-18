# ReelForge Kaggle GPU beta worker

Free GPU generation for the ReelForge beta, using open-source models on a
Kaggle T4 session. Two workers exist:

- `wan_worker.py` — Wan 2.1 1.3B, the intended beta provider (`wan`).
  Documented in [Setup: Wan 2.1](#setup-wan-21-the-intended-beta-provider)
  below.
- `reelforge_worker.py` — LTX-Video (`kaggle` provider), kept for now; see
  [Setup: LTX-Video](#setup-ltx-video) further down.

Both share the same worker API, auth token and queue mechanism (see below);
only the model and the queue lane (`provider=wan` vs `provider=kaggle`)
differ.

Kaggle is for development and beta generation. It is **not** long-term
production GPU infrastructure: sessions are time-limited, can be killed
without warning, and there is no availability guarantee. The provider
abstraction exists so this can be replaced without touching the app.

## How it fits together

The backend never starts, calls, or depends on reaching a Kaggle notebook.
Control runs the other way round, because a Kaggle session has no stable
inbound address:

```
Next.js frontend
      |
FastAPI backend  ──>  scene_jobs table  <──  Kaggle worker polls for work
      |                                            |
      |                                      LTX-Video on a T4
      |                                            |
      |               uploads the mp4  <───────────┘
      v
ffmpeg normalisation  ->  concat  ->  audio  ->  final reel  ->  storage
```

1. A render asks the `kaggle` provider for a scene.
2. The provider inserts a `scene_jobs` row and waits.
3. The worker claims it, generates, and uploads the clip.
4. The provider normalises the upload and hands it to the existing pipeline.

Nothing about Kaggle reaches `render.py` or the frontend. Customers see only
"Generating", "Generated" and "Needs another try".

## What the backend does not trust

The uploaded clip's duration, resolution and frame rate are all treated as
suggestions. Every clip goes through `ffmpeg.build_normalize_cmd`, which is
what guarantees the render pipeline gets exactly the scene it asked for.
Concatenation stream-copies, so one worker running a different config would
otherwise silently corrupt the join.

Uploads are also checked before they are accepted: size cap, declared content
type, an ffprobe decode, and a length check. A clip **shorter** than the scene
is refused, because ffmpeg can trim a long clip but cannot extend a short one,
and accepting one would quietly produce a reel shorter than the storyboard.

## Model choice

Default: **`Lightricks/LTX-Video-0.9.7-distilled`**, loaded in-process via
diffusers' `LTXConditionPipeline.from_pretrained()`.

Why this one:

- It's diffusers-native: `from_pretrained()` downloads and caches the weights
  directly from the Hugging Face repo, no separate LTX-Video checkout, custom
  loader, or YAML pipeline config needed - unlike the raw `.safetensors`
  checkpoints the official repo's `inference.py` script expects.
- The checkpoint is 2B-class, so with `enable_model_cpu_offload()` plus
  attention/VAE slicing and tiling it targets a T4's 16 GB. (A prior CLI-based
  runner using the official `inference.py`, with and without the spatial
  upscaler, reliably ran out of memory on a real T4 loading the checkpoint +
  text encoder alone - see "Known limitations" below.)
- It's guidance-distilled: `guidance_scale` 1.0 and roughly 8 steps, which is
  what makes generation times tolerable on a T4.

A T4 is Turing (sm_75) and has **no bfloat16 support**, so fp16 is used. Many
LTX examples show bf16; those target Ampere or newer.

The model is configuration, not a constant: set `REELFORGE_LTX_MODEL_ID` to
any diffusers-compatible LTX repo on Hugging Face.

## Generation geometry

Generation happens **below** the customer's final frame, deliberately:

| Setting | Default | Why |
| --- | --- | --- |
| `REELFORGE_GEN_SHORT_EDGE` | 480 | Fits a T4 with sane generation times |
| `REELFORGE_GEN_LONG_EDGE` | 832 | Both edges must be divisible by 32 |
| `REELFORGE_GEN_FPS` | 25 | LTX frame rate; the pipeline runs at 30 |

The backend's ffmpeg layer scales and crops to the real aspect ratio, so
1:1 and 4:5 work even though the model is asked for a plain portrait or
landscape frame. Frame counts are snapped up to LTX's 8k+1 requirement and
the backend trims to the exact customer duration. There is **no 6-second
floor** here: unlike the hosted API, a 2-second scene generates ~2 seconds.

## Setup: Wan 2.1 (the intended beta provider)

Create a notebook, set the accelerator to **GPU T4 x2**, enable internet, and
add your worker token as a Kaggle Secret rather than typing it in a cell.
No custom repo clone or build step is needed — `diffusers` pulls the model
straight from the Hugging Face Hub.

```python
# Cell 1 - dependencies. transformers is pinned EXACTLY, not >=: an unpinned
# floor resolved to 5.0.0 on a real run and silently reinitialised the text
# encoder's embed_tokens.weight at random instead of loading it (missing
# from the checkpoint per transformers' own load report). No flash_attn: a
# T4 (Turing, cc 7.5) cannot build or run it, and diffusers falls back to
# plain PyTorch SDPA attention anyway.
!pip -q install "diffusers==0.37.1" "transformers==4.49.0" accelerate ftfy imageio imageio-ffmpeg

# Cell 2 - the worker (from your repo, or uploaded as a dataset)
!git clone https://github.com/sujithp28/reelforge.git /kaggle/working/reelforge

# Cell 3 - configuration. Read the token from Kaggle Secrets, never inline.
import os
from kaggle_secrets import UserSecretsClient
os.environ["REELFORGE_API_BASE"] = "https://your-public-url"
os.environ["REELFORGE_WORKER_TOKEN"] = UserSecretsClient().get_secret("REELFORGE_WORKER_TOKEN")
os.environ["REELFORGE_CUDA_DEVICE"] = "0"

# Cell 4 - confirm the model actually runs on this session before touching
# the queue at all. Writes ./wan_self_test.mp4 and exits.
!python /kaggle/working/reelforge/kaggle/wan_worker.py --self-test

# Cell 5 - run the real worker
!python /kaggle/working/reelforge/kaggle/wan_worker.py
```

Set `REELFORGE_VIDEO_PROVIDER=wan` in the backend's `.env` to route renders
here. `REELFORGE_KAGGLE_WORKER_TOKEN` is the same shared secret as the
Kaggle Secret above — one worker API, one token, regardless of which model
is running on the other end.

Verified on a real Kaggle T4 (Sep 2026): one 3.5s 480p clip at 30 steps
took ~18 minutes end to end. Wan2.1 1.3B has no step-distilled checkpoint,
so this is materially slower than the LTX path — a real hardware limit, not
a bug.

## Setup: LTX-Video

The original beta provider (`kaggle`), kept for now. Same accelerator and
worker-token setup as above.

Runs `Lightricks/LTX-Video-0.9.7-distilled` in-process via diffusers'
`LTXConditionPipeline` - no separate LTX-Video repo checkout or custom
`--pipeline_config` YAML needed. `diffusers` downloads and caches the weights
itself on first `from_pretrained()` call, the same way `wan_worker.py`
handles Wan.

```python
# Cell 1 - dependencies (no repo clone; diffusers loads the model directly)
!pip -q install diffusers transformers accelerate

# Cell 2 - the worker (from your repo, or uploaded as a dataset)
!git clone https://github.com/sujithp28/reelforge.git /kaggle/working/reelforge

# Cell 3 - configuration. Read the token from Kaggle Secrets, never inline.
import os
from kaggle_secrets import UserSecretsClient
os.environ["REELFORGE_API_BASE"] = "https://your-public-url"
os.environ["REELFORGE_WORKER_TOKEN"] = UserSecretsClient().get_secret("REELFORGE_WORKER_TOKEN")
os.environ["REELFORGE_CUDA_DEVICE"] = "0"

# Cell 4 - run it
!python /kaggle/working/reelforge/kaggle/reelforge_worker.py
```

One GPU is used. Two are available, but splitting one small model across both
buys nothing and costs a lot of complexity.

## The backend must be reachable from Kaggle

The worker polls **inbound** to your API, so `localhost:8000` will not work
from a Kaggle session. You need either:

- a deployed API with a public HTTPS URL, or
- a secure tunnel to your local backend (ngrok, Cloudflare Tunnel, tailscale
  funnel, or similar).

**No tunnel is installed or configured by this repository.** Set it up
yourself and point `REELFORGE_API_BASE` at the resulting HTTPS URL. Because
the worker endpoints are guarded by a shared secret, exposing the API this way
means exposing the whole API: use a strong token, prefer a URL that is not
guessable, and take the tunnel down when you are not testing.

## Trying it without a GPU

`--dry-run` swaps the model for a synthetic ffmpeg clip, so the whole loop can
be exercised locally with no GPU, no weights and no downloads:

```bash
REELFORGE_API_BASE=http://localhost:8000 REELFORGE_WORKER_TOKEN=dev-token python kaggle/reelforge_worker.py --dry-run --max-jobs 5
REELFORGE_API_BASE=http://localhost:8000 REELFORGE_WORKER_TOKEN=dev-token python kaggle/wan_worker.py --dry-run --max-jobs 5
```

It deliberately produces the wrong frame rate and a slightly wrong length, to
prove the backend really is normalising what it receives.

## Environment variables

Worker side (set in the Kaggle session):

| Variable | Default | Purpose |
| --- | --- | --- |
| `REELFORGE_API_BASE` | required | Public HTTPS URL of the backend |
| `REELFORGE_WORKER_TOKEN` | required | Must match the backend's token |
| `REELFORGE_WORKER_ID` | `kaggle-1` | Shown in logs; distinguishes workers |
| `REELFORGE_LTX_MODEL_ID` | `Lightricks/LTX-Video-0.9.7-distilled` | HF repo id, loaded via `LTXConditionPipeline.from_pretrained` |
| `REELFORGE_LTX_GUIDANCE_SCALE` | `1.0` | Guidance scale (distilled checkpoints want ~1.0, not 3+) |
| `REELFORGE_CUDA_DEVICE` | `0` | Which GPU to use |
| `REELFORGE_GEN_SHORT_EDGE` | `480` | Generation short edge (÷32) |
| `REELFORGE_GEN_LONG_EDGE` | `832` | Generation long edge (÷32) |
| `REELFORGE_GEN_FPS` | `25` | Generation frame rate |
| `REELFORGE_STEPS_STANDARD` | `8` | Steps for Standard quality |
| `REELFORGE_STEPS_HIGH` | `10` | Steps for High quality |
| `REELFORGE_MAX_SCENE_SECONDS` | `20` | Refuse scenes longer than this |
| `REELFORGE_GENERATION_TIMEOUT` | `1500` | Reserved; not currently enforced (same as `wan_worker.py`) - the backend's `REELFORGE_KAGGLE_CLAIM_TIMEOUT` requeues a stuck job instead |

Backend side: see `.env.example` for `REELFORGE_KAGGLE_*`.

## Failure handling

- A scene that fails is reported to the backend and the worker keeps polling.
  One bad scene never ends the session.
- A retryable failure goes back to `pending` for another attempt, bounded by
  `REELFORGE_KAGGLE_MAX_ATTEMPTS`.
- A job whose worker disappears is requeued after
  `REELFORGE_KAGGLE_CLAIM_TIMEOUT`, so a killed Kaggle session cannot wedge a
  render.
- If nothing finishes within `REELFORGE_KAGGLE_JOB_TIMEOUT`, the scene fails
  with "AI generation is temporarily unavailable. Please try this scene
  again." Customers never see CUDA errors, model names, tokens or paths.

## Known limitations

- **The GPU path is unverified.** There is no GPU in the development
  environment. A prior CLI-based `LTXRunner` (shelling out to the official
  LTX-Video repo's `inference.py`) was tested on a real Kaggle T4 and
  reliably hit `torch.OutOfMemoryError` loading the 2B distilled checkpoint +
  text encoder alone (14.56 GiB total, both with and without the spatial
  upscaler) - this in-process diffusers rewrite is meant to fix that with
  `enable_model_cpu_offload()` plus attention/VAE slicing and tiling, but
  has not itself been run against real weights yet. Everything else,
  including the full queue, upload, normalisation and retry flow, is
  verified with the `--dry-run` runner.
- Standard and High both use the same distilled checkpoint in beta, and
  differ only in step count. The UI does not claim otherwise.
- No visual consistency guarantees between scenes.
- One worker generates one scene at a time.
