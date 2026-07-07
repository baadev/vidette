# Hardware guide

> All numbers are **design targets** until the M2 benchmark harness ships; each tier below
> then gets published measurements per release. Vidette's compute budget is a feature: the
> reference platform is a ~$150 mini-PC, not a GPU rack.

## Quick picks

| Tier | Hardware | Cameras (1080p–2K) | What runs locally | Notes |
|---|---|---|---|---|
| Minimal | Raspberry Pi 5 (8 GB) + SSD | 2–4 | T0–T2; T3 via cloud (opt-in) or off | USB/NVMe SSD mandatory — SD cards die from segment writes |
| **Sweet spot** | Intel N100/N150 mini-PC, 16 GB, 2 TB NVMe | 4–8 | T0–T2 (OpenVINO on iGPU); small VLM experimental | ~7–15 W; the reference box for all published budgets |
| Accelerated edge | + Hailo-8/-8L (incl. RPi AI Kit) or Coral | 8–16 | T0–T2 at higher fps | adapter-driven, 📐 M3 |
| GPU | any x86 + RTX 3060-class (12 GB) | 8–16+ | everything incl. 7B-class VLM locally | TensorRT path |
| Jetson | Orin Nano / NX | 4–8 | T0–T2 + small VLM | one box, low power |
| Apple Silicon | M-series Mac mini | 4–8 | everything incl. VLM (CoreML/Metal) | excellent dev machine; popular home-server choice |

## Sizing logic

- **Decode is the hidden cost.** Vidette only decodes **substreams** for analysis (main
  streams are recorded codec-copy, ~zero CPU). Always configure the camera's low-res
  substream; a missing substream is the #1 cause of oversized hardware.
- **Detection fps is elastic.** 5 fps during motion is the default; the shedding ladder lowers
  it under pressure instead of falling over.
- **The VLM is rare by design** (~10–50 calls/day/camera — see
  [the cascade](architecture/ai-pipeline.md)); it needs *capacity*, not *throughput*. A small
  local model that answers in 5 s is fine — the fast alert already went out at T2.
- **RAM:** 8 GB runs the core comfortably; 16 GB adds headroom for embeddings + a small local
  VLM; the VLM's needs dominate above that.
- **Storage:** see the [sizing table](architecture/storage.md#sizing). Prefer NVMe/SATA SSD
  for the DB + previews, HDD acceptable for segment archive.

## Accelerator matrix

| Backend | Tier 1 detect | Tier 3 VLM | Status |
|---|---|---|---|
| CPU (ONNX Runtime) | ✓ (small models) | slow, not recommended | 📐 M2 |
| OpenVINO (Intel CPU/iGPU) | ✓ reference path | small VLMs experimental | 📐 M2 |
| CUDA/TensorRT (NVIDIA) | ✓ | ✓ (Ollama/llama.cpp offload) | 📐 M2/M3 |
| CoreML/Metal (Apple) | ✓ | ✓ | 📐 M2/M3 |
| Hailo-8 / -8L | ✓ | — | 📐 M3 (plugin) |
| Coral TPU | ✓ (legacy niche) | — | 🔭 demand-driven |

## Total cost of ownership framing

A one-time ~$150–300 box + disk replaces per-camera cloud subscription tiers, keeps footage
at home, and adds capabilities the subscriptions don't have. We publish power draw with the
benchmarks (target: < 15 W steady-state on the reference box) — a security system shouldn't
cost more in electricity than it saved in subscriptions.
