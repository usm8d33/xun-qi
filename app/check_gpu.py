"""setup.bat 最後檢查：AI 套件能不能用顯示卡。"""
import sys

if len(sys.argv) > 1 and sys.argv[1] == "lite":
    import onnxruntime as ort
    p = ort.get_available_providers()
    print("輕量版：onnxruntime", ort.__version__)
    print("可以使用顯示卡（DirectML）" if "DmlExecutionProvider" in p else "AI 會使用 CPU（速度較慢，但一樣能分類和搜尋）")
    sys.exit(0)

import torch

print("PyTorch", torch.__version__)
try:
    if torch.cuda.is_available():
        x = torch.ones(4, device="cuda") * 2  # 確認真的能在這張卡上執行（RTX 50 需要新版）
        assert float(x.sum()) == 8.0
        print("可以使用顯示卡：", torch.cuda.get_device_name(0))
    elif getattr(torch, "xpu", None) is not None and torch.xpu.is_available():
        x = torch.ones(4, device="xpu") * 2
        assert float(x.sum()) == 8.0
        print("可以使用 Intel 顯示晶片：", torch.xpu.get_device_name(0))
    else:
        print("AI 會使用 CPU（速度較慢，但一樣能分類和搜尋）")
except Exception as e:  # noqa
    print("［注意］顯示卡測試失敗，程式會自動改用 CPU：", e)
