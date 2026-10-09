@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Xun-Qi 尋棲 - 下載 AI 模型
set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" ( echo 請先執行 setup.bat & pause & exit /b 1 )
set "EDITION=standard"
if exist "runtime\edition.txt" set /p EDITION=<"runtime\edition.txt"
for /f "tokens=* delims= " %%e in ("%EDITION%") do set "EDITION=%%e"
echo 版本：%EDITION%

if /i "%EDITION%"=="lite" goto lite_model
if exist "models\embeddinggemma-2\model.safetensors" (
  echo [1/2] 分類模型已經在 models\embeddinggemma-2，不需要再下載。
) else (
  echo [1/2] 下載 google/embeddinggemma-2（約 1.5GB，免登入）到 models\embeddinggemma-2 ...
  "%PY%" -c "from huggingface_hub import snapshot_download; snapshot_download('google/embeddinggemma-2', local_dir=r'models\embeddinggemma-2')"
  if errorlevel 1 ( echo [錯誤] 下載失敗，請重試 & pause & exit /b 1 )
)
goto ocr

:lite_model
if exist "models\embeddinggemma-2-onnx\onnx\vision_encoder_quantized.onnx_data" (
  echo [1/2] 輕量版模型已經在 models\embeddinggemma-2-onnx，不需要再下載。
) else (
  echo [1/2] 下載 onnx-community/embeddinggemma-2-ONNX 的 int8 模型（約 0.5GB）到 models\embeddinggemma-2-onnx ...
  "%PY%" -c "from huggingface_hub import snapshot_download; snapshot_download('onnx-community/embeddinggemma-2-ONNX', local_dir=r'models\embeddinggemma-2-onnx', allow_patterns=['onnx/model_quantized.onnx*', 'onnx/vision_encoder_quantized.onnx*', 'tokenizer.json', '*.md'])"
  if errorlevel 1 ( echo [錯誤] 下載失敗，請重試 & pause & exit /b 1 )
)

:ocr
rem OCR 模型：PP-OCRv5。旗艦版用 server（約 170MB，最準），輕量版、標準版用 mobile（約 21MB）；字典內建在模型裡
set "OCRURL=https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.0/onnx/PP-OCRv5"
set "SZ=mobile"
if /i "%EDITION%"=="flagship" set "SZ=server"
if not exist "models\ocr" mkdir "models\ocr"
if exist "models\ocr\ch_PP-OCRv5_det_%SZ%.onnx" if exist "models\ocr\ch_PP-OCRv5_rec_%SZ%.onnx" (
  echo [2/2] OCR 模型（%SZ%）已經在 models\ocr，不需要再下載。
  goto done
)
echo [2/2] 下載 OCR 模型（%SZ%）到 models\ocr ...
if not exist "models\ocr\ch_PP-OCRv5_det_%SZ%.onnx" curl -L --fail -o "models\ocr\ch_PP-OCRv5_det_%SZ%.onnx" "%OCRURL%/det/ch_PP-OCRv5_det_%SZ%.onnx"
if not exist "models\ocr\ch_PP-OCRv5_rec_%SZ%.onnx" curl -L --fail -o "models\ocr\ch_PP-OCRv5_rec_%SZ%.onnx" "%OCRURL%/rec/ch_PP-OCRv5_rec_%SZ%.onnx"
if errorlevel 1 echo [提醒] OCR 模型下載失敗。可以之後重跑這個檔案，或自己下載後放到 models\ocr。

:done
echo 完成！之後雙擊 start.bat 就能使用。
pause
