# -*- coding: utf-8 -*-
"""
简易图片标注文件管理器（只读安全模式）
=====================================

一个"最简方式"的 Windows 文件管理器：只管理你选定的那**一个文件夹**，
可以为其中每个文件配置一张图片（封面），鼠标悬停时就在右侧「文件详情」
的图片区域放大预览，单击该图片还能看大图。

界面要点：
  * 列表会显示每个文件的缩略图、大小、类型、修改时间、评分；
  * 文件夹的大小也会（在后台线程里）自动统计出来，不卡界面；
  * 悬停 = 预览；单击详情图片 = 看大图；右键 = 设置图片 / 评分 / 打开。

安全承诺（代码级约束，见 tests/test_file_manager.py 的验证）
-----------------------------------------------------------
本程序**绝不**对用户文件做下列事情：
  * 删除文件        * 移动文件        * 重命名文件
  * 解压压缩包      * 整理目录        * 修改文件内容
  * 覆盖文件        * 批量改变目录结构

程序只会：
  1) 以只读方式扫描文件夹、读取文件属性（os.scandir / os.stat）；
  2) 以只读方式打开图片做预览与缩略图；
  3) 把你选中的封面图片**复制一份**到程序自己的 data\\covers 目录（源文件只读、不动）；
  4) 把封面路径、评分、备注写进程序自己的 data\\library.json。

所有写操作都必须通过 guard_app_write() 检查目标位于程序自己的数据目录内，
否则直接抛 SafetyError。源码中不存在 os.remove / os.rename / shutil.move /
shutil.rmtree / Path.unlink 等任何删除移动类调用（单元测试会静态检查这一点）。

运行：双击 run.bat，或  python file_manager.py
可选：python file_manager.py --make-demo E:\\imagine\\demo   # 生成示例素材
      python file_manager.py --folder E:\\some\\dir          # 直接打开某文件夹
      python file_manager.py --smoke 2000                    # 冒烟测试，2 秒后自动退出

可选依赖：Pillow（用于 JPG/WebP/BMP/TIFF 预览）。未安装时 PNG/GIF 仍可预览。
"""

from __future__ import annotations

import argparse
import base64
import collections
import hashlib
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import zlib
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ---------------------------------------------------------------------------
# 0. 可选依赖：Pillow
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 取决于环境
    from PIL import Image, ImageOps, ImageTk

    HAS_PIL = True
    PIL_VERSION = getattr(Image, "__version__", "?")
except Exception:  # pragma: no cover
    Image = ImageOps = ImageTk = None  # type: ignore
    HAS_PIL = False
    PIL_VERSION = ""

# ---------------------------------------------------------------------------
# 1. 常量与路径
# ---------------------------------------------------------------------------
APP_NAME = "简易文件管理器"
#: 版本号：发新版本时改这里（会显示在窗口标题和「安全设置」里）
APP_VERSION = "1.0.0"

#: 运行模式。现在只有「只读模式」（已完成）；「修改模式」是预留接口，
#: 见 ensure_edit_mode() / guard_user_write()，以及文件末尾那几个编辑操作骨架。
MODE_READONLY = "readonly"
MODE_EDIT = "edit"
MODE_LABELS = {MODE_READONLY: "只读模式", MODE_EDIT: "修改模式"}
_mode = MODE_READONLY


def get_mode() -> str:
    return _mode


def set_mode(mode: str) -> None:
    """切换运行模式。

    「修改模式」尚未实现，现在只接受只读模式 —— 将来加编辑功能时，
    所有写用户文件的代码都必须先过 ensure_edit_mode()，没人能绕过闸门。
    """
    global _mode
    if mode not in MODE_LABELS:
        raise ValueError("未知模式：%r" % (mode,))
    if mode == MODE_EDIT:
        raise NotImplementedError(
            "修改模式尚未实现（当前版本 %s 只有只读模式）" % APP_VERSION)
    _mode = mode


def mode_label(mode: str = "") -> str:
    return MODE_LABELS.get(mode or get_mode(), mode or get_mode())


def window_title(mode: str = "") -> str:
    return "%s · 图片标注（%s · v%s）" % (APP_NAME, mode_label(mode), APP_VERSION)


APP_TITLE = window_title(MODE_READONLY)
if getattr(sys, "frozen", False):
    # 以后打包成 exe：数据目录要跟着 exe 走，而不是解包出来的临时目录
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = APP_DIR / "data"

#: 允许写入的根目录。默认是程序自己的 data 目录；测试中可临时替换。
_write_root: Path = DEFAULT_DATA_DIR

#: 右侧详情面板里那张"放大预览图"的画布尺寸（固定，切换文件时布局不跳动）
DETAIL_IMG_W, DETAIL_IMG_H = 316, 210
#: 文件夹大小统计的上限，避免超大目录卡住界面
SIZE_CAP_FILES = 150000
SIZE_CAP_SECONDS = 20.0
#: 文件夹大小结果存盘（data\cache 下），这样切回看过的文件夹是"秒显"
SIZE_CACHE_NAME = "folder_sizes.json"
SIZE_CACHE_MAX = 20000          # 最多记多少条，超出就丢掉最老的
SIZE_CACHE_TRUST_SECONDS = 300  # 5 分钟内算过的结果直接信任，不重复校验
SIZE_MAX_WORKERS = 2            # 同时最多几个线程在读盘（太多会把磁盘和界面拖垮）
THUMB_CACHE_MAX = 800           # 列表缩略图内存缓存条数（约 800×7.7KB ≈ 6 MB）
RECENT_FOLDERS_MAX = 12         # "最近管理的文件夹"最多记几个
ROW_HEIGHT = 52                 # 与 Treeview 的 rowheight 一致（窗口化列表按它算可见行数）
ROW_POOL_EXTRA = 4              # 可见行之外多缓存几行，滚动时不闪
WHEEL_ROWS = 3                  # 滚轮每格滚动几行
#: 右侧详情面板宽度（状态栏算"多选/全选"位置时也要用，保持一致）
DETAIL_PANEL_W = 372
#: 底部"多选/全选"再往左留一点空隙，让它停在列表滚动条左侧、不贴边
SEL_BAR_RIGHT_GAP = 38
#: 文件身份缓存（data\cache\file_ids.json）：取一次 ID 要多开一次句柄，所以必须缓存
ID_CACHE_NAME = "file_ids.json"
ID_CACHE_MAX = 60000

IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".ico", ".avif", ".heic",
}
#: 不装 Pillow 时 Tk 8.6 能直接显示的格式
TK_NATIVE_EXTS = {".png", ".gif", ".ppm", ".pgm"}
DOC_EXTS = {".txt", ".md", ".rtf", ".doc", ".docx", ".pdf", ".xls", ".xlsx",
            ".ppt", ".pptx", ".csv", ".log", ".ini", ".json", ".xml", ".yaml", ".yml"}
ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso", ".cab"}
MEDIA_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".ogg", ".mp4", ".mkv",
              ".avi", ".mov", ".wmv", ".webm", ".flv", ".ts"}
CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".h", ".cpp", ".hpp",
             ".cs", ".java", ".go", ".rs", ".rb", ".php", ".sh", ".ps1", ".bat",
             ".cmd", ".html", ".css", ".sql", ".vue", ".kt", ".swift"}

TYPE_LABEL = {
    "folder": "文件夹", "image": "图片", "doc": "文档", "archive": "压缩包",
    "media": "音视频", "code": "代码", "file": "其他文件",
}


def set_write_root(path) -> None:
    """仅供测试使用：把允许写入的根目录指向别处。"""
    global _write_root
    _write_root = Path(path).resolve()


def get_write_root() -> Path:
    return _write_root


def data_dir() -> Path:
    return get_write_root()


def covers_dir() -> Path:
    return get_write_root() / "covers"


def cache_dir() -> Path:
    return get_write_root() / "cache"


def library_file() -> Path:
    return get_write_root() / "library.json"


def config_file() -> Path:
    return get_write_root() / "config.json"


# ---------------------------------------------------------------------------
# 2. 安全写入守卫
# ---------------------------------------------------------------------------
class SafetyError(RuntimeError):
    """试图写入程序数据目录以外的位置（例如用户文件夹）时抛出。"""


class EditModeUnavailable(SafetyError):
    """只读模式下有人试图改动用户文件时抛出 —— 这是「修改模式」的总闸门。"""


def ensure_edit_mode(action: str) -> None:
    """【修改模式的唯一闸门】只读模式下，任何改动用户文件的操作都在这里被挡下。

    将来实现修改模式时只需要三步：
      1) 让 set_mode(MODE_EDIT) 可用，并在界面上明显标出"现在是修改模式"；
      2) 每个编辑动作动手前先调用 ensure_edit_mode("重命名文件")；
      3) 通过 guard_user_write() 写用户文件，并调用 record_operation() 留痕。
    """
    if get_mode() != MODE_EDIT:
        raise EditModeUnavailable(
            "当前是%s，不会%s。\n（改动用户文件属于预留的「修改模式」，本版本不提供。）"
            % (mode_label(), action))


def guard_user_write(path, action: str = "改动文件") -> Path:
    """【修改模式下写用户文件的唯一入口】只读模式下一律抛 EditModeUnavailable。

    这样以后即使加了编辑功能，写用户文件的路径也全部集中在这一处，便于审计与留痕。
    """
    ensure_edit_mode(action)
    return Path(path)


def record_operation(action: str, detail: dict) -> None:
    """【预留】把一次修改操作追加到 data\\oplog.jsonl，便于以后做撤销 / 追责。

    现在没有代码会走到这里（只读模式下闸门先抛错），先把接口和落盘位置定下来。
    """
    path = get_write_root() / "oplog.jsonl"
    guard_app_write(path)
    ensure_data_dirs()
    line = json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "action": action, "detail": detail}, ensure_ascii=False)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def _is_inside(path: Path, base: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except Exception:
        return False


def guard_app_write(path) -> Path:
    """任何写操作的唯一入口：目标必须位于程序自己的数据目录内。"""
    p = Path(path)
    if not _is_inside(p, get_write_root()):
        raise SafetyError(
            "安全守卫已阻止写入程序数据目录以外的路径：%s\n"
            "本程序不会改动你的文件。" % p
        )
    return p


def ensure_data_dirs() -> None:
    for d in (data_dir(), covers_dir(), cache_dir()):
        guard_app_write(d / ".keep")
        d.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, text: str) -> None:
    """在程序数据目录内原子写文件（先写临时文件，再 os.replace）。"""
    guard_app_write(path)
    ensure_data_dirs()
    tmp = path.with_name(path.name + ".tmp")
    guard_app_write(tmp)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)  # 仅在程序数据目录内部替换自己的文件


def format_app_data() -> int:
    """"格式化"：清空程序自己的 data 目录（等于恢复出厂设置）。

    **只删 data 目录里的东西**：标注、封面副本、缓存、配置。
    每一步都过 guard_app_write，路径只要不在 data 目录内就会抛错，
    所以它没有任何机会碰到你的文件。返回删除的文件数。
    整个程序里只有这一个函数会做删除动作（测试里有静态审计盯着）。
    """
    root = get_write_root()
    if not root.is_dir():
        return 0
    removed = 0
    victims = sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    for path in victims:
        guard_app_write(path)          # 双保险：不在 data 目录内直接抛错
        try:
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# ---------------------------------------------------------------------------
# 3. 配置与标注库（JSON，均存放在程序自己的 data 目录）
# ---------------------------------------------------------------------------
def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, type(default)) else default
    except FileNotFoundError:
        return default
    except Exception:
        # 索引损坏时留一份备份，绝不静默丢弃
        try:
            guard_app_write(path)
            backup = path.with_name(path.name + ".bad-%d" % int(time.time()))
            shutil.copyfile(path, backup)
        except Exception:
            pass
        return default


def load_config() -> dict:
    return load_json(config_file(), {})


def save_config(cfg: dict) -> None:
    atomic_write_text(config_file(), json.dumps(cfg, ensure_ascii=False, indent=2))


def norm_root(folder) -> str:
    return os.path.normcase(str(Path(folder).resolve()))


def norm_rel(rel) -> str:
    return str(rel).replace("\\", "/").lower()


