"""setup.bat 最後檢查：AI 套件能不能用顯示卡。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.lang import t as _t  # noqa: E402

if len(sys.argv) > 1 and sys.argv[1] == "lite":
    import onnxruntime as ort
    p = ort.get_available_providers()
    print(_t("gpu.lite_ort"), ort.__version__)
    print(_t("gpu.dml_ok") if "DmlExecutionProvider" in p else _t("gpu.cpu"))
    sys.exit(0)

import torch

print("PyTorch", torch.__version__)
try:
    if torch.cuda.is_available():
        x = torch.ones(4, device="cuda") * 2  # 確認真的能在這張卡上執行（RTX 50 需要新版）
        assert float(x.sum()) == 8.0
        print(_t("gpu.cuda_ok"), torch.cuda.get_device_name(0))
    elif getattr(torch, "xpu", None) is not None and torch.xpu.is_available():
        x = torch.ones(4, device="xpu") * 2
        assert float(x.sum()) == 8.0
        print(_t("gpu.xpu_ok"), torch.xpu.get_device_name(0))
    else:
        print(_t("gpu.cpu"))
except Exception as e:  # noqa
    print(_t("gpu.test_failed"), e)
