# -*- coding: utf-8 -*-
"""
开发期自检脚本：把界面真正渲染出来并截图，用于人工核对效果。
只截取本程序自己的窗口区域，不移动鼠标、不操作桌面其它内容。

用法：  python tools/capture_demo.py [输出目录] [示例文件夹]
"""

import ctypes
import os
import shutil
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import file_manager as fm  # noqa: E402

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
SRCCOPY = 0x00CC0020


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def capture_region(x: int, y: int, w: int, h: int) -> bytes:
    """抓取屏幕上一块矩形，返回 BGRA 原始字节。"""
    hdc = user32.GetDC(0)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(memdc, bmp)
    gdi32.BitBlt(memdc, 0, 0, w, h, hdc, x, y, SRCCOPY)
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h          # 自顶向下
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0      # BI_RGB
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(memdc, bmp, 0, h, buf, ctypes.byref(info), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(0, hdc)
    return buf.raw


def bgra_to_png(raw: bytes, w: int, h: int) -> bytes:
    pm = fm.Pixmap(w, h)
    for i in range(w * h):
        b, g, r, a = raw[i * 4:i * 4 + 4]
        j = i * 4
        pm.buf[j] = r
        pm.buf[j + 1] = g
        pm.buf[j + 2] = b
        pm.buf[j + 3] = 255
    return fm.png_bytes(pm)


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs"
    demo = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "demo"
    out_dir.mkdir(parents=True, exist_ok=True)
    work_data = out_dir / "_capture_data"
    shutil.rmtree(work_data, ignore_errors=True)
    work_data.mkdir(parents=True, exist_ok=True)

    fm.enable_dpi_awareness()
    fm.set_write_root(work_data)
    fm.ensure_data_dirs()

    import tkinter as tk
    root = tk.Tk()
    app = fm.App(root, folder=str(demo))

    # 预先配置几个封面与评分，让截图能体现真实效果
    lib, folder = app.lib, app.folder
    pairs = [("会议记录.txt", "封面-海边.png", 5, "季度例会要点"),
             ("安装包.zip", "封面-晚霞.png", 3, ""),
             ("demo_script.py", "封面-森林.png", 4, "")]
    for name, cover, rating, note in pairs:
        src = demo / cover
        if (demo / name).exists() and src.exists():
            rel = lib.import_cover(src, "%s|%s" % (fm.norm_root(folder), name))
            lib.update(folder, name, cover=rel, rating=rating, note=note)
    # 演示"启动项"：给会议记录.txt 挂一个启动程序（只记录路径，不会真的运行）
    notepad = Path(os.environ.get("WINDIR", r"C:\Windows")) / "notepad.exe"
    if (demo / "会议记录.txt").exists() and notepad.exists():
        lib.update(folder, "会议记录.txt", launch=str(notepad))
    # 演示"最近管理的文件夹"
    app._remember_folder(str(demo / "示例子文件夹"), save=False)
    app._remember_folder(str(demo), save=False)
    app._remember_folder(str(ROOT), save=False)
    app.refresh()
    app.note_var.set("")

    root.geometry("1400x760+40+30")
    root.attributes("-topmost", True)
    for _ in range(20):
        root.update()
        time.sleep(0.05)
    root.lift()
    for _ in range(25):          # 等后台线程把文件夹大小算出来
        root.update()
        time.sleep(0.05)

    # 截图期间屏蔽真实鼠标移动/离开带来的事件，保证画面稳定
    # （绑定的是当时的绑定方法对象，必须 unbind 才能真正解绑）
    app.tree.unbind("<Motion>")
    app.tree.unbind("<Leave>")

    x, y = 40, 30
    hwnd = user32.FindWindowW(None, fm.APP_TITLE)
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    x, y, w, h = rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    shots = []

    # 1) 主界面（列表 + 详情面板）
    row = None
    for item, e in app.item_entry.items():
        if e.name == "会议记录.txt":
            row = item
    if row:
        app.tree.selection_set(row)
    app.update_detail()
    for _ in range(12):
        root.update()
        time.sleep(0.05)
    (out_dir / "screenshot-main.png").write_bytes(
        bgra_to_png(capture_region(x, y, w, h), w, h))
    shots.append("screenshot-main.png")

    # 2) 悬停预览：鼠标停在文件上，放大预览就出现在右侧"文件详情"的图片区
    if row:
        app.preview_in_detail(row)
        for _ in range(12):
            root.update()
            time.sleep(0.05)
        (out_dir / "screenshot-hover.png").write_bytes(
            bgra_to_png(capture_region(x, y, w, h), w, h))
        shots.append("screenshot-hover.png")

    # 3) 单击详情里的图片 → 大图查看器
    if row:
        viewer = app.open_viewer()
        if viewer is not None:
            viewer.attributes("-topmost", True)
            viewer.lift()
            for _ in range(14):
                root.update()
                time.sleep(0.05)
            vhwnd = user32.FindWindowW(None, viewer.title())
            vrect = RECT()
            user32.GetWindowRect(vhwnd, ctypes.byref(vrect))
            vw = vrect.right - vrect.left
            vh = vrect.bottom - vrect.top
            if vw > 50 and vh > 50:
                (out_dir / "screenshot-viewer.png").write_bytes(
                    bgra_to_png(
                        capture_region(vrect.left, vrect.top, vw, vh), vw, vh))
                shots.append("screenshot-viewer.png")
            viewer.destroy()

    # 4) 窄窗口：看底部那一行（状态文字 + 多选/全选）在窄窗口下是否正常
    root.geometry("900x520+40+30")
    for _ in range(20):
        root.update()
        time.sleep(0.05)
    hwnd = user32.FindWindowW(None, fm.APP_TITLE)
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    x, y, w, h = rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    if row:
        app.preview_in_detail(row)
    # 顺便演示多选：勾上"多选"并选中三行
    app.multi_var.set(True)
    for i in range(min(3, len(app.visible))):
        app._selected_set.add(i)
    app._selected_index = 0
    app._remember_selection_names()
    app._sync_selection_item()
    app._update_sel_label()
    for _ in range(12):
        root.update()
        time.sleep(0.05)
    (out_dir / "screenshot-small.png").write_bytes(
        bgra_to_png(capture_region(x, y, w, h), w, h))
    shots.append("screenshot-small.png")
    app.multi_var.set(False)
    app._select_all(False)

    # 5) "最近管理的文件夹"下拉（连主窗口一起拍，方便看清位置）
    root.geometry("1200x620+40+30")
    for _ in range(14):
        root.update()
        time.sleep(0.05)
    app.show_recent_folders()
    for _ in range(12):
        root.update()
        time.sleep(0.05)
    pop = app._recent_popup
    if pop is not None:
        px, py = pop.winfo_rootx(), pop.winfo_rooty()
        pw, ph = pop.winfo_width(), pop.winfo_height()
        mx, my = root.winfo_rootx() - 8, root.winfo_rooty() - 31
        mw, mh = root.winfo_width() + 16, root.winfo_height() + 39
        ux0, uy0 = min(mx, px), min(my, py)
        ux1, uy1 = max(mx + mw, px + pw), max(my + mh, py + ph)
        (out_dir / "screenshot-recent.png").write_bytes(
            bgra_to_png(capture_region(ux0, uy0, ux1 - ux0, uy1 - uy0),
                        ux1 - ux0, uy1 - uy0))
        shots.append("screenshot-recent.png")
        app._close_recent()

    # 6) 安全设置（默认上锁 🔒 → 『格式化』灰掉不可点）
    app.show_safety()
    for _ in range(14):
        root.update()
        time.sleep(0.05)
    shwnd = user32.FindWindowW(None, "安全设置")
    srect = RECT()
    user32.GetWindowRect(shwnd, ctypes.byref(srect))
    sw, sh = srect.right - srect.left, srect.bottom - srect.top
    if sw > 50 and sh > 50:
        (out_dir / "screenshot-safety.png").write_bytes(
            bgra_to_png(capture_region(srect.left, srect.top, sw, sh), sw, sh))
        shots.append("screenshot-safety.png")
    app._close_safety()

    root.destroy()
    shutil.rmtree(work_data, ignore_errors=True)
    print("已生成：%s" % "、".join(shots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