class Library:
    """按"文件夹 → 相对路径 → 标注"保存封面、评分、备注。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else library_file()
        self._data = {"version": 1, "folders": {}}
        self._root_keys: dict = {}      # 缓存"文件夹 → 规范键"，避免每次都 resolve
        self._id_index: dict = {}       # 文件夹键 → {文件ID: 记录键}
        self.load()

    def load(self) -> None:
        obj = load_json(self.path, {"version": 1, "folders": {}})
        if isinstance(obj, dict) and isinstance(obj.get("folders"), dict):
            self._data = obj

    def save(self) -> None:
        atomic_write_text(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))

    def _root_key(self, root) -> str:
        key = self._root_keys.get(str(root))
        if key is None:
            key = norm_root(root)
            self._root_keys[str(root)] = key
        return key

    def folder(self, root) -> dict:
        return self._data.setdefault("folders", {}).setdefault(self._root_key(root), {})

    def _ids_for(self, root_key: str) -> dict:
        """文件夹内"文件ID → 记录键"的索引（懒建，写入时失效）。"""
        idx = self._id_index.get(root_key)
        if idx is None:
            idx = {}
            for name_key, rec in self._data.get("folders", {}).get(root_key, {}).items():
                ident = rec.get("id") if isinstance(rec, dict) else None
                if ident:
                    idx[ident] = name_key
            self._id_index[root_key] = idx
        return idx

    def _invalidate_ids(self, root_key: str) -> None:
        self._id_index.pop(root_key, None)

    def entry(self, root, rel, ident: str = "") -> dict:
        """取某个文件的标注：先按名字，再按文件ID（这样改过名的文件也认得出来）。"""
        key = self._root_key(root)
        folder = self._data.get("folders", {}).get(key) or {}
        rec = folder.get(norm_rel(rel))
        if isinstance(rec, dict):
            return rec
        if ident:
            name_key = self._ids_for(key).get(ident)
            if name_key is not None:
                rec = folder.get(name_key)
                if isinstance(rec, dict):
                    return rec
        return {}

    def update(self, root, rel, ident: str = "", **fields) -> dict:
        key = self._root_key(root)
        folder = self._data.setdefault("folders", {}).setdefault(key, {})
        name_key = norm_rel(rel)
        if name_key not in folder and ident:
            alt = self._ids_for(key).get(ident)
            if alt:
                name_key = alt
        e = folder.setdefault(name_key, {})
        for k, v in fields.items():
            if v in (None, "", 0):
                e.pop(k, None)
            else:
                e[k] = v
        if ident:
            e["id"] = ident
        if not e:
            folder.pop(name_key, None)
        self._invalidate_ids(key)
        self.save()
        return e

    def update_many(self, root, items, **fields) -> int:
        """一次给多个文件写同样的标注（比如"全选后评星"），只落盘一次。"""
        key = self._root_key(root)
        folder = self._data.setdefault("folders", {}).setdefault(key, {})
        ids = self._ids_for(key)
        n = 0
        for rel, ident in items:
            name_key = norm_rel(rel)
            if name_key not in folder and ident:
                alt = ids.get(ident)
                if alt:
                    name_key = alt
            e = folder.setdefault(name_key, {})
            for k, v in fields.items():
                if v in (None, "", 0):
                    e.pop(k, None)
                else:
                    e[k] = v
            if ident:
                e["id"] = ident
            if not e:
                folder.pop(name_key, None)
            n += 1
        self._invalidate_ids(key)
        self.save()
        return n

    def reconcile(self, root, pairs) -> int:
        """按文件ID把"改过名/移过位"的标注挪到新名字下，并补记 ID。

        pairs 是 [(文件名, 文件ID)]。返回改动条数。
        """
        key = self._root_key(root)
        folder = self._data.setdefault("folders", {}).setdefault(key, {})
        if not folder:
            return 0
        ids = self._ids_for(key)
        changed = 0
        claimed = set()
        for rel, ident in pairs:
            if not ident or ident in claimed:
                continue
            claimed.add(ident)
            name_key = norm_rel(rel)
            rec = folder.get(name_key)
            if isinstance(rec, dict):
                if rec.get("id") != ident:       # 老记录补上 ID（一次性）
                    rec["id"] = ident
                    changed += 1
                continue
            old_key = ids.get(ident)
            if old_key is not None and old_key != name_key:
                rec = folder.pop(old_key, None)   # 文件改了名 → 把记录搬到新名字下
                if isinstance(rec, dict):
                    folder[name_key] = rec
                    changed += 1
        if changed:
            self._invalidate_ids(key)
            self.save()
        return changed

    # -- 封面 ---------------------------------------------------------------
    def cover_path(self, lib_entry: dict):
        """返回封面图片在本程序数据目录中的绝对路径（只读用途）。"""
        rel = lib_entry.get("cover")
        if not rel:
            return None
        p = (get_write_root() / rel).resolve()
        if not _is_inside(p, get_write_root()):
            return None
        return p if p.is_file() else None

    def import_cover(self, source: Path, key: str) -> str:
        """把用户选中的图片**复制**进 data\\covers，返回相对路径。

        只读打开源文件；目标名由 key 哈希决定，重复设置会覆盖程序自己
        的那一份副本，绝不触碰源文件。
        """
        source = Path(source)
        if not source.is_file():
            raise SafetyError("找不到图片文件：%s" % source)
        _validate_image(source)
        ext = source.suffix.lower() or ".png"
        name = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ext
        ensure_data_dirs()
        dest = guard_app_write(covers_dir() / name)
        if source.resolve() != dest.resolve():
            with open(source, "rb") as fsrc, open(dest, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst, 1024 * 256)
        return "covers/" + name


def _validate_image(path: Path) -> None:
    """确认所选文件确实是一张图片；只读检查。"""
    if HAS_PIL:
        try:
            with Image.open(path) as im:
                im.verify()
        except Exception as exc:
            raise ValueError("这不是一张可识别的图片（%s）" % exc)
        return
    ext = path.suffix.lower()
    if ext not in TK_NATIVE_EXTS:
        raise ValueError(
            "未安装 Pillow，只能使用 PNG / GIF 图片。\n"
            "如需 JPG、WebP、BMP 等格式，请双击『安装可选依赖-Pillow.bat』后重试。"
        )
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"\x89PNG\r\n\x1a\n") or head[:3] == b"GIF":
        return
    raise ValueError("这不是一张可识别的 PNG / GIF 图片。")


# ---------------------------------------------------------------------------
# 4. 纯标准库 PNG 生成（不依赖 Pillow 也能画图标）
# ---------------------------------------------------------------------------
class Pixmap:
    """极小的 RGBA 画布：3 倍超采样后缩小，得到平滑图标。"""

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.buf = bytearray(w * h * 4)

    def blend(self, x: int, y: int, color, alpha: float = 1.0) -> None:
        if x < 0 or y < 0 or x >= self.w or y >= self.h or alpha <= 0:
            return
        i = (y * self.w + x) * 4
        r, g, b = color[0], color[1], color[2]
        if len(color) > 3:
            alpha *= color[3] / 255.0
        sa = self.buf[i + 3] / 255.0
        na = alpha + sa * (1 - alpha)
        if na <= 0:
            self.buf[i:i + 4] = b"\x00\x00\x00\x00"
            return
        for k, c in enumerate((r, g, b)):
            old = self.buf[i + k]
            self.buf[i + k] = int((c * alpha + old * sa * (1 - alpha)) / na + 0.5)
        self.buf[i + 3] = int(na * 255 + 0.5)

    def rect(self, x0, y0, x1, y1, color, radius: float = 0) -> None:
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
        for y in range(max(0, y0), min(self.h, y1)):
            for x in range(max(0, x0), min(self.w, x1)):
                if radius:
                    cx = min(max(x + 0.5, x0 + radius), x1 - radius)
                    cy = min(max(y + 0.5, y0 + radius), y1 - radius)
                    if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 > radius * radius:
                        continue
                self.blend(x, y, color)

    def disc(self, cx, cy, r, color) -> None:
        for y in range(int(cy - r), int(cy + r) + 1):
            for x in range(int(cx - r), int(cx + r) + 1):
                if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= r * r:
                    self.blend(x, y, color)

    def tri(self, p1, p2, p3, color) -> None:
        xs = (p1[0], p2[0], p3[0])
        ys = (p1[1], p2[1], p3[1])

        def edge(a, b, p):
            return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])

        for y in range(int(min(ys)), int(max(ys)) + 1):
            for x in range(int(min(xs)), int(max(xs)) + 1):
                p = (x + 0.5, y + 0.5)
                w0, w1, w2 = edge(p1, p2, p), edge(p2, p3, p), edge(p3, p1, p)
                if (w0 >= 0 and w1 >= 0 and w2 >= 0) or (w0 <= 0 and w1 <= 0 and w2 <= 0):
                    self.blend(x, y, color)

    def vgradient(self, x0, y0, x1, y1, top, bottom) -> None:
        height = max(1, y1 - y0)
        for y in range(int(y0), int(y1)):
            t = (y - y0) / height
            color = tuple(int(top[k] + (bottom[k] - top[k]) * t) for k in range(3))
            self.rect(x0, y, x1, y + 1, color)

    def downscale(self, factor: int) -> "Pixmap":
        out = Pixmap(self.w // factor, self.h // factor)
        n = factor * factor
        for y in range(out.h):
            for x in range(out.w):
                r = g = b = a = 0
                for dy in range(factor):
                    row = (y * factor + dy) * self.w
                    for dx in range(factor):
                        i = (row + x * factor + dx) * 4
                        r += self.buf[i]
                        g += self.buf[i + 1]
                        b += self.buf[i + 2]
                        a += self.buf[i + 3]
                j = (y * out.w + x) * 4
                out.buf[j] = r // n
                out.buf[j + 1] = g // n
                out.buf[j + 2] = b // n
                out.buf[j + 3] = a // n
        return out


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_bytes(pm: Pixmap) -> bytes:
    stride = pm.w * 4
    raw = bytearray()
    for y in range(pm.h):
        raw.append(0)
        raw += pm.buf[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", pm.w, pm.h, 8, 6, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _png_chunk(b"IEND", b""))


_ICON_PNG_CACHE: dict = {}
_ICON_PHOTO_CACHE: dict = {}


def reset_photo_cache() -> None:
    """PhotoImage 绑定在创建它的 Tk 解释器上；新的 root 会让旧对象失效。"""
    _ICON_PHOTO_CACHE.clear()


def _cached_photo(key):
    ph = _ICON_PHOTO_CACHE.get(key)
    if ph is None:
        return None
    try:
        ph.width()  # 触碰一下；已失效的图片会抛 TclError
        return ph
    except Exception:
        _ICON_PHOTO_CACHE.pop(key, None)
        return None


def _draw_icon(kind: str, size: int) -> bytes:
    S = 3
    W = size * S
    pm = Pixmap(W, W)
    m = int(W * 0.08)
    r = int(W * 0.16)
    if kind == "folder":
        pm.rect(m, int(W * 0.22), int(W * 0.62), int(W * 0.40), (214, 150, 58), int(W * 0.07))
        pm.rect(m, int(W * 0.28), W - m, W - m, (240, 176, 74), r)
        pm.rect(m, int(W * 0.36), W - m, W - m, (249, 199, 112), r)
    elif kind == "image":
        pm.rect(m, m, W - m, W - m, (74, 144, 217), r)
        pm.tri((W * 0.16, W * 0.80), (W * 0.44, W * 0.38), (W * 0.70, W * 0.80), (255, 255, 255))
        pm.tri((W * 0.48, W * 0.80), (W * 0.68, W * 0.52), (W * 0.88, W * 0.80), (208, 229, 250))
        pm.disc(W * 0.70, W * 0.30, W * 0.09, (255, 209, 102))
    elif kind == "doc":
        pm.rect(m, m, W - m, W - m, (170, 178, 188), int(W * 0.12))
        pm.rect(m + 2 * S, m + 2 * S, W - m - 2 * S, W - m - 2 * S, (252, 252, 253), int(W * 0.11))
        for i in range(4):
            y = int(W * (0.30 + i * 0.13))
            wide = 0.68 if i == 3 else 0.82
            pm.rect(W * 0.26, y, W * (0.26 + wide * 0.48), y + int(W * 0.05), (176, 184, 196))
    elif kind == "archive":
        pm.rect(m, m, W - m, W - m, (186, 133, 62), r)
        pm.rect(m, int(W * 0.42), W - m, int(W * 0.55), (214, 168, 100))
        pm.rect(W * 0.44, int(W * 0.20), W * 0.58, int(W * 0.40), (140, 96, 40), int(W * 0.03))
        pm.disc(W * 0.51, W * 0.66, W * 0.07, (140, 96, 40))
    elif kind == "media":
        pm.rect(m, m, W - m, W - m, (123, 94, 167), r)
        pm.tri((W * 0.38, W * 0.28), (W * 0.38, W * 0.72), (W * 0.74, W * 0.50), (255, 255, 255))
    elif kind == "code":
        pm.rect(m, m, W - m, W - m, (60, 126, 110), r)
        pm.tri((W * 0.42, W * 0.30), (W * 0.42, W * 0.70), (W * 0.20, W * 0.50), (255, 255, 255))
        pm.tri((W * 0.58, W * 0.30), (W * 0.58, W * 0.70), (W * 0.80, W * 0.50), (255, 255, 255))
    else:
        pm.rect(m, m, W - m, W - m, (146, 152, 160), r)
        pm.rect(W * 0.32, W * 0.32, W * 0.68, W * 0.68, (240, 242, 245), int(W * 0.06))
    return png_bytes(pm.downscale(S))


def icon_photo(kind: str, size: int = 44) -> tk.PhotoImage:
    """取一个图标；大图标用已有小图标放大（Tk 原生 zoom），

    纯 Python 逐像素画一个 116px 图标要 0.9 秒，所以超过 64px 就改成
    "画一半大小 + 放大 2 倍"，视觉上没差别，速度却快 4 倍以上。
    """
    key = (kind, size)
    ph = _cached_photo(key)
    if ph is not None:
        return ph
    if size > 64:
        small, factor = size, 1
        while small > 64:
            small //= 2
            factor *= 2
        base = icon_photo(kind, small)
        try:
            ph = base.zoom(factor, factor)
        except Exception:
            ph = base
    else:
        if key not in _ICON_PNG_CACHE:
            _ICON_PNG_CACHE[key] = _draw_icon(kind, size)
        ph = tk.PhotoImage(data=base64.b64encode(_ICON_PNG_CACHE[key]).decode("ascii"))
    _ICON_PHOTO_CACHE[key] = ph
    return ph


# ---------------------------------------------------------------------------
# 5. 文件扫描（纯只读）
# ---------------------------------------------------------------------------
@dataclass
class Entry:
    name: str
    path: Path
    is_dir: bool
    size: int
    mtime: float
    ident: str = ""      # 卷序列号:文件ID（拿不到就是空串，退回按名字）

    @property
    def ext(self) -> str:
        return "" if self.is_dir else self.path.suffix.lower()

    @property
    def kind(self) -> str:
        if self.is_dir:
            return "folder"
        e = self.ext
        if e in IMAGE_EXTS:
            return "image"
        if e in DOC_EXTS:
            return "doc"
        if e in ARCHIVE_EXTS:
            return "archive"
        if e in MEDIA_EXTS:
            return "media"
        if e in CODE_EXTS:
            return "code"
        return "file"

    @property
    def rel(self) -> str:
        return self.name


def _same_path(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except Exception:
        return False


def scan_folder(folder, skip: Path | None = None) -> list:
    """只读列出文件夹的直接子项；skip 目录（程序自己的 data）会被隐藏。

    注意：这里绝不能用 Path.resolve() 去逐个比对 —— 那会对每个文件都发一次
    系统调用（2 万个文件就要 8 秒）。先比名字，名字相同才做完整路径比较。
    """
    folder = Path(folder)
    skip_name = skip.name.lower() if skip is not None else None
    skip_key = os.path.normcase(os.path.abspath(str(skip))) if skip is not None else None
    out: list = []
    try:
        with os.scandir(folder) as it:
            for de in it:
                if skip_name is not None and de.name.lower() == skip_name:
                    if skip_key is not None and \
                            os.path.normcase(os.path.abspath(de.path)) == skip_key:
                        continue
                try:
                    st = de.stat(follow_symlinks=False)
                    is_dir = de.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                out.append(Entry(
                    name=de.name,
                    path=Path(de.path),
                    is_dir=is_dir,
                    size=0 if is_dir else int(st.st_size),
                    mtime=float(st.st_mtime),
                ))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []
    except OSError:
        return []
    return out


def file_ident(path) -> str:
    """取"卷序列号:文件ID"作为文件身份（NTFS 上是 128 位，且改名字、移动都不会变）。

    拿不到（网络盘 / FAT32 / 某些虚拟盘）就返回空串，调用方会退回按名字匹配。
    注意：这需要 os.stat()（比 DirEntry.stat 多开一次句柄），所以调用方一定要缓存。
    """
    try:
        st = os.stat(path)
    except OSError:
        return ""
    ino = getattr(st, "st_ino", 0) or 0
    dev = getattr(st, "st_dev", 0) or 0
    if not ino or not dev:
        return ""
    return "%d:%d" % (dev, ino)


_OPEN_ERROR_HINTS = {
    0xC0000142: "系统不允许它启动资源管理器（0xC0000142：子进程 DLL 初始化失败）",
    5: "系统拒绝了这次打开操作（错误码 5：拒绝访问）",
}


def _open_folder_via_shell(path) -> bool:
    """请"已经在运行的那个资源管理器"打开文件夹（不新建进程）。

    有些受限环境不允许本进程启动新程序，但愿意把请求转交给 shell，
    所以这是 os.startfile 之外的一条备用路。
    """
    try:
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        shell32.SHParseDisplayName.restype = ctypes.c_long
        shell32.SHParseDisplayName.argtypes = [
            ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_ulong, ctypes.c_void_p]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitialize(None)
        pidl = ctypes.c_void_p()
        if shell32.SHParseDisplayName(str(path), None, ctypes.byref(pidl), 0,
                                      None) != 0 or not pidl:
            return False
        hr = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0)
        ole32.CoTaskMemFree(pidl)
        return hr == 0
    except Exception:
        return False


def shell_open(path) -> str:
    """用系统默认方式打开文件/文件夹，返回实际用到的方式；全都失败就抛异常。

    为什么要这么绕：打包成 exe 后，在受限环境里（沙箱、安全软件策略等）
    ShellExecute 可能"拒绝访问"，而且这种失败有时**不抛异常却也打不开**。
    所以这里按"最可靠 → 次可靠"依次尝试，并且**真的核实结果**，
    绝不谎报成功（曾经出现状态栏写"已打开"、其实什么都没发生）。
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError("找不到：%s" % target)
    problems = []

    # 1) 交给系统 shell（正常情况下这一步就够了）
    try:
        os.startfile(str(target))            # noqa: S606 - 用户主动触发的打开
        return "startfile"
    except OSError as exc:
        problems.append("ShellExecute：%s" % exc)

    # 2) 文件夹：请已经在运行的资源管理器来开（不新建进程）
    if target.is_dir() and _open_folder_via_shell(target):
        return "shell-api"

    # 3) 自己拉起资源管理器 / 让 cmd 转交一次，并核实它有没有起来
    cmd = (["explorer.exe", str(target)] if target.is_dir()
           else ["cmd.exe", "/c", "start", "", str(target)])
    try:
        proc = subprocess.Popen(cmd)
    except OSError as exc:
        problems.append("启动 %s：%s" % (cmd[0], exc))
        raise OSError("；".join(problems))
    try:
        code = proc.wait(timeout=1.5)        # 正常交接会立刻退出（退出码 0 或 1）
    except subprocess.TimeoutExpired:
        return "explorer"                    # 还在跑，说明它确实去开窗口了
    except Exception:
        return "explorer"
    if code in (0, 1):
        return "explorer"
    hint = _OPEN_ERROR_HINTS.get(code & 0xFFFFFFFF, "错误码 0x%08X" % (code & 0xFFFFFFFF))
    problems.append("%s：%s" % (cmd[0], hint))
    raise OSError("；".join(problems))


