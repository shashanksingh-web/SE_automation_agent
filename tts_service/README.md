# TTS service (wired in, host-run)

Standalone Hindi text-to-speech experiment for turning a generated `PitchScript.script_hindi`
into audio the SE can play back. Runs directly on the host, outside Docker, on purpose --
Docker Desktop's own VM here is allocated only ~5.8GB RAM / 2 CPUs (`docker info`), too tight
to add an ML model into the same containers already running web/celery_worker/celery_beat/redis
without risking OOM instability for the whole stack. This directory's venv uses the host
Mac's own resources (16GB unified memory, MPS/Metal GPU) instead.

## Model history

- `VibeVoice-Hindi-7B` (the model originally asked for): not viable here. Full model is
  37.4GB; even the 8-bit quantized version needs ~12GB of VRAM, and that figure assumes a
  dedicated NVIDIA GPU -- this Mac has no separate VRAM, everything shares the same 16GB pool.
  The Qwen2.5-7B backbone's inference stack also leans on CUDA-specific optimizations
  (flash-attention, bitsandbytes) with little to no Apple Silicon/MPS support.
- `ai4bharat/indic-parler-tts`: better voice quality, tried second, but is a gated Hugging
  Face repo -- requires a HF account, accepting the model's license on its own page, and an
  access token. Blocked on that (explicit user choice: use MMS-TTS instead rather than do the
  account/token dance).
- `facebook/mms-tts-hin` (**current choice**): no gating, no account needed, 139MB on disk,
  runs on this Mac's MPS backend today. Lower voice quality than Parler-TTS would have given
  (more robotic, VITS-based single-speaker model) but the only one that actually ran without
  further setup. See `test_synth.py` for a minimal working example.

## Requirements note

`requirements.txt` here is Python **3.12** specific -- the host's default `python3` resolves
to 3.14, which is too new for `tokenizers` (a `transformers` dependency): its Rust/PyO3 build
toolchain doesn't yet support Python 3.14, so the wheel build fails outright. Installed
python@3.12 via Homebrew (`/opt/homebrew/bin/python3.12`) specifically for this venv; the rest
of this project's own Python code is unaffected (Django runs in Docker on its own
python:3.12-slim image already).

## Status

Wired into the Django app. `server.py` is a persistent FastAPI service (model loads once at
startup, not per-request) exposing `POST /synthesize` and `GET /health`; `planning/views.py`'s
`pitch_audio` view (`GET /api/planning/pitch/<daily_task_id>/audio/`) calls it over
`host.docker.internal:8765` from inside the web container, strips this app's own pitch-script
markup first, and caches the resulting WAV to `output/pitch_audio/` content-addressed by a hash
of the cleaned text.

Verified live end-to-end 2026-09-25: a fresh (uncached) request through the real Django endpoint
took ~27.5s and returned a valid WAV; a repeat request for the same pitch hit the disk cache and
returned the identical bytes in ~0.3s.

Still genuinely missing: a "Play pitch audio" control in PitchPanel.tsx (the frontend doesn't
call this endpoint yet), and this service has no supervision -- it's a bare `uvicorn` process on
whoever's Mac started it, with nothing to restart it if it dies or the machine reboots. Until
that's addressed, `pitch_audio` reliably works only on a machine where someone has manually
started `server.py` (see Running below); everywhere else it correctly degrades to a 503, never a
500 or a broken response.

## Setup (already done once on this machine)

```bash
brew install python@3.12
/opt/homebrew/bin/python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running it

```bash
source venv/bin/activate
uvicorn server:app --host 0.0.0.0 --port 8765
```

Leave this running in its own terminal (or `nohup`/a process manager) -- there's no
supervision today, so it stays up only as long as this process does. `host.docker.internal`
is Docker Desktop's built-in DNS name for the host machine, reachable from inside the web
container without extra network config; `TTS_SERVICE_URL` (env var, default
`http://host.docker.internal:8765`) overrides it if the service ever runs somewhere else.
