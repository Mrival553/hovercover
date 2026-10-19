# -*- coding: utf-8 -*-
"""验证打包后的 exe：启动它、截它自己的窗口、再关掉（不碰桌面其它内容）。

用法：python tools/verify_pack.py [exe路径] [输出png]
"""

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import capture_demo as cap          # noqa: E402  （借用截图用的 BitBlt 代码）
import file_manager as fm           # noqa: E402

fm.enable_dpi_awareness()           # 不声明 DPI 感知的话，坐标会被系统按缩放虚拟化，截不全
user32 = ctypes.windll.user32


def main(argv) -> int:
    exe = Path(argv[0]) if argv else \
        ROOT / "打包" / fm.APP_NAME / (fm.APP_NAME + ".exe")
    out = Path(argv[1]) if len(argv) > 1 else ROOT / "docs" / "packaged-running.png"
    if not exe.is_file():
        print("找不到 exe：%s" % exe)
        return 1

    log = ROOT / "build" / "verify_out.txt"
    log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([str(exe), "--folder", str(ROOT / "demo")],
                            stdout=open(log, "w"), stderr=subprocess.STDOUT)
    print("已启动：%s（pid %d）" % (exe.name, proc.pid))
    hwnd = 0
    for _ in range(40):
        time.sleep(0.25)
        hwnd = user32.FindWindowW(None, fm.APP_TITLE)
        if hwnd:
            break
    if not hwnd:
        print("没找到窗口（标题 %r）" % fm.APP_TITLE)
        proc.terminate()
        return 1
    time.sleep(1.5)                     # 等它画完（缩略图/文件夹大小）
    r = cap.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    print("窗口：%dx%d @ (%d,%d)  标题=%r" % (w, h, r.left, r.top, fm.APP_TITLE))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(cap.bgra_to_png(cap.capture_region(r.left, r.top, w, h), w, h))
    print("截图已保存：%s" % out)
    data_dir = exe.parent / "data"
    print("data 目录在 exe 旁边：%s（%s）" % (
        data_dir.is_dir(), sorted(p.name for p in data_dir.glob("*"))))
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except Exception:
        proc.kill()
    print("已关闭打包后的程序")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