def human_size(n: int) -> str:
    if n <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    i = min(int(math.log(n, 1024)), len(units) - 1)
    v = n / (1024 ** i)
    return ("%d B" % n) if i == 0 else ("%.1f %s" % (v, units[i]))


def human_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def natural_key(name: str):
    """自然排序键：数字在前、且按数值比较（1 < 2 < 10，不是 1 < 10 < 2）。

    和资源管理器一致：纯数字名字排在字母名字前面。
    """
    parts = re.split(r"(\d+)", str(name).lower())
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts if p != ""]


def unique_path(path: Path) -> Path:
    """找一个还不存在的文件名（绝不覆盖已有文件）。"""
    path = Path(path)
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name("%s-%d%s" % (path.stem, i, path.suffix))
        if not cand.exists():
            return cand
    return path.with_name("%s-%d%s" % (path.stem, int(time.time()), path.suffix))


def display_width(text) -> int:
    """文本的"显示宽度"：中文/全角算 2 格，其余算 1 格（对齐表格要用）。"""
    width = 0
    for ch in str(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def format_aligned_table(header, rows, gap: int = 2) -> str:
    """排成"列对齐"的纯文本：每列按最宽的内容用空格补齐。

    用空格而不是制表符 —— 制表符遇到中文（宽度 2）就对不齐了。
    """
    header = [str(h) for h in header]
    cols = len(header)
    widths = [display_width(h) for h in header]
    for row in rows:
        for i in range(cols):
            widths[i] = max(widths[i], display_width(row[i]))
    lines = []
    for row in [tuple(header)] + [tuple(str(c) for c in r) for r in rows]:
        cells = []
        for i, cell in enumerate(row):
            pad = widths[i] - display_width(cell)
            cells.append(cell + " " * (pad + (gap if i < cols - 1 else 0)))
        lines.append("".join(cells).rstrip())
    return "\r\n".join(lines) + "\r\n"


def export_file_list(path, rows) -> Path:
    """把清单写成 txt。

    rows 是 [(名称, 类型, 大小, 评分)]，会按列对齐（记事本里表头和数据也齐）。
    用 "x" 模式**新建**文件：文件已存在就会抛 FileExistsError ——
    也就是说它永远不会覆盖任何已有文件。
    """
    text = format_aligned_table(("命名", "类型", "大小", "评分"), rows)
    target = Path(path)
    with open(target, "x", encoding="utf-8-sig", newline="") as f:
        f.write(text)
    return target


def stars_text(rating: int) -> str:
    rating = max(0, min(5, int(rating or 0)))
    return ("★" * rating + "☆" * (5 - rating)) if rating else "—"


# ---------------------------------------------------------------------------
# 6. 图片加载与缩略图（只读；缩略图缓存写在 data\\cache）
# ---------------------------------------------------------------------------
def load_photo(src: Path | None, box) -> tuple:
    """按 box 缩放加载图片，返回 (PhotoImage|None, 状态)。

    box 可以是整数（正方形）或 (宽, 高)。状态：ok / missing / need_pil / error。
    """
    if src is None:
        return None, "missing"
    src = Path(src)
    if not src.is_file():
        return None, "missing"
    if isinstance(box, (tuple, list)):
        bw, bh = int(box[0]), int(box[1])
    else:
        bw = bh = int(box)
    bw, bh = max(1, bw), max(1, bh)
    ext = src.suffix.lower()
    if HAS_PIL:
        try:
            st = src.stat()
            key = hashlib.sha1(
                ("%s|%d|%d|%dx%d" % (str(src).lower(), int(st.st_mtime),
                                     st.st_size, bw, bh)).encode("utf-8")).hexdigest()
            cached = cache_dir() / (key + ".png")
            if cached.is_file():
                try:
                    return tk.PhotoImage(file=str(cached)), "ok"
                except Exception:
                    pass
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im) or im
                im = im.convert("RGBA")
                im.thumbnail((bw, bh), Image.LANCZOS)
                try:
                    ensure_data_dirs()
                    guard_app_write(cached)
                    im.save(cached, "PNG")
                except Exception:
                    pass
                return ImageTk.PhotoImage(im), "ok"
        except Exception:
            return None, "error"
    if ext in TK_NATIVE_EXTS:
        try:
            ph = tk.PhotoImage(file=str(src))
            factor = max(1, math.ceil(max(ph.width() / float(bw),
                                          ph.height() / float(bh))))
            if factor > 1:
                ph = ph.subsample(factor, factor)
            return ph, "ok"
        except Exception:
            return None, "error"
    return None, "need_pil"


