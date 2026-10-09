# Xun-Qi 尋棲

> every photo finds its nest

[中文](README.md) | English

Send photos, videos and documents from your phone to a Windows PC with the free **LocalSend** app. Xun-Qi receives them directly (compatible with the LocalSend protocol v2), sorts them automatically with Google's **EmbeddingGemma 2**, and lets you find anything later by typing a sentence, searching by image, or spotting duplicates. Everything runs on your own computer — nothing is uploaded to the cloud.

This program is not an official LocalSend product. The user interface is currently in Traditional Chinese.

## Screenshots

| Home | Dark mode |
|---|---|
| ![Home](docs/screenshots/home.png) | ![Dark mode](docs/screenshots/home-dark.png) |

| Search by sentence | Batch review |
|---|---|
| ![Search](docs/screenshots/search.png) | ![Review](docs/screenshots/review.png) |

| Send to phone | Choosing a device in LocalSend |
|---|---|
| ![Send to phone](docs/screenshots/send.png) | <img src="docs/screenshots/phone.png" width="240" alt="Two devices shown on the phone"> |

Sample photos in the screenshots are from Pixabay (Pixabay Content License).

## Features
- Your phone sees two devices: **傳送** (Send, save only) and **傳送並分類** (Send & Sort, save, then sort automatically)
- Every transfer is a batch you can review; once everything checks out, it tells you it's safe to delete from your phone
- Search with a sentence (Chinese or English), text inside photos (OCR), or a moment inside a video
- Search by image, pick the sharpest shot from a burst, compare exact and near duplicates
- Custom categories, GPS places, timeline, export, index backup and restore
- Send to phone (Export → Send to phone):
  - From the library: send received items (whole batches too) back to your phone, verified unchanged before sending
  - From the PC: any file or folder on your computer, sent only and not added to the library
- Your files are never deleted by the program

## Three editions
| Edition | AI model | Runs on | OCR | Video | Size (approx.) |
|---|---|---|---|---|---|
| Lite | ONNX int8 | DirectML or CPU | mobile | every 6 s | 1 GB |
| Standard | PyTorch original | NVIDIA / Intel GPU or CPU | mobile | every 4 s | 2–5 GB |
| Flagship | PyTorch original | NVIDIA / Intel GPU or CPU | server | every 2 s | 2–5 GB |

All three have the same features; they differ in accuracy, speed and size. Not sure? Pick **Lite**.

## Getting started (no install, Windows 10/11 64-bit)
1. Download **Source code (zip)** from the [latest release](../../releases/latest) and extract it anywhere (e.g. `D:\XunQi`). Keep the path short and avoid cloud-synced folders.
2. Run `setup.bat` and choose an edition (it suggests one based on your graphics card).
3. Run `download_model.bat` to download the AI model.
4. From then on, run `start.bat` and open `http://127.0.0.1:8765` in your browser.
5. On your phone (iPhone or Android), install LocalSend, join the same Wi-Fi as the PC, and pick the device ending in **傳送並分類** (Send & Sort).

Full instructions (in Chinese) are in `使用說明.txt`.

## License
Code: Apache License 2.0. Third-party models and data are listed in `NOTICE`.
