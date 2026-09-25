"""Standalone Hindi TTS service (facebook/mms-tts-hin) -- runs on the HOST, not Docker.

Why a separate process instead of importing the model straight into the Django app:
Docker Desktop's own VM here is capped at ~5.8GB RAM / 2 CPUs (see `docker info`),
already shared by web/celery_worker/celery_beat/redis -- loading an ML model into that
same allocation risks OOM for the whole stack. This runs against the host Mac's own
16GB instead (see README.md for the full VibeVoice-Hindi-7B -> Indic Parler-TTS ->
MMS-TTS-Hindi decision trail).

The model loads ONCE at process startup (a few seconds), not per-request -- the whole
point of this being a persistent service rather than a one-shot script like
test_synth.py. planning/views.py's pitch-audio endpoint calls this over HTTP via
host.docker.internal from inside the web container.

Run with: uvicorn server:app --host 0.0.0.0 --port 8765
(from this directory, with venv activated)
"""
from __future__ import annotations

import io
import logging
import re

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from transformers import AutoTokenizer, VitsModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tts_service")

MODEL_NAME = "facebook/mms-tts-hin"
# MPS (Apple Silicon GPU) when available, CPU fallback otherwise -- confirmed both work
# on this machine in test_synth.py; MPS is meaningfully faster for a 7-8 second clip.
_DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"

# Loaded once at import time (module-level, not per-request) -- FastAPI/uvicorn imports
# this module exactly once per worker process, so this genuinely runs a single time.
logger.info("Loading %s onto %s ...", MODEL_NAME, _DEVICE)
_model = VitsModel.from_pretrained(MODEL_NAME).to(_DEVICE)
_tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
_model.eval()
logger.info("Model loaded, sampling_rate=%d", _model.config.sampling_rate)

app = FastAPI(title="Hindi TTS (MMS-TTS-Hindi)")

# A real pitch script (Sale+Collection combo, Club section, 10 scheme bullets) easily
# runs 3000-4000+ chars -- confirmed live (JAI MAA MAHAMAYA KRISHI SEWA KENDRA's pitch,
# 3688 chars). A single VITS forward pass over that much text both takes a while and
# risks a garbled/truncated tail (the model's positional encoding and training data
# skew toward much shorter utterances) -- so instead of rejecting long text, this
# chunks it into sentence-sized pieces (Hindi danda "।" plus ?/!), synthesizes each
# separately, and concatenates the resulting audio with a short silence gap, matching
# how a person would naturally pause between sentences anyway.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[।?!])\s+")
_MAX_CHUNK_CHARS = 400  # comfortably inside what a single VITS call handles cleanly
_SILENCE_GAP_SECONDS = 0.35


class SynthesizeRequest(BaseModel):
    text: str


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "device": _DEVICE, "sampling_rate": _model.config.sampling_rate}


def _split_into_chunks(text: str) -> list[str]:
    """Sentence-boundary split, then greedily packed back up to _MAX_CHUNK_CHARS --
    keeps chunks as large as safely possible (fewer chunks = fewer audible seams) while
    never splitting mid-sentence and never exceeding the per-call size that risks a
    degraded/truncated synthesis."""
    sentences = [s.strip() for s in _SENTENCE_BOUNDARY_RE.split(text) if s.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) > _MAX_CHUNK_CHARS and current:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _synthesize_one(text: str) -> np.ndarray:
    inputs = _tokenizer(text, return_tensors="pt").to(_DEVICE)
    with torch.no_grad():
        waveform = _model(**inputs).waveform
    return waveform.cpu().numpy().squeeze()


@app.post("/synthesize")
def synthesize(req: SynthesizeRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must be non-empty")

    chunks = _split_into_chunks(text)
    logger.info("Synthesizing %d chars across %d chunk(s)", len(text), len(chunks))

    sample_rate = _model.config.sampling_rate
    silence = np.zeros(int(sample_rate * _SILENCE_GAP_SECONDS), dtype=np.float32)
    pieces: list[np.ndarray] = []
    for i, chunk in enumerate(chunks):
        pieces.append(_synthesize_one(chunk))
        if i < len(chunks) - 1:
            pieces.append(silence)
    audio = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]

    buf = io.BytesIO()
    sf.write(buf, audio, sample_rate, format="WAV")
    buf.seek(0)
    return Response(content=buf.read(), media_type="audio/wav")