# ---------------------------------------------------------------------------
# 7. 文件夹大小统计（纯只读；在工作线程里跑，不卡界面）
# ---------------------------------------------------------------------------
def measure_folder(path, cap_files: int = SIZE_CAP_FILES,
                   cap_seconds: float = SIZE_CAP_SECONDS) -> tuple:
    """递归统计文件夹总大小，返回 (字节数, 是否被上限截断)。

    只读：只做 scandir/stat。不跟随符号链接与目录联接，避免出现环。
    """
    total = 0
    count = 0
    truncated = False
    deadline = time.monotonic() + cap_seconds
    stack = [Path(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for de in it:
                    try:
                        if de.is_dir(follow_symlinks=False):
                            stack.append(Path(de.path))
                        elif de.is_file(follow_symlinks=False):
                            total += de.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
        if count >= cap_files or time.monotonic() > deadline:
            truncated = True
            break
    return total, truncated


# ---------------------------------------------------------------------------
# 8. 主窗口
# ---------------------------------------------------------------------------
class App:
    PREVIEW_DELAY_MS = 150      # 悬停多久后把放大预览切到该文件
    AUTO_REFRESH_MS = 5000

    def __init__(self, root: tk.Tk, folder=None, smoke_ms: int = 0, reveal: bool = True):
        self.root = root
        try:
            root.title(APP_TITLE)
            root.configure(background="#F0F0F0")   # 避免启动瞬间闪出黑底
        except Exception:
            pass
        reset_photo_cache()  # 新的 Tk 解释器：清掉上一轮失效的图片对象
        self.lib = Library()
        self.cfg = load_config()
        self.folder: Path | None = None
        self.entries: list = []
        self.visible: list = []
        self.item_entry: dict = {}
        # 窗口化列表：树上只保留固定几十行，滚动时复用它们改内容（外观与原来完全一致）
        self._row_ids: list = []
        self._offset = 0                 # 当前第一行对应 self.visible 的下标
        self._selected_index = None      # 逻辑选中项（滚远了也还记得）
        self._selected_set: set = set()  # 多选：下标集合
        self._selected_names: set = set()  # 同上，但按名字记住，刷新/切换后能还原
        self._sel_guard = False          # 程序改选中时抑制 <<TreeviewSelect>>
        self._pool_job = None
        self._last_tree_height = -1
        self._pool_rebuilds = 0          # 行池真正重建过几次（测试用）
        self._detail_entry: Entry | None = None
        self._hover_job = None
        self._hover_item = None
        self._auto_job = None
        self._size_job = None
        self._size_queue: queue.Queue = queue.Queue()
        self._size_pending: set = set()
        self._size_verified: set = set()   # 本次会话里真正校验过的文件夹
        self._size_wanted: collections.deque = collections.deque()   # 排队等线程的文件夹
        self._size_active = 0                                       # 正在跑的工作线程数
        self._size_lock = threading.Lock()
        self._sizes_enabled = False        # 界面画完之前先不读盘，避免启动时黑屏
        self._size_saved_at = 0.0
        self._thumb_cache: dict = {}       # 缩略图内存缓存，避免每次刷新重新解码
        # 文件身份（卷序列号:文件ID）缓存与后台取号：改名/移动后标注靠它跟随
        self._id_cache: dict = {}          # 规范化路径 -> (size, mtime, ident, 时间)
        self._id_queue: queue.Queue = queue.Queue()
        self._id_pending: set = set()
        self._id_wanted: collections.deque = collections.deque()
        self._id_active = False
        self._id_job = None
        self._id_dirty = False
        self._by_path: dict = {}           # 路径 -> Entry（收结果时用）
        self._load_id_cache()
        # path -> (bytes, truncated, 文件夹修改时间, 统计时刻)；从磁盘缓存恢复
        self._size_cache: dict = {}
        self._load_size_cache()
        self._sig = None
        self.sort_key = self.cfg.get("sort_key", "name")
        self.sort_rev = bool(self.cfg.get("sort_rev", False))
        self.search_var = tk.StringVar()
        self.auto_var = tk.BooleanVar(value=bool(self.cfg.get("auto_refresh", True)))
        self.status_var = tk.StringVar(value="就绪")
        self._revealed = False
        self._closing = False

        # 先隐藏窗口，等界面搭好、尺寸定好再显示 —— 这样就不会先黑一下
        if reveal:
            try:
                root.withdraw()
            except Exception:
                pass

        self._build_ui()
        self._build_menu()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<F5>", lambda _e: self.refresh())

        start = folder or self.cfg.get("last_folder")
        if start and Path(start).is_dir():
            self.set_folder(start)
        else:
            self.show_empty_hint()
        self._schedule_auto()
        self._size_job = self.root.after(150, self._drain_sizes)
        self._ensure_id_drain()
        if not reveal:
            self._sizes_enabled = True     # 不显示窗口的场景（测试/截图）直接允许统计

        if reveal:
            self.root.after_idle(self.reveal)
        if smoke_ms:
            self.root.after(smoke_ms, self.on_close)

    def reveal(self) -> None:
        """把已经画好的窗口显示出来，并等它真正画完再开始读盘。"""
        if self._revealed:
            return
        self._revealed = True
        try:
            self.root.update_idletasks()
            self.root.deiconify()
            self.root.lift()
            self.root.update()            # 强制立刻完成首帧绘制，避免出现黑窗口
        except Exception:
            pass
        try:
            self.root.after(300, self._enable_folder_sizes)
        except Exception:
            self._sizes_enabled = True

    def _enable_folder_sizes(self) -> None:
        """界面已经画在屏幕上了，这才开始后台统计文件夹大小。"""
        self._sizes_enabled = True
        self._pump_size_workers()
        self._ensure_size_drain()

    def _apply_mode_ui(self) -> None:
        """工具栏标签、窗口标题都由"当前模式"驱动。

        以后加「修改模式」时，只要 set_mode(MODE_EDIT) 生效，这里的文字和颜色
        会自动跟着变（再加上各编辑入口的 ensure_edit_mode 闸门即可）。
        """
        readonly = get_mode() == MODE_READONLY
        text = ("🔒 只读模式：不改动你的文件" if readonly
                else "✏️ 修改模式：会改动你的文件")
        colour = "#1A7F37" if readonly else "#B54708"
        try:
            self.mode_label_widget.configure(text=text, foreground=colour)
        except Exception:
            pass
        try:
            self.root.title(window_title())
        except Exception:
            pass

    # -- 界面 ---------------------------------------------------------------
    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.configure(".", font=("Microsoft YaHei UI", 10))
            style.configure("Treeview", rowheight=52)
            style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        except Exception:
            pass

        bar = ttk.Frame(self.root, padding=(8, 6, 8, 4))
        bar.pack(side="top", fill="x")
        ttk.Button(bar, text="选择文件夹…", command=self.choose_folder).pack(side="left")
        self.recent_btn = ttk.Button(bar, text="▼", width=3,
                                     command=self.show_recent_folders)
        self.recent_btn.pack(side="left", padx=(2, 0))
        ttk.Button(bar, text="刷新 (F5)", command=self.refresh).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="打开此文件夹", command=self.open_folder).pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Label(bar, text="搜索：").pack(side="left")
        ent = ttk.Entry(bar, textvariable=self.search_var, width=22)
        ent.pack(side="left", padx=(4, 0))
        ent.bind("<KeyRelease>", lambda _e: self.apply_filter())
        ttk.Checkbutton(bar, text="自动刷新", variable=self.auto_var,
                        command=self._schedule_auto).pack(side="left", padx=10)
        ttk.Button(bar, text="安全设置", command=self.show_safety).pack(side="left")
        self.mode_label_widget = ttk.Label(bar, text="", foreground="#1A7F37")
        self.mode_label_widget.pack(side="right")
        self._apply_mode_ui()

        # 状态栏要先占位：主体（详情面板内容很高）是 expand 的，如果后 pack 状态栏，
        # 窗口一矮它就会被整个挤出窗口 —— 这就是"缩小后下部不适配"的原因。
        self._status_frame = ttk.Frame(self.root, padding=(10, 4, 10, 6))
        self._status_frame.pack(side="bottom", fill="x")

        body = ttk.Frame(self.root)
        body.pack(side="top", fill="both", expand=True, padx=8, pady=4)

        # 右侧详情面板固定宽度 + 可滚动（内容比窗口高时用滚轮往下看）
        right = ttk.Frame(body, width=372)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self.detail_canvas = tk.Canvas(right, highlightthickness=0,
                                       background="#F0F0F0", width=344,
                                       yscrollincrement=48)   # 滚轮每格 48 像素
        detail_vsb = ttk.Scrollbar(right, orient="vertical",
                                   command=self.detail_canvas.yview)
        self.detail_canvas.configure(yscrollcommand=detail_vsb.set)
        detail_vsb.pack(side="right", fill="y")
        self.detail_canvas.pack(side="left", fill="both", expand=True)
        detail_inner = ttk.Frame(self.detail_canvas)
        self._detail_window = self.detail_canvas.create_window(
            (0, 0), window=detail_inner, anchor="nw")
        detail_inner.bind("<Configure>", lambda _e: self.detail_canvas.configure(
            scrollregion=self.detail_canvas.bbox("all")))
        self.detail_canvas.bind("<Configure>", lambda e: self.detail_canvas.itemconfigure(
            self._detail_window, width=e.width))
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        cols = ("size", "type", "mtime", "rating")
        self.tree = ttk.Treeview(left, columns=cols, show="tree headings",
                                 selectmode="browse")
        self.tree.heading("#0", text="名称 / 图片", command=lambda: self.set_sort("name"))
        self.tree.column("#0", width=330, minwidth=220, stretch=True)
        for col, label, width, anchor in (
            ("size", "大小", 90, "e"), ("type", "类型", 90, "w"),
            ("mtime", "修改时间", 140, "w"), ("rating", "评分", 96, "w"),
        ):
            self.tree.heading(col, text=label, command=lambda c=col: self.set_sort(c))
            self.tree.column(col, width=width, anchor=anchor, stretch=False)
        self.tree.tag_configure("dir", foreground="#0B5394")

        # 说明：外观和以前完全一样（还是 Treeview + ttk.Scrollbar），
        # 只是树上只保留"可见的那些行"，滚动条由我们按"第几项 / 共几项"驱动，
        # 这样几万项也是秒开，而且排序/搜索不再需要重建整张表。
        self.vsb = ttk.Scrollbar(left, orient="vertical", command=self._on_scrollbar)
        self.tree.configure(yscrollcommand=self._ignore_tree_scroll)
        self.tree.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

        self.tree.bind("<MouseWheel>", self._on_tree_wheel)
        self.tree.bind("<Button-4>", self._on_tree_wheel)
        self.tree.bind("<Button-5>", self._on_tree_wheel)
        self.tree.bind("<Up>", lambda _e: self._move_selection(-1))
        self.tree.bind("<Down>", lambda _e: self._move_selection(1))
        self.tree.bind("<Prior>", lambda _e: self._move_selection(-self._page_rows()))
        self.tree.bind("<Next>", lambda _e: self._move_selection(self._page_rows()))
        self.tree.bind("<Home>", lambda _e: self._move_selection(absolute="home"))
        self.tree.bind("<End>", lambda _e: self._move_selection(absolute="end"))
        self.tree.bind("<Configure>", self._on_tree_configure)

        self.empty_hint = ttk.Label(
            left, text="还没有选择文件夹\n\n点击左上角『选择文件夹…』，选中你要管理的那个文件夹即可",
            justify="center", foreground="#6B7280", font=("Microsoft YaHei UI", 12))

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Button-1>", self._on_row_click, add="+")
        self.tree.bind("<Control-a>", lambda _e: (self._select_all(True), "break")[1])
        self.tree.bind("<Motion>", self.on_motion)
        self.tree.bind("<Leave>", self.on_leave)
        self.tree.bind("<Double-1>", self.on_double)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<Return>", lambda _e: self.open_selected())
        self.tree.bind("<Key>", self.on_key)

        self._build_detail(detail_inner)
        self._bind_detail_wheel(detail_inner)
        self.detail_canvas.bind("<MouseWheel>", self._on_detail_wheel)

        status = self._status_frame
        # 多选 / 全选：右侧固定留出"详情面板宽度 + 一点空隙"，
        # 这样无论窗口拉多宽，它都稳定停在列表右边缘（滚动条）左侧那一段。
        self.multi_var = tk.BooleanVar(value=False)
        self.all_var = tk.BooleanVar(value=False)
        ttk.Frame(status, width=DETAIL_PANEL_W + SEL_BAR_RIGHT_GAP).pack(
            side="right", fill="y")
        sel_bar = ttk.Frame(status)
        sel_bar.pack(side="right")
        ttk.Checkbutton(sel_bar, text="多选", variable=self.multi_var,
                        command=self._on_multi_toggle).pack(side="left")
        ttk.Checkbutton(sel_bar, text="全选", variable=self.all_var,
                        command=self._on_all_toggle).pack(side="left", padx=(10, 0))
        self.sel_label = ttk.Label(sel_bar, text="", foreground="#5B6472")
        self.sel_label.pack(side="left", padx=(12, 0))
        ttk.Label(status, textvariable=self.status_var, foreground="#4B5563").pack(
            side="left", fill="x", expand=True)

    def _on_detail_wheel(self, event):
        """在详情面板上滚滚轮：只滚详情面板，不影响左边列表。"""
        try:
            box = self.detail_canvas.bbox("all")
            if not box or box[3] - box[1] <= self.detail_canvas.winfo_height():
                return None
            self.detail_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        except Exception:
            return None

    def _bind_detail_wheel(self, widget) -> None:
        """给详情面板里所有控件（含子控件）都挂上滚轮。"""
        try:
            widget.bind("<MouseWheel>", self._on_detail_wheel)
        except Exception:
            pass
        for child in widget.winfo_children():
            self._bind_detail_wheel(child)

    def _build_detail(self, parent: ttk.Frame) -> None:
        pad = {"padx": 12}
        ttk.Label(parent, text="文件详情 / 图片配置",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(6, 4), **pad)
        # 固定的"放大预览"画布：悬停或选中文件时，图片就显示在这里
        self._detail_img_ref = self._blank_detail_canvas()
        self.detail_img = tk.Label(parent, image=self._detail_img_ref,
                                   background="#F7F8FA", bd=1, relief="solid",
                                   cursor="hand2")
        self.detail_img.pack(fill="x", padx=12)
        self.detail_img.bind("<Button-1>", lambda _e: self.open_viewer())
        self.detail_info = ttk.Label(parent, text="未选择文件", justify="left",
                                     anchor="nw", wraplength=320)
        self.detail_info.pack(fill="x", pady=(6, 0), **pad)

        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(4, 0), **pad)
        ttk.Label(row, text="评分：").pack(side="left")
        self.star_labels = []
        for i in range(1, 6):
            lb = tk.Label(row, text="☆", font=("Microsoft YaHei UI", 16),
                          foreground="#E8A33D", cursor="hand2", background="#F0F0F0")
            lb.pack(side="left")
            lb.bind("<Button-1>", lambda _e, i=i: self.set_rating(i))
            self.star_labels.append(lb)
        ttk.Button(row, text="清除", width=5,
                   command=lambda: self.set_rating(0)).pack(side="left", padx=(8, 0))

        nrow = ttk.Frame(parent)
        nrow.pack(fill="x", pady=(6, 0), **pad)
        ttk.Label(nrow, text="备注：").pack(side="left")
        self.note_var = tk.StringVar()
        ne = ttk.Entry(nrow, textvariable=self.note_var)
        ne.pack(side="left", fill="x", expand=True, padx=(2, 6))
        ne.bind("<Return>", lambda _e: self.save_note())
        ttk.Button(nrow, text="保存", width=6, command=self.save_note).pack(side="left")

        btns = ttk.Frame(parent)
        btns.pack(fill="x", pady=(8, 0), **pad)
        ttk.Button(btns, text="为该文件设置图片…", command=self.choose_cover).pack(fill="x")
        b2 = ttk.Frame(parent)
        b2.pack(fill="x", pady=(6, 0), **pad)
        ttk.Button(b2, text="用文件自身作图片", command=self.use_self_cover).pack(
            side="left", fill="x", expand=True)
        ttk.Button(b2, text="清除图片", command=self.clear_cover).pack(
            side="left", fill="x", expand=True, padx=(6, 0))
        b3 = ttk.Frame(parent)
        b3.pack(fill="x", pady=(6, 0), **pad)
        ttk.Button(b3, text="启动项", command=self.open_selected).pack(
            side="left", fill="x", expand=True)
        ttk.Button(b3, text="⚙", width=3, command=self.show_launch_menu).pack(
            side="left", padx=(6, 0))
        b4 = ttk.Frame(parent)
        b4.pack(fill="x", pady=(6, 0), **pad)
        ttk.Button(b4, text="在资源管理器中显示", command=self.reveal_selected).pack(fill="x")

    def _build_menu(self) -> None:
        m = tk.Menu(self.root, tearoff=0)
        m.add_command(label="启动项 / 打开（双击）", command=self.open_selected)
        m.add_command(label="在资源管理器中显示", command=self.reveal_selected)
        m.add_command(label="复制完整路径", command=self.copy_path)
        m.add_separator()
        m.add_command(label="为该文件设置图片…", command=self.choose_cover)
        m.add_command(label="用文件自身作图片", command=self.use_self_cover)
        m.add_command(label="清除图片", command=self.clear_cover)
        m.add_separator()
        rate = tk.Menu(m, tearoff=0)
        for i in range(6):
            rate.add_command(label=("%d 星" % i) if i else "清除评分",
                             command=lambda i=i: self.set_rating(i))
        m.add_cascade(label="评分", menu=rate)
        launch = tk.Menu(m, tearoff=0)
        launch.add_command(label="选择启动程序（只启动它）…",
                           command=lambda: self.choose_launch_program(False))
        launch.add_command(label="选择启动程序（并把此文件传给它）…",
                           command=lambda: self.choose_launch_program(True))
        launch.add_separator()
        launch.add_command(label="清除启动项", command=self.clear_launch_program)
        m.add_cascade(label="启动项设置", menu=launch)
        m.add_separator()
        m.add_command(label="重新统计所有文件夹大小", command=self.recompute_folder_sizes)
        m.add_command(label="刷新列表（F5）", command=self.refresh)
        m.add_separator()
        m.add_command(label="（本程序不会删除 / 移动 / 重命名你的文件）", state="disabled")
        self.menu = m

        self.launch_menu = tk.Menu(self.root, tearoff=0)
        self.launch_menu.add_command(label="选择启动程序（只启动它）…",
                                     command=lambda: self.choose_launch_program(False))
        self.launch_menu.add_command(label="选择启动程序（并把此文件传给它）…",
                                     command=lambda: self.choose_launch_program(True))
        self.launch_menu.add_separator()
        self.launch_menu.add_command(label="清除启动项", command=self.clear_launch_program)

    def show_launch_menu(self) -> None:
        """详情面板里那个 ⚙ 小按钮：设置 / 清除启动项。"""
        try:
            x = self.root.winfo_pointerx()
            y = self.root.winfo_pointery()
            self.launch_menu.tk_popup(x, y)
        finally:
            try:
                self.launch_menu.grab_release()
            except Exception:
                pass

    # -- 文件夹 -------------------------------------------------------------
    def choose_folder(self) -> None:
        init = str(self.folder) if self.folder else str(Path.home())
        picked = filedialog.askdirectory(title="选择要管理的文件夹（只会管理这一个）",
                                         initialdir=init, mustexist=True)
        if picked:
            self.set_folder(picked)

    def set_folder(self, folder) -> None:
        folder = Path(folder)
        if not folder.is_dir():
            messagebox.showwarning("无法打开", "文件夹不存在：%s" % folder)
            return
        self.folder = folder
        self.cfg["last_folder"] = str(folder)
        self._remember_folder(folder, save=False)
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self.empty_hint.place_forget()
        # 注意：不清空文件夹大小缓存 —— 切回看过的文件夹要能秒显
        self.refresh()          # refresh → apply_filter 会把"共 N 项（显示 M 项）…"写上状态栏

    def show_empty_hint(self) -> None:
        self.empty_hint.place(relx=0.5, rely=0.45, anchor="center")

    def open_folder(self) -> None:
        if self.folder:
            try:
                shell_open(self.folder)
            except Exception as exc:
                self._open_failed(exc)

    # -- 列表 ---------------------------------------------------------------
    def refresh(self, keep_selection: bool = False) -> None:
        if not self.folder:
            self.show_empty_hint()
            return
        sel_rel = None
        if keep_selection:
            cur = self.selected_entry()
            sel_rel = cur.rel if cur else None

        self.entries = scan_folder(self.folder, skip=data_dir())
        self._sig = self._signature(self.entries)
        self._by_path = {str(e.path): e for e in self.entries}
        self._attach_idents()          # 补文件身份（有标注的文件夹才值得去取）
        self.apply_filter(keep_selection=keep_selection, sel_rel=sel_rel)

    @staticmethod
    def _signature(entries) -> tuple:
        return tuple(sorted((e.name, e.is_dir, e.size, int(e.mtime)) for e in entries))

    def sort_entries(self, entries) -> list:
        key = self.sort_key
        if key == "size":
            def size_of(e):
                if not e.is_dir:
                    return e.size
                got = self._size_cache.get(str(e.path))
                return got[0] if got else -1      # 还没算出来的排在最后
            f = lambda e: (e.is_dir, size_of(e), e.name.lower())
        elif key == "type":
            f = lambda e: (e.is_dir is False, TYPE_LABEL.get(e.kind, ""), e.name.lower())
        elif key == "mtime":
            f = lambda e: (e.is_dir, e.mtime, e.name.lower())
        elif key == "rating":
            r = lambda e: int(self._lib_entry(e).get("rating", 0) or 0)
            f = lambda e: (e.is_dir, -r(e), e.name.lower())
        else:
            f = lambda e: (not e.is_dir, e.name.lower())
        return sorted(entries, key=f, reverse=self.sort_rev)

    def set_sort(self, key: str) -> None:
        if self.sort_key == key:
            self.sort_rev = not self.sort_rev
        else:
            self.sort_key, self.sort_rev = key, False
        self.cfg["sort_key"], self.cfg["sort_rev"] = self.sort_key, self.sort_rev
        try:
            save_config(self.cfg)
        except Exception:
            pass
        self.apply_filter()

    # -- 窗口化列表（只渲染可见行）------------------------------------------
    def _ignore_tree_scroll(self, *_args) -> None:
        """Treeview 自己的滚动通知丢掉：滚动条由我们驱动。"""
        return None

    def _viewport_rows(self) -> int:
        """一屏**真正能显示**的行数（不含额外缓存的缓冲行）。"""
        try:
            height = self.tree.winfo_height()
        except Exception:
            height = 0
        return max(1, int(height // ROW_HEIGHT))

    def _visible_rows(self) -> int:
        """行池大小 = 一屏可见行 + 缓冲行（多出来的几行只是为了滚动不闪）。"""
        return self._viewport_rows() + ROW_POOL_EXTRA

    def _page_rows(self) -> int:
        return max(1, self._visible_rows() - ROW_POOL_EXTRA - 1)

    def _ensure_pool(self) -> None:
        """按窗口高度准备固定数量的行；数量没变就什么都不做（复用行）。"""
        want = self._visible_rows()
        if len(self._row_ids) == want:
            return
        self._pool_rebuilds += 1
        try:
            self.tree.delete(*self._row_ids)
        except Exception:
            pass
        self._row_ids = [self.tree.insert("", "end", text="") for _ in range(want)]
        self.item_entry.clear()
        # 滚动上限按"一屏可见行数"算 —— 缓冲行只是藏在可视区下方，不能算进上限，
        # 否则最后 ROW_POOL_EXTRA 项会永远滚不出来。
        self._offset = max(0, min(self._offset,
                                  max(0, len(self.visible) - self._viewport_rows())))
        self._fill_rows()

    def _fill_rows(self) -> None:
        """把当前窗口内的文件写进那几十行里（只改内容，不重建行）。"""
        total = len(self.visible)
        offset = self._offset
        self.item_entry.clear()
        for i, iid in enumerate(self._row_ids):
            idx = offset + i
            if idx >= total:
                self.tree.item(iid, text="", image="", values=("", "", "", ""),
                               tags=())
                continue
            e = self.visible[idx]
            lib_entry = self._lib_entry(e)
            rating = int(lib_entry.get("rating", 0) or 0)
            self.tree.item(
                iid, text="  " + e.name, image=self.thumb_for(e, lib_entry),
                values=(self._size_cell_text(e), TYPE_LABEL.get(e.kind, ""),
                        human_time(e.mtime), stars_text(rating)),
                tags=("dir",) if e.is_dir else ())
            self.item_entry[iid] = e
        try:
            self.tree.yview_moveto(0.0)      # 池子第一行永远在可视区顶部
        except Exception:
            pass
        self._hover_item = None              # 行内容变了，悬停预览需要重新判定
        self._sync_selection_item()
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        total = len(self.visible)
        view = self._viewport_rows()
        if total <= 0:
            self.vsb.set(0.0, 1.0)
            return
        first = self._offset / float(total)
        last = min(1.0, (self._offset + view) / float(total))
        try:
            self.vsb.set(first, last)
        except Exception:
            pass

    def _set_offset(self, offset: int) -> None:
        total = len(self.visible)
        max_off = max(0, total - self._viewport_rows())
        offset = max(0, min(int(offset), max_off))
        if offset == self._offset:
            self._sync_scrollbar()
            return
        self._offset = offset
        self._fill_rows()

    def _scroll_by(self, delta: int) -> None:
        self._set_offset(self._offset + delta)

    def _on_scrollbar(self, *args) -> None:
        """滚动条回调：支持拖滑块(moveto)、翻页(pages)和按行(scroll)。"""
        if not args:
            return
        cmd = args[0]
        total = max(1, len(self.visible))
        if cmd == "moveto":
            try:
                self._set_offset(int(float(args[1]) * total))
            except Exception:
                pass
        elif cmd == "scroll":
            try:
                n = int(args[1])
            except Exception:
                return
            what = args[2] if len(args) > 2 else "units"
            self._scroll_by(n * self._page_rows() if what == "pages"
                            else n * WHEEL_ROWS)

    def _on_tree_wheel(self, event):
        delta = getattr(event, "delta", 0)
        if delta:
            self._scroll_by(-WHEEL_ROWS if delta > 0 else WHEEL_ROWS)
        elif getattr(event, "num", 0) == 4:
            self._scroll_by(-WHEEL_ROWS)
        elif getattr(event, "num", 0) == 5:
            self._scroll_by(WHEEL_ROWS)
        return "break"

    def _on_tree_configure(self, event=None) -> None:
        """窗口大小变了才重算行池；同一时刻最多排一个待办，避免事件互相触发。"""
        height = getattr(event, "height", None)
        if height is not None:
            if height == self._last_tree_height:
                return
            self._last_tree_height = height
        if self._pool_job is not None:
            return
        try:
            self._pool_job = self.root.after_idle(self._pool_tick)
        except Exception:
            self._pool_job = None

    def _pool_tick(self) -> None:
        self._pool_job = None
        if not self._alive():
            return
        self._ensure_pool()

    def _sync_selection_item(self) -> None:
        """把选中的行在树上高亮（支持多选；不触发回调）。"""
        self._sel_guard = True
        try:
            self.tree.selection_remove(*self.tree.selection())
            wanted = set(self._selected_set)
            if self._selected_index is not None:
                wanted.add(self._selected_index)
            rows = []
            for i, iid in enumerate(self._row_ids):
                if (self._offset + i) in wanted:
                    rows.append(iid)
            if rows:
                self.tree.selection_set(*rows)
        except Exception:
            pass
        finally:
            self._sel_guard = False

    # -- 多选 / 全选 ---------------------------------------------------------
    def _row_index(self, item):
        """树上某一行对应的 self.visible 下标。"""
        try:
            return self._offset + self._row_ids.index(item)
        except ValueError:
            return None

    def _on_row_click(self, event):
        """自己处理点击：Ctrl 或勾了"多选"就是多选，否则单选。"""
        item = self.tree.identify_row(event.y)
        idx = self._row_index(item) if item else None
        if idx is None or not (0 <= idx < len(self.visible)):
            return None
        ctrl = bool(getattr(event, "state", 0) & 0x0004)
        multi = bool(self.multi_var.get()) or ctrl
        if multi:
            if idx in self._selected_set:
                self._selected_set.discard(idx)
            else:
                self._selected_set.add(idx)
            self._selected_index = idx
            self.all_var.set(False)
        else:
            self._selected_set = {idx}
            self._selected_index = idx
        self._remember_selection_names()
        self._sync_selection_item()
        self._update_sel_label()
        self.update_detail()
        return "break"

    def _remember_selection_names(self) -> None:
        self._selected_names = {self.visible[i].rel for i in self._selected_set
                                if 0 <= i < len(self.visible)}

    def _select_all(self, on: bool) -> None:
        if on:
            self._selected_set = set(range(len(self.visible)))
            self.all_var.set(True)
            if self._selected_index is None and self.visible:
                self._selected_index = 0
        else:
            self._selected_set = set()
            self._selected_index = None      # 取消全选 = 什么都不选
            self.all_var.set(False)
        self._remember_selection_names()
        self._sync_selection_item()
        self._update_sel_label()
        self.update_detail()

    def _on_all_toggle(self) -> None:
        self._select_all(bool(self.all_var.get()))

    def _on_multi_toggle(self) -> None:
        if not self.multi_var.get():
            keep = self._selected_index
            self._selected_set = {keep} if keep is not None else set()
        self._remember_selection_names()
        self._sync_selection_item()
        self._update_sel_label()

    def _update_sel_label(self) -> None:
        n = len([i for i in self._selected_set if 0 <= i < len(self.visible)])
        try:
            self.sel_label.configure(text=("已选 %d 项" % n) if n > 1 else "")
        except Exception:
            pass

    def selected_entries(self) -> list:
        """当前选中的所有文件（多选/全选时是全部选中项）。"""
        idxs = sorted(i for i in self._selected_set if 0 <= i < len(self.visible))
        if not idxs:
            one = self.selected_entry()
            return [one] if one else []
        return [self.visible[i] for i in idxs]

    # -- 导出清单 -----------------------------------------------------------
    def _export_size_text(self, entry: Entry) -> str:
        if not entry.is_dir:
            return human_size(entry.size)
        got = self._size_cache.get(str(entry.path))
        return human_size(got[0]) if got else "—"

    def export_selected(self, parent=None) -> None:
        """把选中（或全部）文件的 命名/类型/大小/评分 导出成一个 txt。"""
        parent = parent or self.root
        if self.folder is None or not self.visible:
            messagebox.showinfo("没有可导出的内容", "请先打开一个文件夹。", parent=parent)
            return
        items = self.selected_entries()
        if not items:
            if not messagebox.askyesno(
                    "导出", "当前没有选中任何文件。\n要导出全部 %d 项吗？" % len(self.visible),
                    parent=parent):
                return
            items = list(self.visible)
        target_dir = filedialog.askdirectory(title="选择导出到哪个文件夹", mustexist=True,
                                             parent=parent)
        if not target_dir:
            return
        rows = []
        for e in sorted(items, key=lambda en: natural_key(en.name)):
            rating = int(self._lib_entry(e).get("rating", 0) or 0)
            rows.append((e.name, TYPE_LABEL.get(e.kind, ""),
                         self._export_size_text(e), rating if rating else "-"))
        base_name = "文件清单_%s_%s.txt" % (Path(self.folder).name or "列表",
                                          time.strftime("%Y%m%d_%H%M%S"))
        last_error = None
        for _ in range(5):
            path = unique_path(Path(target_dir) / base_name)
            try:
                export_file_list(path, rows)
            except FileExistsError as exc:      # 极小概率：刚好被人抢先建了
                last_error = exc
                continue
            except Exception as exc:
                messagebox.showerror("导出失败", str(exc), parent=parent)
                return
            self.status_var.set("已导出 %d 项到 %s" % (len(rows), path))
            messagebox.showinfo("导出完成",
                                "已导出 %d 项：\n%s" % (len(rows), path), parent=parent)
            return
        messagebox.showerror("导出失败", str(last_error), parent=parent)

    def _on_tree_select(self, _event=None) -> None:
        if self._sel_guard:
            return
        sel = self.tree.selection()
        if not sel:
            return
        e = self.item_entry.get(sel[0])
        if e is None:
            return
        try:
            self._selected_index = self.visible.index(e)
        except ValueError:
            self._selected_index = None
        self.update_detail()

    def _move_selection(self, delta: int = 0, absolute: str | None = None) -> str:
        total = len(self.visible)
        if total == 0:
            return "break"
        if absolute == "home":
            idx = 0
        elif absolute == "end":
            idx = total - 1
        else:
            base = self._selected_index
            idx = (0 if base is None else base + delta)
        idx = max(0, min(idx, total - 1))
        self._selected_index = idx
        pool = max(1, len(self._row_ids))
        if idx < self._offset:
            self._set_offset(idx)
        elif idx >= self._offset + pool:
            self._set_offset(idx - pool + 1)
        else:
            self._sync_selection_item()
        self.update_detail()
        return "break"

    def apply_filter(self, keep_selection: bool = False, sel_rel: str | None = None) -> None:
        if self.folder is None:
            self.show_empty_hint()
            return
        q = self.search_var.get().strip().lower()
        items = [e for e in self.entries if (not q or q in e.name.lower())]
        items = self.sort_entries(items)

        if sel_rel is None and not self._selected_names:
            cur = self.selected_entry()
            sel_rel = cur.rel if cur else None
        self.visible = items
        self._selected_index = None
        if sel_rel:
            for i, e in enumerate(items):
                if e.rel == sel_rel:
                    self._selected_index = i
                    break

        # 还原多选（按名字，过滤/刷新后仍在的项保持选中）
        names = set(self._selected_names)
        if sel_rel:
            names.add(sel_rel)
        self._selected_set = set()
        if self.all_var.get():
            self._selected_set = set(range(len(items)))
        elif names:
            for i, e in enumerate(items):
                if e.rel in names:
                    self._selected_set.add(i)
        if self._selected_index is None and self._selected_set:
            self._selected_index = min(self._selected_set)
        if self._selected_index is not None and self._selected_set:
            self._selected_set.add(self._selected_index)
        self._remember_selection_names()

        self._ensure_pool()
        pool = max(1, len(self._row_ids))
        if self._selected_index is not None:
            if self._selected_index < self._offset:
                self._offset = self._selected_index
            elif self._selected_index >= self._offset + pool:
                self._offset = self._selected_index - pool + 1
        # 内容变了就必须重画（不能只靠"偏移量变了没"来判断）
        self._offset = max(0, min(self._offset, max(0, len(self.visible) - pool)))
        self._fill_rows()

        for e in items:                      # 文件夹大小照旧全部排队（后台慢慢算）
            self._maybe_verify_size(e)
        self.status_var.set("共 %d 项（显示 %d 项）· %s · 只读"
                            % (len(self.entries), len(items), self.folder))
        self._update_sel_label()
        self._hover_item = None
        self.update_detail(self._detail_entry)

    def thumb_for(self, entry: Entry, lib_entry: dict):
        """列表缩略图：优先使用用户配置的封面/自身图片，否则用类型图标。

        结果按（路径 + 修改时间 + 封面）缓存，刷新/自动刷新时不再重复解码，
        大目录下这一点非常关键（否则每次重排都要重新读几百张图）。
        """
        cover_rel = lib_entry.get("cover") or (
            "self" if lib_entry.get("use_self") else "")
        key = (str(entry.path), int(entry.mtime), cover_rel)
        cached = self._thumb_cache.get(key)
        if cached is not None:
            try:
                cached.width()          # 触碰一下，已失效的会抛异常
                return cached
            except Exception:
                self._thumb_cache.pop(key, None)

        src = None
        cover = self.lib.cover_path(lib_entry)
        if cover is not None:
            src = cover
        elif lib_entry.get("use_self") and entry.kind == "image":
            src = entry.path
        photo = None
        if src is not None:
            photo, _status = load_photo(src, 44)
        if photo is None:
            photo = icon_photo(entry.kind, 44)
        if len(self._thumb_cache) >= THUMB_CACHE_MAX:
            self._thumb_cache.clear()   # 简单省事：满了就整批丢掉重建
        self._thumb_cache[key] = photo
        return photo

    def selected_entry(self) -> Entry | None:
        """当前选中项。

        优先用树上真实高亮的那一行（例如刚点完、事件还没处理），
        该行已经滚出可视区时再退回到逻辑下标 —— 这样滚多远都还记得选中。
        """
        sel = self.tree.selection()
        if sel:
            e = self.item_entry.get(sel[0])
            if e is not None:
                return e
        idx = self._selected_index
        if idx is None or not (0 <= idx < len(self.visible)):
            return None
        return self.visible[idx]

    # -- 详情面板 -----------------------------------------------------------
    def _blank_detail_canvas(self) -> tk.PhotoImage:
        canvas = tk.PhotoImage(master=self.root, width=DETAIL_IMG_W, height=DETAIL_IMG_H)
        canvas.put("#F7F8FA", to=(0, 0, DETAIL_IMG_W, DETAIL_IMG_H))
        return canvas

    def _detail_canvas(self, src, kind: str) -> tk.PhotoImage:
        """把图片（或类型图标）居中画在固定尺寸的画布上，切换文件时布局不跳动。"""
        canvas = self._blank_detail_canvas()
        photo = None
        if src is not None:
            photo, _status = load_photo(src, (DETAIL_IMG_W - 8, DETAIL_IMG_H - 8))
        if photo is None:
            icon = icon_photo(kind, 116)
            photo = icon
        try:
            x = max(0, (DETAIL_IMG_W - photo.width()) // 2)
            y = max(0, (DETAIL_IMG_H - photo.height()) // 2)
            self.root.tk.call(str(canvas), "copy", str(photo), "-to", x, y)
        except Exception:
            pass
        return canvas

    def _flush_pending_note(self) -> None:
        """切换详情面板前，把还没保存的备注悄悄存下来（只写程序自己的 data）。"""
        e = self._detail_entry
        if e is None or self.folder is None or not hasattr(self, "note_var"):
            return
        try:
            typed = self.note_var.get().strip()
        except Exception:
            return
        stored = self._lib_entry(e).get("note", "") or ""
        if typed != stored:
            try:
                self.lib.update(self.folder, e.rel, ident=e.ident, note=typed)
            except Exception:
                pass

    def update_detail(self, entry=None) -> None:
        """显示某个文件的详情；entry 为空时显示当前选中项。"""
        e = entry if entry is not None else self.selected_entry()
        if e is not self._detail_entry:
            self._flush_pending_note()
        self._detail_entry = e
        if e is None:
            self._detail_img_ref = self._blank_detail_canvas()
            self.detail_img.configure(image=self._detail_img_ref)
            self.detail_info.configure(text="未选择文件\n\n把鼠标停在列表中的文件上，这里就会放大预览它的图片")
            self.note_var.set("")
            self.update_stars(0)
            return
        lib_entry = self._lib_entry(e)
        src = self.hover_source(e, lib_entry)
        self._detail_img_ref = self._detail_canvas(src, e.kind)
        self.detail_img.configure(image=self._detail_img_ref)
        tip = {"need_pil": "（该格式预览需要 Pillow）", "missing": "（未配置图片）",
               "error": "（图片无法读取）"}.get(
                   load_photo(src, (DETAIL_IMG_W - 8, DETAIL_IMG_H - 8))[1]
                   if src is not None else "missing", "")
        if e.is_dir:
            size_txt = self._folder_size_text(e)
            img_txt = "图片：%s%s" % ("已配置封面" if src is not None else "未配置", tip)
        else:
            size_txt = human_size(e.size)
            if src is None:
                img_txt = "图片：未配置%s（右键『为该文件设置图片…』）" % tip
            elif lib_entry.get("cover"):
                img_txt = "图片：已配置封面"
            else:
                img_txt = "图片：使用文件自身%s" % tip
        lines = [
            e.name,
            "类型：%s%s" % (TYPE_LABEL.get(e.kind, ""), (" / " + e.ext) if e.ext else ""),
            "大小：%s" % size_txt,
            "修改时间：%s" % human_time(e.mtime),
            "位置：%s" % e.path,
            img_txt,
        ]
        target = lib_entry.get("launch")
        if target:
            lines.append("启动项：%s%s" % (
                Path(target).name,
                "（启动时带上该文件）" if lib_entry.get("launch_arg") else "（只启动程序）"))
        self.detail_info.configure(text="\n".join(lines))
        self.note_var.set(lib_entry.get("note", "") or "")
        self.update_stars(int(lib_entry.get("rating", 0) or 0))

    def update_stars(self, rating: int) -> None:
        for i, lb in enumerate(self.star_labels, start=1):
            lb.configure(text="★" if i <= rating else "☆")

    def hover_source(self, entry: Entry, lib_entry: dict):
        """悬停预览优先使用配置的封面，其次文件自身（图片文件）。"""
        cover = self.lib.cover_path(lib_entry)
        if cover is not None:
            return cover
        # 没有配置封面时，图片文件就把自己展示出来
        if entry.kind == "image":
            return entry.path
        return None

    # -- 悬停：把放大预览切到鼠标所指的文件 ---------------------------------
    def _cancel_hover(self) -> None:
        if self._hover_job:
            try:
                self.root.after_cancel(self._hover_job)
            except Exception:
                pass
            self._hover_job = None

    def on_motion(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if item == self._hover_item:
            return
        self._hover_item = item
        self._cancel_hover()
        if item:
            self._hover_job = self.root.after(
                self.PREVIEW_DELAY_MS, lambda it=item: self.preview_in_detail(it))
        else:
            self.update_detail()          # 鼠标离开列表 → 回到当前选中项

    def on_leave(self, _event=None) -> None:
        self._cancel_hover()
        self._hover_item = None
        self.update_detail()              # 回到当前选中项的详情

    def preview_in_detail(self, item: str) -> None:
        """鼠标悬停到某个文件：直接在详情面板里放大预览它（不改动选中状态）。"""
        self._hover_job = None
        e = self.item_entry.get(item)
        if e is None or not self.tree.exists(item):
            return
        self.update_detail(e)

    # -- 大图查看器 ---------------------------------------------------------
    def open_viewer(self):
        """单击详情面板里的图片 → 打开一张大图（单击或 Esc 关闭）。"""
        e = self._detail_entry or self.selected_entry()
        if e is None or self.folder is None:
            return None
        lib_entry = self._lib_entry(e)
        src = self.hover_source(e, lib_entry)
        if src is None:
            messagebox.showinfo("没有图片",
                                "『%s』还没有配置图片。\n选中它后点『为该文件设置图片…』即可。"
                                % e.name)
            return None
        box = (int(self.root.winfo_screenwidth() * 0.82),
               int(self.root.winfo_screenheight() * 0.78))
        photo, status = load_photo(src, box)
        if photo is None:
            messagebox.showinfo("无法显示这张图片", {
                "need_pil": "这种格式需要安装 Pillow 才能显示（见 README）。",
                "missing": "图片文件不存在了。",
            }.get(status, "图片无法读取。"))
            return None
        win = tk.Toplevel(self.root)
        win.title("图片预览 - %s" % e.name)
        win.configure(background="#20242B")
        win._photo_ref = photo            # 保住引用，避免被回收
        try:
            win.transient(self.root)      # 始终待在主窗口前面
        except Exception:
            pass
        tk.Label(win, image=photo, background="#20242B").pack(padx=12, pady=(12, 6))
        tk.Label(win, text="%s　·　单击图片或按 Esc 关闭" % e.name,
                 background="#20242B", foreground="#C9CFD8").pack(pady=(0, 12))
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry("+%d+%d" % (max(0, (sw - w) // 2), max(0, (sh - h) // 3)))
        win.bind("<Escape>", lambda _e: win.destroy())
        win.bind("<Button-1>", lambda _e: win.destroy())
        try:
            win.focus_set()
        except Exception:
            pass
        return win

    # -- 文件身份：改名/移动后标注自动跟随 ---------------------------------
    def _id_cache_path(self) -> Path:
        return cache_dir() / ID_CACHE_NAME

    def _load_id_cache(self) -> None:
        obj = load_json(self._id_cache_path(), {})
        entries = obj.get("ids") if isinstance(obj, dict) else None
        if not isinstance(entries, dict):
            return
        for key, rec in entries.items():
            try:
                size, mtime, ident, ts = rec
                self._id_cache[str(key)] = (int(size), int(mtime), str(ident), float(ts))
            except Exception:
                continue

    def _save_id_cache(self, force: bool = False) -> None:
        if not self._id_cache:
            return
        if len(self._id_cache) > ID_CACHE_MAX:
            oldest = sorted(self._id_cache.items(), key=lambda kv: kv[1][3])
            for key, _rec in oldest[:len(self._id_cache) - ID_CACHE_MAX]:
                self._id_cache.pop(key, None)
        payload = {"version": 1, "updated": time.time(),
                   "ids": {k: list(v) for k, v in self._id_cache.items()}}
        try:
            atomic_write_text(self._id_cache_path(),
                              json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    def _ensure_id_drain(self) -> None:
        if self._id_job is None and self._alive():
            try:
                self._id_job = self.root.after(150, self._drain_idents)
            except Exception:
                self._id_job = None

    def _attach_idents(self) -> None:
        """给列表条目补上"文件身份"；没缓存到的交给后台线程按需去取。

        只有在"这个文件夹已经有标注"时才值得去取 ID —— 否则纯属白花钱开句柄。
        """
        if self.folder is None:
            return
        has_records = bool(self.lib.folder(self.folder))
        for e in self.entries:
            cached = self._id_cache.get(os.path.normcase(str(e.path)))
            if cached is not None and cached[0] == e.size and cached[1] == int(e.mtime):
                e.ident = cached[2] if cached[2] else ""
            elif has_records:
                self._request_ident(e)
        self._ensure_id_drain()

    def _request_ident(self, entry: Entry) -> None:
        path = str(entry.path)
        if path in self._id_pending:
            return
        self._id_pending.add(path)
        self._id_wanted.append((path, entry.size, int(entry.mtime)))
        self._pump_id_worker()
        self._ensure_id_drain()

    def _pump_id_worker(self) -> None:
        if self._id_active or not self._id_wanted:
            return
        path, size, mtime = self._id_wanted.popleft()
        self._id_active = True
        threading.Thread(target=self._id_worker, args=(path, size, mtime),
                         name="file-ident", daemon=True).start()

    def _id_worker(self, path: str, size: int, mtime: int) -> None:
        ident = ""
        try:
            ident = file_ident(path)
        except Exception:
            ident = ""
        try:
            self._id_queue.put((path, size, mtime, ident))
        finally:
            self._id_active = False
            self._pump_id_worker()

    def _drain_idents(self) -> None:
        """主线程收 ID 结果：写缓存、补到条目上，全部取完再做一次改名归并。"""
        self._id_job = None
        if not self._alive():
            return
        changed = False
        while True:
            try:
                path, size, mtime, ident = self._id_queue.get_nowait()
            except queue.Empty:
                break
            self._id_cache[os.path.normcase(path)] = (size, mtime, ident, time.time())
            self._id_pending.discard(path)
            e = self._by_path.get(path)
            if e is not None and e.size == size and int(e.mtime) == mtime:
                e.ident = ident or ""
            changed = True
        if changed:
            self._id_dirty = True
        if not self._id_pending and not self._id_wanted and self._id_dirty:
            self._id_dirty = False
            self._reconcile_idents()
        if (self._id_pending or self._id_wanted) and self._alive():
            self._id_job = self.root.after(150, self._drain_idents)

    def _reconcile_idents(self) -> None:
        """按身份把"改过名"的标注搬到新名字下（这一步就是"标注跟随重命名"）。"""
        if self.folder is None:
            return
        pairs = [(e.rel, e.ident) for e in self.entries if e.ident]
        moved = self.lib.reconcile(self.folder, pairs) if pairs else 0
        self._save_id_cache(force=True)
        if moved:
            self._fill_rows()
            self.update_detail(self._detail_entry)
            self.status_var.set("已按文件身份找回 %d 条标注（文件改名/移动不会丢）" % moved)

    def _ensure_ident(self, entry: Entry) -> None:
        """给某个文件准备好身份信息：有缓存立刻补上，没有就丢给后台去取。

        标注操作（评分/备注/封面/启动项）都会先调它 —— 这样即使用户是第一次
        标注这个文件夹，记录里也能很快带上文件 ID，日后再改名就不会丢。
        """
        if entry is None or entry.ident:
            return
        cached = self._id_cache.get(os.path.normcase(str(entry.path)))
        if cached is not None and cached[0] == entry.size and cached[1] == int(entry.mtime):
            entry.ident = cached[2] or ""
            return
        self._request_ident(entry)

    def _lib_entry(self, entry: Entry) -> dict:
        """取标注：优先按文件身份（改过名也认得），拿不到身份就按名字。"""
        if entry is None or self.folder is None:
            return {}
        return self.lib.entry(self.folder, entry.rel, entry.ident)

    # -- 文件夹大小 ---------------------------------------------------------
    def _size_cache_path(self) -> Path:
        return cache_dir() / SIZE_CACHE_NAME

    def _load_size_cache(self) -> None:
        """从 data\\cache 读回上次的统计结果（只读，读失败就当没有）。"""
        obj = load_json(self._size_cache_path(), {})
        entries = obj.get("sizes") if isinstance(obj, dict) else None
        if not isinstance(entries, dict):
            return
        for key, rec in entries.items():
            try:
                size, truncated, mtime, computed = rec
                self._size_cache[str(key)] = (int(size), bool(truncated),
                                              int(mtime), float(computed))
            except Exception:
                continue

    def _save_size_cache(self, force: bool = False) -> None:
        """把统计结果写回 data\\cache（只写程序自己的目录）。"""
        now = time.time()
        if not force and now - self._size_saved_at < 5.0:
            return
        if not self._size_cache and not force:
            return
        self._size_saved_at = now
        if len(self._size_cache) > SIZE_CACHE_MAX:
            oldest = sorted(self._size_cache.items(), key=lambda kv: kv[1][3])
            for key, _rec in oldest[:len(self._size_cache) - SIZE_CACHE_MAX]:
                self._size_cache.pop(key, None)
        payload = {"version": 1, "updated": now,
                   "sizes": {k: list(v) for k, v in self._size_cache.items()}}
        try:
            atomic_write_text(self._size_cache_path(),
                              json.dumps(payload, ensure_ascii=False))
        except Exception:
            pass

    def _request_folder_size(self, entry: Entry) -> None:
        """把文件夹排进统计队列；真正开工由有界线程池决定。"""
        key = str(entry.path)
        if key in self._size_pending:
            return
        self._size_pending.add(key)
        self._size_wanted.append(entry)
        self._pump_size_workers()
        self._ensure_size_drain()

    def _ensure_size_drain(self) -> None:
        """确保"收取结果"的定时器在跑。

        这个定时器发现没事干时会自己停下（省电），所以每次发起统计都必须唤醒它 ——
        否则算好的结果会一直堆在队列里没人取，界面上永远显示"计算中…"。
        （曾经的 bug：格式化清掉 last_folder 后程序是"无文件夹"状态启动，
        第一次轮询空转即停，之后再打开文件夹就再也收不到结果。）
        只能在主线程调用。
        """
        if self._size_job is None and self._alive():
            try:
                self._size_job = self.root.after(120, self._drain_sizes)
            except Exception:
                self._size_job = None

    def _pump_size_workers(self) -> None:
        """最多同时跑 SIZE_MAX_WORKERS 个线程。

        以前是"每个文件夹一个线程"，打开大目录时会瞬间几十个线程同时读盘，
        把磁盘和界面都拖住（表现就是窗口先黑几秒）。现在排队慢慢来。
        """
        if not self._sizes_enabled:
            return
        with self._size_lock:
            while self._size_wanted and self._size_active < SIZE_MAX_WORKERS:
                entry = self._size_wanted.popleft()
                key = str(entry.path)
                if key not in self._size_pending:
                    continue
                self._size_active += 1
                threading.Thread(target=self._size_worker,
                                 args=(key, entry.path, int(entry.mtime)),
                                 name="folder-size", daemon=True).start()

    def _size_worker(self, key: str, path, mtime: int) -> None:
        try:
            size, truncated = measure_folder(path)
        except Exception:
            size, truncated = 0, True
        try:
            self._size_queue.put((key, size, truncated, mtime))
        finally:
            # 线程里就把名额让出来并接着排下一个，不用等主线程 120 毫秒的轮询，
            # 子文件夹很多时（几百个）整体能快一倍左右
            with self._size_lock:
                self._size_active = max(0, self._size_active - 1)
            self._pump_size_workers()

    def _maybe_verify_size(self, entry: Entry) -> None:
        """有缓存就先显示缓存；需要校验时丢给后台线程，绝不阻塞界面。"""
        if not entry.is_dir:
            return
        key = str(entry.path)
        got = self._size_cache.get(key)
        if got is None:
            self._request_folder_size(entry)
            return
        _size, _trunc, mtime, computed = got
        if (int(entry.mtime) != mtime
                or (key not in self._size_verified
                    and time.time() - computed > SIZE_CACHE_TRUST_SECONDS)):
            self._request_folder_size(entry)

    def _folder_size_text(self, entry: Entry) -> str:
        if not entry.is_dir:
            return human_size(entry.size)
        key = str(entry.path)
        got = self._size_cache.get(key)
        if got is None:
            self._request_folder_size(entry)
            return "计算中…"
        size, truncated, _mtime, _computed = got
        self._maybe_verify_size(entry)
        suffix = "（含子文件）" if key in self._size_verified else "（含子文件·上次统计）"
        return ("≥ " if truncated else "") + human_size(size) + suffix

    def _alive(self) -> bool:
        """窗口还在、而且没在关闭过程中：定时回调必须先问这一句。"""
        if self._closing:
            return False
        try:
            return bool(self.root.winfo_exists())
        except Exception:
            return False

    def _drain_sizes(self) -> None:
        """在主线程里收取后台线程算好的文件夹大小（Tk 只能在主线程用）。"""
        # 先撤销任何还排在队里的旧回调，避免留下"孤儿"定时器
        # （窗口关闭后再触发会报 invalid command name）
        if self._size_job is not None:
            try:
                self.root.after_cancel(self._size_job)
            except Exception:
                pass
        self._size_job = None
        if not self._alive():
            return
        changed = False
        while True:
            try:
                key, size, truncated, mtime = self._size_queue.get_nowait()
            except queue.Empty:
                break
            self._size_cache[key] = (size, truncated, mtime, time.time())
            self._size_verified.add(key)
            self._size_pending.discard(key)
            changed = True
        if changed:
            self._update_size_cells()
            self._save_size_cache()
        self._pump_size_workers()
        if (self._size_pending or changed) and self._alive():
            self._size_job = self.root.after(120, self._drain_sizes)

    def _size_cell_text(self, entry: Entry) -> str:
        if not entry.is_dir:
            return human_size(entry.size)
        got = self._size_cache.get(str(entry.path))
        if got is None:
            return "计算中…"
        return ("≥ " if got[1] else "") + human_size(got[0])

    def recompute_folder_sizes(self) -> None:
        """右键『重新统计所有文件夹大小』：丢掉缓存，全部重算一遍。"""
        self._size_cache.clear()
        self._size_verified.clear()
        self._size_pending.clear()
        self._size_wanted.clear()
        self._save_size_cache(force=True)
        for e in self.visible:
            if e.is_dir:
                self._request_folder_size(e)
        self._update_size_cells()
        self.status_var.set("正在重新统计文件夹大小…")

    def _update_size_cells(self) -> None:
        for item, e in self.item_entry.items():
            if e.is_dir:
                try:
                    self.tree.set(item, "size", self._size_cell_text(e))
                except Exception:
                    pass
        # 按大小排序时，必须等这一批都算完再重排：否则每来一个结果就把整张表
        # 推倒重建，大目录下界面会一直卡着（看起来就像"黑窗口"）
        if self.sort_key == "size" and not self._size_pending:
            cur = self.selected_entry()
            self.apply_filter(keep_selection=bool(cur),
                              sel_rel=cur.rel if cur else None)
        # 只刷新当前正在看的那个文件，不要因为后台算完了就跳回选中项
        self.update_detail(self._detail_entry)

    # -- 交互 ---------------------------------------------------------------
    def on_double(self, event) -> None:
        if not self.tree.identify_row(event.y):
            return
        # 多选状态（勾了"多选"或按着 Ctrl）禁止双击打开，避免一次启动一堆文件
        if self.multi_var.get() or (getattr(event, "state", 0) & 0x0004):
            return
        self.open_selected()

    def on_right_click(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        self.update_detail()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def on_key(self, event) -> None:
        if event.char in ("0", "1", "2", "3", "4", "5"):
            self.set_rating(int(event.char))
        elif event.char and event.char.lower() == "o":
            self.open_selected()

    def open_selected(self) -> None:
        """双击 / 点『启动项』：设了启动项就用启动项，否则按系统默认方式打开该文件。"""
        e = self.selected_entry() or self._detail_entry
        if e is None:
            return
        self.launch_entry(e)

    def _launch_target(self, entry: Entry) -> tuple:
        """返回 (模式, 目标)。模式 'target' = 用用户设的启动项；'default' = 打开文件本身。"""
        lib_entry = self._lib_entry(entry)
        target = lib_entry.get("launch")
        if target and Path(target).exists():
            return "target", target, bool(lib_entry.get("launch_arg"))
        return "default", str(entry.path), False

    @staticmethod
    def build_launch_command(target: str, file_path, use_arg: bool):
        """程序类（exe/bat…）可以直接带参数启动；其它交给系统打开（返回字符串）。"""
        p = Path(target)
        if use_arg and p.suffix.lower() in {".exe", ".com", ".bat", ".cmd"}:
            return [str(p), str(file_path)]
        return str(p)

    def launch_entry(self, entry: Entry) -> None:
        mode, target, use_arg = self._launch_target(entry)
        try:
            if mode == "target":
                cmd = self.build_launch_command(target, entry.path, use_arg)
                if isinstance(cmd, list):
                    subprocess.Popen(cmd, cwd=str(Path(target).parent))
                else:
                    shell_open(cmd)
                self.status_var.set("已用启动项启动：%s" % Path(target).name)
            else:
                shell_open(entry.path)
                self.status_var.set("已打开：%s" % entry.name)
        except Exception as exc:
            self._open_failed(exc)

    def _open_failed(self, exc) -> None:
        """打不开时给出人话提示（受限环境是最常见的坑）。"""
        messagebox.showwarning(
            "无法打开",
            "%s\n\n"
            "如果这个程序是从开发工具 / 沙箱这类受限环境里启动的，请改成在 "
            "Windows 资源管理器里双击它运行 —— 受限环境下系统不允许它打开别的程序。"
            % exc)

    def choose_launch_program(self, use_arg: bool = False) -> None:
        """给当前文件挑一个启动程序；对话框默认就停在这个文件所在的文件夹里。"""
        e = self.selected_entry() or self._detail_entry
        if e is None or self.folder is None:
            return
        start_dir = e.path.parent if e.path.parent.is_dir() else self.folder
        picked = filedialog.askopenfilename(
            title="为『%s』选择启动程序%s" % (
                e.name, "（启动时把该文件传给它）" if use_arg else "（只启动它）"),
            initialdir=str(start_dir),
            filetypes=[("程序 / 快捷方式", "*.exe *.com *.bat *.cmd *.lnk"),
                       ("所有文件", "*.*")])
        if not picked:
            return
        self._ensure_ident(e)
        self.lib.update(self.folder, e.rel, ident=e.ident, launch=str(Path(picked)),
                        launch_arg=use_arg)
        self.update_detail(self._detail_entry)
        self.status_var.set("已为 %s 设置启动项：%s" % (e.name, Path(picked).name))

    def clear_launch_program(self) -> None:
        e = self.selected_entry() or self._detail_entry
        if e is None or self.folder is None:
            return
        self.lib.update(self.folder, e.rel, ident=e.ident, launch=None, launch_arg=None)
        self.update_detail(self._detail_entry)
        self.status_var.set("已清除 %s 的启动项" % e.name)

    # -- 最近管理的文件夹 ---------------------------------------------------
    def _remember_folder(self, folder: Path, save: bool = True) -> None:
        key = os.path.normcase(str(folder))
        recents = [p for p in self.cfg.get("recent_folders", [])
                   if isinstance(p, str) and os.path.normcase(p) != key]
        recents.insert(0, str(folder))
        self.cfg["recent_folders"] = recents[:RECENT_FOLDERS_MAX]
        if save:
            try:
                save_config(self.cfg)
            except Exception:
                pass

    def forget_recent_folder(self, path: str, save: bool = True) -> None:
        """只从"最近列表"里删掉这条记录 —— 绝不删除或改动那个文件夹。"""
        self.cfg["recent_folders"] = [p for p in self.cfg.get("recent_folders", [])
                                      if p != path]
        if save:
            try:
                save_config(self.cfg)
            except Exception:
                pass

    def _close_recent(self) -> None:
        pop = getattr(self, "_recent_popup", None)
        if pop is not None:
            try:
                pop.destroy()
            except Exception:
                pass
            self._recent_popup = None

    def show_recent_folders(self) -> None:
        """『选择文件夹…』旁边那个 ▼：列出最近管理过的文件夹（只显示文件夹名）。"""
        self._close_recent()
        recents = [p for p in self.cfg.get("recent_folders", []) if isinstance(p, str)]
        pop = tk.Toplevel(self.root)
        pop.overrideredirect(True)
        pop.attributes("-topmost", True)
        pop.withdraw()
        outer = tk.Frame(pop, background="#8A929E")
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, background="#FFFFFF")
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(inner, text="最近管理的文件夹", background="#F3F5F8",
                 foreground="#5B6472", anchor="w", padx=10, pady=4).pack(fill="x")
        if not recents:
            tk.Label(inner, text="（还没有记录）", background="#FFFFFF",
                     foreground="#9AA1AC", anchor="w", padx=12, pady=8).pack(fill="x")
        for path in recents:
            self._recent_row(inner, path)
        pop.update_idletasks()
        w, h = pop.winfo_reqwidth(), pop.winfo_reqheight()
        x = self.recent_btn.winfo_rootx()
        y = self.recent_btn.winfo_rooty() + self.recent_btn.winfo_height() + 2
        sw, sh = pop.winfo_screenwidth(), pop.winfo_screenheight()
        pop.geometry("+%d+%d" % (min(x, max(0, sw - w - 8)), min(y, max(0, sh - h - 40))))
        pop.deiconify()
        pop.focus_set()
        pop.bind("<Escape>", lambda _e: self._close_recent())
        pop.bind("<FocusOut>", lambda _e: self._close_recent())
        self._recent_popup = pop

    def _recent_row(self, parent: tk.Frame, path: str) -> None:
        exists = Path(path).is_dir()
        name = Path(path).name or path
        drive = Path(path).drive or "?"
        row = tk.Frame(parent, background="#FFFFFF")
        row.pack(fill="x")
        # 先显示盘符，这样不同盘里的同名文件夹一眼能分清
        tk.Label(row, text="%s " % drive, background="#FFFFFF", foreground="#5B6472",
                 padx=8, pady=5, font=("Microsoft YaHei UI", 9, "bold")).pack(side="left")
        btn = tk.Button(row, text=name + ("" if exists else "（已不存在）"),
                        anchor="w", relief="flat", bd=0, background="#FFFFFF",
                        activebackground="#EAF2FB", padx=2, pady=5,
                        foreground="#202020" if exists else "#9AA1AC",
                        cursor="hand2" if exists else "arrow",
                        command=lambda: self._choose_recent(path))
        if not exists:
            btn.configure(state="disabled")
        btn.pack(side="left", fill="x", expand=True)
        btn.bind("<Enter>", lambda _e, p=path: self.status_var.set(p))
        forget = tk.Button(row, text="✕", relief="flat", bd=0, background="#FFFFFF",
                           activebackground="#FBE9E9", foreground="#A03A3A",
                           padx=6, pady=5, cursor="hand2",
                           command=lambda: self._ask_forget_recent(path))
        forget.pack(side="right")

    def _choose_recent(self, path: str) -> None:
        self._close_recent()
        self.set_folder(path)

    def _ask_forget_recent(self, path: str) -> None:
        name = Path(path).name or path
        if not messagebox.askyesno(
                "从最近列表移除",
                "只从『最近管理的文件夹』列表里删掉『%s』这条记录，\n"
                "不会删除、移动或改动这个文件夹本身。\n\n继续？" % name,
                parent=self.root):
            return
        self.forget_recent_folder(path)
        self._close_recent()
        self.show_recent_folders()

    def reveal_selected(self) -> None:
        e = self.selected_entry()
        if e is None:
            return
        try:
            if e.is_dir:
                shell_open(e.path)
            else:
                subprocess.Popen(["explorer", "/select,", str(e.path)])
        except Exception as exc:
            self._open_failed(exc)

    def copy_path(self) -> None:
        e = self.selected_entry()
        if e is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(str(e.path))
        self.status_var.set("已复制路径：%s" % e.path)

    # -- 标注操作（只写程序自己的 data 目录） --------------------------------
    def _cover_key(self, entry: Entry) -> str:
        return "%s|%s" % (norm_root(self.folder), norm_rel(entry.rel))

    def choose_cover(self) -> None:
        e = self.selected_entry()
        if e is None:
            messagebox.showinfo("请先选择文件", "请先在列表中选择一个文件。")
            return
        types = [("图片文件", "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tif *.tiff *.ico"),
                 ("所有文件", "*.*")]
        picked = filedialog.askopenfilename(title="选择用作『%s』的图片" % e.name,
                                            filetypes=types)
        if not picked:
            return
        try:
            rel = self.lib.import_cover(Path(picked), self._cover_key(e))
        except Exception as exc:
            messagebox.showerror("无法使用该图片", str(exc))
            return
        self._ensure_ident(e)
        self.lib.update(self.folder, e.rel, ident=e.ident, cover=rel, use_self=None,
                        cover_source=str(picked))
        self.refresh(keep_selection=True)
        self.status_var.set("已为 %s 配置图片" % e.name)

    def use_self_cover(self) -> None:
        e = self.selected_entry()
        if e is None:
            return
        if e.kind != "image":
            messagebox.showinfo("仅适用于图片文件",
                                "『%s』不是图片文件。\n可以点『为该文件设置图片…』另外选一张图片。" % e.name)
            return
        self._ensure_ident(e)
        self.lib.update(self.folder, e.rel, ident=e.ident, use_self=True, cover=None)
        self.refresh(keep_selection=True)
        self.status_var.set("已把 %s 自身设为它的图片" % e.name)

    def clear_cover(self) -> None:
        e = self.selected_entry()
        if e is None:
            return
        self.lib.update(self.folder, e.rel, ident=e.ident, cover=None, use_self=None, cover_source=None)
        self.refresh(keep_selection=True)
        self.status_var.set("已清除 %s 的图片配置" % e.name)

    def set_rating(self, rating: int) -> None:
        """评星：多选/全选时一次给所有选中项打分（只落盘一次）。"""
        items = self.selected_entries()
        if not items:
            return
        for e in items:
            self._ensure_ident(e)
        if len(items) == 1:
            e = items[0]
            self.lib.update(self.folder, e.rel, ident=e.ident, rating=int(rating))
        else:
            self.lib.update_many(self.folder,
                                 [(e.rel, e.ident) for e in items], rating=int(rating))
        for e in items:
            self.update_row(e)
        self.update_stars(int(rating))
        if len(items) > 1:
            self.status_var.set("已为 %d 个文件评分 %d 星" % (len(items), rating))
        else:
            self.status_var.set("已为 %s 评分 %d 星" % (items[0].name, rating))

    def save_note(self) -> None:
        e = self.selected_entry()
        if e is None:
            return
        self._ensure_ident(e)
        self.lib.update(self.folder, e.rel, ident=e.ident, note=self.note_var.get().strip())
        self.update_row(e)
        self.status_var.set("已保存 %s 的备注" % e.name)

    def update_row(self, entry: Entry) -> None:
        for item, e in self.item_entry.items():
            if e is entry or e.rel == entry.rel:
                lib_entry = self._lib_entry(e)
                self.tree.item(item, values=(
                    self._size_cell_text(e),
                    TYPE_LABEL.get(e.kind, ""),
                    human_time(e.mtime),
                    stars_text(int(lib_entry.get("rating", 0) or 0))))
                return

    # -- 自动刷新 -----------------------------------------------------------
    def _schedule_auto(self) -> None:
        if self._auto_job:
            try:
                self.root.after_cancel(self._auto_job)
            except Exception:
                pass
            self._auto_job = None
        self.cfg["auto_refresh"] = bool(self.auto_var.get())
        try:
            save_config(self.cfg)
        except Exception:
            pass
        if self.auto_var.get():
            self._auto_job = self.root.after(self.AUTO_REFRESH_MS, self._auto_tick)

    def _auto_tick(self) -> None:
        self._auto_job = None
        if not self._alive():
            return
        try:
            if self.folder and self.auto_var.get():
                fresh = scan_folder(self.folder, skip=data_dir())
                if self._signature(fresh) != self._sig:
                    sel = self.selected_entry()
                    self.entries = fresh
                    self._sig = self._signature(fresh)
                    self.apply_filter(keep_selection=bool(sel),
                                      sel_rel=sel.rel if sel else None)
        finally:
            if self.auto_var.get() and self._alive():
                self._auto_job = self.root.after(self.AUTO_REFRESH_MS, self._auto_tick)

    # -- 其它 ---------------------------------------------------------------
    def show_safety(self) -> None:
        messagebox.showinfo("安全说明", (
            "本程序只做只读扫描和预览，绝不会对你的文件执行：\n\n"
            "  • 删除    • 移动    • 重命名\n"
            "  • 解压压缩包    • 整理目录\n"
            "  • 修改内容    • 覆盖文件    • 批量改结构\n\n"
            "你配置的图片会被复制一份到程序自己的 data\\covers 目录，\n"
            "评分与备注写在 data\\library.json。\n"
            "你的文件内容、位置、名称、时间戳都不会改变。"
        ))

    # -- 安全设置 / 格式化 ---------------------------------------------------
    def show_safety(self) -> None:
        """『安全设置』：安全承诺 + 格式化（恢复出厂设置，需先把锁关上）。"""
        old = getattr(self, "_safety_win", None)
        if old is not None:
            try:
                old.lift()
                old.focus_set()
                return
            except Exception:
                self._safety_win = None
        win = tk.Toplevel(self.root)
        self._safety_win = win
        win.title("安全设置")
        try:
            win.transient(self.root)
        except Exception:
            pass
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="只读模式只做扫描和预览，绝不会对你的文件执行：",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        ttk.Label(body, justify="left", text=(
            "　• 删除　　• 移动　　• 重命名\n"
            "　• 解压压缩包　　• 整理目录\n"
            "　• 修改内容　　• 覆盖文件　　• 批量改变目录结构")).pack(anchor="w", pady=(6, 0))
        ttk.Label(body, justify="left", text=(
            "你配置的图片会被复制一份到程序自己的 data\\covers 目录，\n"
            "评分、备注、启动项写在 data\\library.json。\n"
            "你的文件内容、位置、名称、时间戳都不会改变。")).pack(anchor="w", pady=(8, 0))
        ttk.Label(body, justify="left", foreground="#5B6472", text=(
            "版本：v%s　·　当前模式：%s　·　修改用户文件的能力（修改模式）尚未实现"
            % (APP_VERSION, mode_label()))).pack(anchor="w", pady=(8, 0))
        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=12)

        row = ttk.Frame(body)
        row.pack(fill="x")
        row.columnconfigure(1, weight=1)
        # 锁：默认上锁（🔒）= 不可用；点开锁（🔓）= 可用。
        # 锁和『格式化』同一行（第 0 行）→ 两者垂直居中对齐；『导出』在下一行同列对齐。
        self._fmt_locked = True
        self._lock_btn = ttk.Button(row, text="🔒", width=4,
                                    command=self._toggle_format_lock)
        self._lock_btn.grid(row=0, column=0, sticky="w")
        self._fmt_btn = ttk.Button(row, text="格式化（恢复出厂设置）", width=24,
                                   state="disabled", command=self._ask_format)
        self._fmt_btn.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self._export_btn = ttk.Button(row, text="导出", width=24,
                                      command=lambda: self.export_selected(
                                          self._safety_win or self.root))
        self._export_btn.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Button(body, text="关闭", command=self._close_safety).pack(anchor="e", pady=(16, 0))
        win.protocol("WM_DELETE_WINDOW", self._close_safety)
        win.update_idletasks()
        try:
            x = self.root.winfo_rootx() + 80
            y = self.root.winfo_rooty() + 60
            win.geometry("+%d+%d" % (max(0, x), max(0, y)))
        except Exception:
            pass

    def _close_safety(self) -> None:
        win = getattr(self, "_safety_win", None)
        self._safety_win = None
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def _toggle_format_lock(self) -> None:
        """默认上锁（🔒）→ 『格式化』灰掉不可点；点一下开锁（🔓）→ 可以点。"""
        self._fmt_locked = not getattr(self, "_fmt_locked", True)
        try:
            self._lock_btn.configure(text="🔒" if self._fmt_locked else "🔓")
            self._fmt_btn.configure(state="disabled" if self._fmt_locked else "normal")
        except Exception:
            pass

    def _ask_format(self) -> None:
        if getattr(self, "_fmt_locked", True):
            return
        if not messagebox.askyesno(
                "确认格式化",
                "真的要格式化吗？\n\n"
                "这会删除程序自己的全部数据：\n"
                "　评分、备注、封面配置、启动项、缩略图缓存、最近打开的文件夹。\n\n"
                "【不会】删除、移动或修改你的任何文件。\n\n"
                "此操作无法撤销，确定继续？",
                icon="warning", default="no", parent=self.root):
            return
        self.format_app_data_action()

    def format_app_data_action(self) -> None:
        """执行格式化：删掉 data 目录内容，并把内存里的状态复位。"""
        try:
            removed = format_app_data()
        except Exception as exc:
            messagebox.showerror("格式化失败", str(exc), parent=self.root)
            return
        self.lib = Library()
        self._size_cache.clear()
        self._size_verified.clear()
        self._size_pending.clear()
        self._size_wanted.clear()
        self._id_cache.clear()
        self._id_pending.clear()
        self._id_wanted.clear()
        self._thumb_cache.clear()
        reset_photo_cache()
        self.cfg = load_config()
        self._selected_index = None
        if self.folder:
            self.refresh()
        self.update_detail(None)
        try:
            self._fmt_locked = True
            self._lock_btn.configure(text="🔒")
            self._fmt_btn.configure(state="disabled")
        except Exception:
            pass
        messagebox.showinfo(
            "格式化完成",
            "已删除 %d 个程序数据文件（标注、封面副本、缓存、配置）。\n"
            "你的文件一个都没有被改动。" % removed, parent=self.root)
        self.status_var.set("已格式化程序数据（恢复出厂设置）")

    def on_close(self) -> None:
        self._closing = True          # 先立旗：定时回调不许再排队
        self._close_recent()
        for job in ("_auto_job", "_size_job", "_hover_job", "_pool_job", "_id_job"):
            handle = getattr(self, job, None)
            if handle:
                try:
                    self.root.after_cancel(handle)
                except Exception:
                    pass
                setattr(self, job, None)
        try:
            self.cfg["last_folder"] = str(self.folder) if self.folder else self.cfg.get("last_folder")
            self.cfg["auto_refresh"] = bool(self.auto_var.get())
            self.cfg["geometry"] = self.root.geometry()
            save_config(self.cfg)
        except Exception:
            pass
        try:
            self._save_size_cache(force=True)      # 文件夹大小结果存盘
        except Exception:
            pass
        try:
            self._save_id_cache(force=True)        # 文件身份缓存存盘
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 9. 修改模式（预留接口 —— 只读模式下这些函数一律先被闸门挡住）
# ---------------------------------------------------------------------------
# 说明：这些骨架现在**不会被任何界面调用**，存在的意义是把"将来要加的东西"
# 的入口、顺序和纪律先定下来：
#     ① ensure_edit_mode()  → ② guard_user_write() → ③ record_operation()
# 无论以后加多少个编辑功能，都必须照这个顺序走，这样"改动用户文件"这件事
# 永远是集中、可见、可追溯的。
def rename_entry(path, new_name: str):
    """【预留】重命名一个用户文件/文件夹。"""
    guard_user_write(path, "重命名文件")
    record_operation("rename", {"path": str(path), "to": str(new_name)})
    raise NotImplementedError("修改模式的重命名功能尚未实现")


def move_entries(paths, target_dir):
    """【预留】把若干用户文件移动到另一个目录。"""
    for p in paths:
        guard_user_write(p, "移动文件")
    record_operation("move", {"paths": [str(p) for p in paths],
                              "target": str(target_dir)})
    raise NotImplementedError("修改模式的移动功能尚未实现")


def delete_entries(paths):
    """【预留】删除若干用户文件（真删除需要回收站 + 二次确认，绝不静默删）。"""
    for p in paths:
        guard_user_write(p, "删除文件")
    record_operation("delete", {"paths": [str(p) for p in paths]})
    raise NotImplementedError("修改模式的删除功能尚未实现")


def create_folder(parent, name: str):
    """【预留】在某个目录下新建文件夹。"""
    guard_user_write(parent, "新建文件夹")
    record_operation("mkdir", {"parent": str(parent), "name": str(name)})
    raise NotImplementedError("修改模式的新建文件夹功能尚未实现")


# ---------------------------------------------------------------------------
# 10. 示例素材与命令行
# ---------------------------------------------------------------------------
def make_demo(folder) -> None:
    """生成一个示例文件夹（只在指定目录内创建新文件，不触碰已有文件）。"""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    presets = [
        ("封面-海边.png", (56, 148, 214), (255, 214, 128)),
        ("封面-森林.png", (34, 110, 82), (168, 224, 132)),
        ("封面-晚霞.png", (196, 88, 108), (255, 196, 120)),
        ("封面-夜空.png", (38, 48, 96), (120, 140, 220)),
    ]
    for name, top, bottom in presets:
        S = 2
        pm = Pixmap(640 * S, 420 * S)
        pm.vgradient(0, 0, 640 * S, 420 * S, top, bottom)
        pm.disc(640 * S * 0.74, 420 * S * 0.26, 46 * S, (255, 246, 214))
        pm.tri((0, 420 * S), (200 * S, 210 * S), (400 * S, 420 * S), tuple(
            max(0, int(c * 0.72)) for c in bottom))
        pm.tri((270 * S, 420 * S), (450 * S, 250 * S), (640 * S, 420 * S), tuple(
            max(0, int(c * 0.55)) for c in bottom))
        pm.rect(0, 400 * S, 640 * S, 420 * S, tuple(
            max(0, int(c * 0.45)) for c in bottom))
        (folder / name).write_bytes(png_bytes(pm.downscale(S)))
    (folder / "会议记录.txt").write_text(
        "这是示例文本文件，用来演示：即使不是图片，也能给它配置一张图片作为封面。\n",
        encoding="utf-8")
    (folder / "安装包.zip").write_text("示例压缩包占位文件（本程序绝不会解压它）。\n",
                                       encoding="utf-8")
    (folder / "demo_script.py").write_text(
        "# 示例代码文件\nprint('hello, 只读文件管理器')\n", encoding="utf-8")
    sub = folder / "示例子文件夹"
    sub.mkdir(exist_ok=True)
    (sub / "说明.txt").write_text("子文件夹只是展示用；程序只管理选中的那一层。\n",
                                  encoding="utf-8")


def enable_dpi_awareness() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="简易图片标注文件管理器（只读安全模式）")
    parser.add_argument("--folder", help="启动时直接管理的文件夹")
    parser.add_argument("--make-demo", metavar="DIR", help="生成示例素材后退出")
    parser.add_argument("--smoke", type=int, default=0, metavar="MS",
                        help="冒烟测试：启动界面，MS 毫秒后自动退出")
    parser.add_argument("--selfcheck", action="store_true", help="打印安全约束检查结果")
    parser.add_argument("--version", action="store_true", help="打印版本号")
    args = parser.parse_args(argv)

    if args.version:
        print("%s v%s（%s）" % (APP_NAME, APP_VERSION, mode_label()))
        return 0

    if args.make_demo:
        make_demo(args.make_demo)
        print("示例素材已生成：%s" % args.make_demo)
        return 0
    if args.selfcheck:
        print("版本：%s v%s（%s）" % (APP_NAME, APP_VERSION, mode_label()))
        print("数据目录：%s" % data_dir())
        print("Pillow：%s" % (PIL_VERSION if HAS_PIL else "未安装（PNG/GIF 仍可预览）"))
        print("允许写入的根目录：%s" % get_write_root())
        return 0

    enable_dpi_awareness()
    root = tk.Tk()
    root.title(APP_TITLE)
    root.minsize(940, 560)
    app = App(root, folder=args.folder, smoke_ms=args.smoke)
    geo = "1200x700"
    if app.cfg.get("geometry") and not args.smoke:
        geo = app.cfg["geometry"]
    try:
        root.geometry(geo)
    except Exception:
        root.geometry("1200x700")
    app.reveal()          # 界面搭好、位置定好后再显示，避免启动时黑一下
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
