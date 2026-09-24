import torch
import soundfile as sf
from transformers import VitsModel, AutoTokenizer

device = "mps" if torch.backends.mps.is_available() else "cpu"
print("Using device:", device)

model_name = "facebook/mms-tts-hin"
print("Loading model (first run downloads weights)...")
model = VitsModel.from_pretrained(model_name).to(device)
tokenizer = AutoTokenizer.from_pretrained(model_name)

text = "नमस्ते! आपका वर्तमान बकाया दस हज़ार रुपये है, जिसे तुरंत चुकता करें।"
inputs = tokenizer(text, return_tensors="pt").to(device)

print("Generating audio...")
with torch.no_grad():
    output = model(**inputs).waveform

audio_arr = output.cpu().numpy().squeeze()
sf.write("test_output.wav", audio_arr, model.config.sampling_rate)
print("Wrote test_output.wav, sample_rate=", model.config.sampling_rate, "duration_sec=", len(audio_arr)/model.config.sampling_rate)
