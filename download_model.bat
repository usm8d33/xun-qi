@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
call app\lang.cmd
if /i "%XQL%"=="en" goto msg_en
set "M_TITLE=Xun-Qi 尋棲 - 下載 AI 模型"
set "M_SETUP=請先執行 setup.bat"
set "M_ED=版本："
set "M_HAVE=[1/2] 分類模型已經在 models\embeddinggemma-2，不需要再下載。"
set "M_GET=[1/2] 下載 google/embeddinggemma-2（約 1.5GB，免登入）到 models\embeddinggemma-2 ..."
set "M_FAIL=[錯誤] 下載失敗，請重試"
set "M_HAVE_LITE=[1/2] 輕量版模型已經在 models\embeddinggemma-2-onnx，不需要再下載。"
set "M_GET_LITE=[1/2] 下載 onnx-community/embeddinggemma-2-ONNX 的 int8 模型（約 0.5GB）到 models\embeddinggemma-2-onnx ..."
set "M_OCR_HAVE1=[2/2] OCR 模型（"
set "M_OCR_HAVE2=）已經在 models\ocr，不需要再下載。"
set "M_OCR_GET1=[2/2] 下載 OCR 模型（"
set "M_OCR_GET2=）到 models\ocr ..."
set "M_OCR_FAIL=[提醒] OCR 模型下載失敗。可以之後重跑這個檔案，或自己下載後放到 models\ocr。"
set "M_DONE=完成！之後雙擊 start.bat 就能使用。"
goto msg_done
:msg_en
set "M_TITLE=Xun-Qi - Download AI models"
set "M_SETUP=Run setup.bat first."
set "M_ED=Edition: "
set "M_HAVE=[1/2] The sorting model is already in models\embeddinggemma-2. No download needed."
set "M_GET=[1/2] Downloading google/embeddinggemma-2 (about 1.5 GB, no sign-in needed) to models\embeddinggemma-2 ..."
set "M_FAIL=[Error] Download failed. Please try again."
set "M_HAVE_LITE=[1/2] The Lite model is already in models\embeddinggemma-2-onnx. No download needed."
set "M_GET_LITE=[1/2] Downloading the int8 model from onnx-community/embeddinggemma-2-ONNX (about 0.5 GB) to models\embeddinggemma-2-onnx ..."
set "M_OCR_HAVE1=[2/2] The OCR model ("
set "M_OCR_HAVE2=) is already in models\ocr. No download needed."
set "M_OCR_GET1=[2/2] Downloading the OCR model ("
set "M_OCR_GET2=) to models\ocr ..."
set "M_OCR_FAIL=[Note] Couldn't download the OCR model. Run this file again later, or download it yourself and put it in models\ocr."
set "M_DONE=Done. From now on, double-click start.bat to use Xun-Qi."
:msg_done
title !M_TITLE!
set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" ( echo !M_SETUP! & pause & exit /b 1 )
set "EDITION=standard"
if exist "runtime\edition.txt" set /p EDITION=<"runtime\edition.txt"
for /f "tokens=* delims= " %%e in ("%EDITION%") do set "EDITION=%%e"
echo !M_ED!%EDITION%

if /i "%EDITION%"=="lite" goto lite_model
if exist "models\embeddinggemma-2\model.safetensors" (
  echo !M_HAVE!
) else (
  echo !M_GET!
  "%PY%" -c "from huggingface_hub import snapshot_download; snapshot_download('google/embeddinggemma-2', local_dir=r'models\embeddinggemma-2')"
  if errorlevel 1 ( echo !M_FAIL! & pause & exit /b 1 )
)
goto ocr

:lite_model
if exist "models\embeddinggemma-2-onnx\onnx\vision_encoder_quantized.onnx_data" (
  echo !M_HAVE_LITE!
) else (
  echo !M_GET_LITE!
  "%PY%" -c "from huggingface_hub import snapshot_download; snapshot_download('onnx-community/embeddinggemma-2-ONNX', local_dir=r'models\embeddinggemma-2-onnx', allow_patterns=['onnx/model_quantized.onnx*', 'onnx/vision_encoder_quantized.onnx*', 'tokenizer.json', '*.md'])"
  if errorlevel 1 ( echo !M_FAIL! & pause & exit /b 1 )
)

:ocr
rem OCR 模型：PP-OCRv5。旗艦版用 server（約 170MB，最準），輕量版、標準版用 mobile（約 21MB）；字典內建在模型裡
set "OCRURL=https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.0/onnx/PP-OCRv5"
set "SZ=mobile"
if /i "%EDITION%"=="flagship" set "SZ=server"
if not exist "models\ocr" mkdir "models\ocr"
if exist "models\ocr\ch_PP-OCRv5_det_%SZ%.onnx" if exist "models\ocr\ch_PP-OCRv5_rec_%SZ%.onnx" (
  echo !M_OCR_HAVE1!%SZ%!M_OCR_HAVE2!
  goto done
)
echo !M_OCR_GET1!%SZ%!M_OCR_GET2!
if not exist "models\ocr\ch_PP-OCRv5_det_%SZ%.onnx" curl -L --fail -o "models\ocr\ch_PP-OCRv5_det_%SZ%.onnx" "%OCRURL%/det/ch_PP-OCRv5_det_%SZ%.onnx"
if not exist "models\ocr\ch_PP-OCRv5_rec_%SZ%.onnx" curl -L --fail -o "models\ocr\ch_PP-OCRv5_rec_%SZ%.onnx" "%OCRURL%/rec/ch_PP-OCRv5_rec_%SZ%.onnx"
if errorlevel 1 echo !M_OCR_FAIL!

:done
echo !M_DONE!
pause
