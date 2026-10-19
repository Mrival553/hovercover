# -*- coding: utf-8 -*-
"""打包脚本：把 file_manager.py 打成绿色版文件夹，输出到 ..\\打包\\

用法：
    python tools/build_pack.py            # onedir（文件夹版，推荐）
    python tools/build_pack.py --onefile  # 单个 exe

做的事情：
  1) 用程序自己的绘图代码生成多尺寸 app.ico（不依赖任何图片库）
  2) 调 PyInstaller（--windowed / 排除用不到的模块）
  3) 在 打包\\ 里补一份使用说明，并顺手跑一次冒烟测试
"""

import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import file_manager as fm  # noqa: E402

APP_NAME = fm.APP_NAME
OUT_DIR = ROOT / "打包"
BUILD_DIR = ROOT / "build"
ICON_PATH = BUILD_DIR / "app.ico"


def draw_icon(size: int) -> bytes:
    """画一枚"文件夹 + 图片"图标（用程序自带的纯 Python 绘图代码）。"""
    S = 4
    W = size * S
    pm = fm.Pixmap(W, W)
    pm.rect(0, 0, W, W, (38, 62, 104), radius=int(W * 0.22))            # 圆角底
    pm.rect(W * 0.15, W * 0.24, W * 0.60, W * 0.36,
            (214, 150, 58), int(W * 0.04))                              # 文件夹标签页
    pm.rect(W * 0.12, W * 0.30, W * 0.74, W * 0.76,
            (240, 176, 74), int(W * 0.07))                              # 文件夹主体
    pm.rect(W * 0.12, W * 0.36, W * 0.74, W * 0.76,
            (249, 199, 112), int(W * 0.07))
    pm.rect(W * 0.46, W * 0.46, W * 0.90, W * 0.90, (255, 255, 255),
            int(W * 0.10))                                              # 图片徽标
    pm.rect(W * 0.49, W * 0.49, W * 0.87, W * 0.87, (74, 144, 217),
            int(W * 0.08))
    pm.tri((W * 0.52, W * 0.84), (W * 0.66, W * 0.60), (W * 0.80, W * 0.84),
           (255, 255, 255))
    pm.disc(W * 0.76, W * 0.58, W * 0.045, (255, 209, 102))
    return fm.png_bytes(pm.downscale(S))


def write_ico(path: Path, sizes=(16, 32, 48, 64, 128, 256)) -> Path:
    """把多个尺寸的 PNG 装进一个 .ico（Vista 以后都支持 PNG 图标）。"""
    images = [(s, draw_icon(s)) for s in sizes]
    entries = b""
    payload = b""
    offset = 6 + 16 * len(images)
    for s, data in images:
        dim = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32,
                               len(data), offset)
        payload += data
        offset += len(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + entries + payload)
    return path


def folder_size_kb(path: Path) -> float:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total / 1024.0


def main(argv) -> int:
    onefile = "--onefile" in argv
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    write_ico(ICON_PATH)
    print("图标已生成：%s" % ICON_PATH)

    cmd = [sys.executable, "-m", "PyInstaller",
           "--noconfirm", "--clean",
           "--windowed",                        # 不要控制台窗口
           "--onefile" if onefile else "--onedir",
           "--name", APP_NAME,
           "--icon", str(ICON_PATH),
           "--distpath", str(OUT_DIR),
           "--workpath", str(BUILD_DIR / "work"),
           "--specpath", str(BUILD_DIR),
           # 这些模块用不到，排除掉能小几 MB
           "--exclude-module", "test",
           "--exclude-module", "unittest",
           "--exclude-module", "pydoc",
           "--exclude-module", "doctest",
           "--exclude-module", "lib2to3",
           "--exclude-module", "pdb",
           "--exclude-module", "distutils",
           "--exclude-module", "setuptools",
           "--exclude-module", "tkinter.test",
           str(ROOT / "file_manager.py")]
    print("开始打包（首次可能要 1-3 分钟）…")
    t0 = time.time()
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        print("打包失败，退出码 %d" % rc)
        return rc

    app_dir = OUT_DIR / APP_NAME
    exe = app_dir / (APP_NAME + ".exe")
    if not onefile and not exe.is_file():
        print("没找到生成的 exe：%s" % exe)
        return 1
    if not onefile and exe.is_file():
        print("打包完成，用时 %.0f 秒；目录大小 %.1f MB"
              % (time.time() - t0, folder_size_kb(app_dir) / 1024.0))
    if onefile and exe.is_file():
        print("打包完成，用时 %.0f 秒；exe 大小 %.1f MB"
              % (time.time() - t0, exe.stat().st_size / 1048576.0))

    # 使用说明放到分发目录里
    readme = OUT_DIR / "使用说明.txt"
    readme.write_text(
        "%s v%s（只读模式）\r\n"
        "================================\r\n\r\n"
        "1. 双击「%s.exe」即可运行，不需要安装 Python。\r\n"
        "2. 「%s.exe」和「_internal」文件夹必须放在一起（_internal 是运行时），别分开。\r\n"
        "3. 程序自己的数据（评分/备注/封面副本/缓存）都在同目录的 data 文件夹里，\r\n"
        "   更新版本时只替换程序文件、保留 data 文件夹即可，标注不会丢。\r\n"
        "4. 它只做只读扫描和预览，绝不会删除、移动、重命名、解压、修改或覆盖你的文件。\r\n"
        "   想清空程序自己的数据：工具栏「安全设置」→ 开锁 →「格式化（恢复出厂设置）」。\r\n"
        "5. 首次运行 Windows 可能提示「已保护你的电脑」（因为 exe 没有数字签名），\r\n"
        "   点「更多信息」→「仍要运行」即可。\r\n"
        "6. JPG/WebP 预览需要 Pillow，已打包在内，无需额外安装。\r\n"
        % (APP_NAME, fm.APP_VERSION, APP_NAME, APP_NAME), encoding="utf-8-sig")
    print("使用说明：%s" % readme)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
