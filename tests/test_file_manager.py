# -*- coding: utf-8 -*-
"""
针对 file_manager.py 的安全性与功能测试。

重点证明：
  1) 扫描、评分、备注、配置封面等操作**不会**改变用户文件的内容、
     修改时间、大小或所在目录的结构；
  2) 所有写操作都被限制在程序自己的数据目录内（guard_app_write）；
  3) 源码中不存在删除 / 移动 / 重命名 / 解压类调用。

运行：  python -m unittest discover -s tests -v
"""

import ast
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import file_manager as fm  # noqa: E402

DEFAULT_ROOT = fm.get_write_root()


def snapshot(folder: Path) -> dict:
    """记录目录结构 + 每个文件的内容哈希 / 大小 / 修改时间。"""
    snap = {}
    for p in sorted(folder.rglob("*")):
        rel = str(p.relative_to(folder))
        if p.is_dir():
            snap[rel] = ("dir",)
        else:
            data = p.read_bytes()
            st = p.stat()
            snap[rel] = (hashlib.sha256(data).hexdigest(), st.st_size, st.st_mtime_ns)
    return snap


def make_png(path: Path, w: int = 64, h: int = 48) -> Path:
    """用纯标准库生成一张 PNG（不需要 Pillow）。"""
    pm = fm.Pixmap(w, h)
    pm.vgradient(0, 0, w, h, (60, 120, 200), (240, 200, 120))
    pm.disc(w * 0.5, h * 0.5, min(w, h) * 0.2, (255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(fm.png_bytes(pm))
    return path


class BaseCase(unittest.TestCase):
    def setUp(self):
        # 临时目录放在系统临时区，并在被管理的项目目录里不留垃圾。
        # 注意：这里特意用普通 mkdir（而不是 tempfile.mkdtemp / TemporaryDirectory）——
        # mkdtemp 会创建 0700 的受限目录，在受管（沙箱）环境下其子目录不可写；
        # 也不依赖自动清理，因为删除子目录可能被环境拒绝，失败只是打印无害报错。
        base = Path(tempfile.gettempdir())
        self.tmp = base / ("fmtest_%d_%d" % (os.getpid(), time.time_ns() % 10 ** 9))
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.managed = self.tmp / "managed"
        self.managed.mkdir()
        self.appdata = self.tmp / "appdata"
        self.appdata.mkdir()
        fm.set_write_root(self.appdata)
        fm.ensure_data_dirs()

    def tearDown(self):
        fm.set_write_root(DEFAULT_ROOT)
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestNoUserFileChanges(BaseCase):
    def test_scan_and_annotations_leave_files_untouched(self):
        (self.managed / "报告.txt").write_text("原始内容", encoding="utf-8")
        (self.managed / "数据.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        make_png(self.managed / "照片.png")
        make_png(self.managed / "封面素材.png")
        (self.managed / "子目录").mkdir()
        (self.managed / "子目录" / "内部.txt").write_text("x", encoding="utf-8")

        before = snapshot(self.managed)

        # 1) 扫描（只读）
        entries = fm.scan_folder(self.managed, skip=fm.data_dir())
        self.assertEqual(len(entries), 5)  # 子目录 + 4 个文件

        # 2) 评分 / 备注 / 配置封面（封面源文件就在被管理目录里）
        lib = fm.Library()
        lib.update(self.managed, "报告.txt", rating=5, note="季度总结")
        rel = lib.import_cover(self.managed / "封面素材.png",
                               "%s|报告.txt" % fm.norm_root(self.managed))
        lib.update(self.managed, "报告.txt", cover=rel)
        lib.update(self.managed, "数据.csv", use_self=True)

        after = snapshot(self.managed)
        self.assertEqual(before, after, "用户文件被改动了！")

        # 3) 标注确实被保存了，而且保存在程序自己的数据目录里
        lib2 = fm.Library()
        e = lib2.entry(self.managed, "报告.txt")
        self.assertEqual(e.get("rating"), 5)
        self.assertEqual(e.get("note"), "季度总结")
        self.assertTrue(lib2.cover_path(e).is_file())
        self.assertTrue(str(lib2.cover_path(e)).startswith(str(self.appdata)))

    def test_cover_import_copies_and_keeps_source(self):
        src = make_png(self.managed / "原图.png")
        before = snapshot(self.managed)
        lib = fm.Library()
        rel = lib.import_cover(src, "key-1")
        dest = self.appdata / rel
        self.assertTrue(dest.is_file(), "封面副本没有落到程序数据目录")
        self.assertEqual(snapshot(self.managed), before, "源文件被改动了！")
        self.assertNotEqual(src.resolve(), dest.resolve())

    def test_rating_does_not_touch_mtime(self):
        f = self.managed / "a.txt"
        f.write_text("hello", encoding="utf-8")
        st_before = f.stat()
        fm.Library().update(self.managed, "a.txt", rating=3)
        st_after = f.stat()
        self.assertEqual((st_before.st_size, st_before.st_mtime_ns),
                         (st_after.st_size, st_after.st_mtime_ns))


class TestWriteGuard(BaseCase):
    def test_guard_blocks_paths_outside_data_dir(self):
        with self.assertRaises(fm.SafetyError):
            fm.guard_app_write(self.managed / "evil.txt")
        with self.assertRaises(fm.SafetyError):
            fm.atomic_write_text(self.managed / "evil.txt", "x")
        with self.assertRaises(fm.SafetyError):
            fm.guard_app_write(Path.home() / "evil.txt")

    def test_no_stray_files_created_in_managed_folder(self):
        (self.managed / "x.txt").write_text("x", encoding="utf-8")
        fm.scan_folder(self.managed)
        fm.Library().update(self.managed, "x.txt", rating=1)
        self.assertEqual([p.name for p in self.managed.iterdir()], ["x.txt"])


class TestLibrary(BaseCase):
    def test_roundtrip_and_clear(self):
        lib = fm.Library()
        lib.update(self.managed, "f.txt", rating=4, note="备注")
        lib2 = fm.Library()
        self.assertEqual(lib2.entry(self.managed, "f.txt").get("rating"), 4)
        self.assertEqual(lib2.entry(self.managed, "f.txt").get("note"), "备注")
        lib2.update(self.managed, "f.txt", rating=0, note="")
        self.assertEqual(fm.Library().entry(self.managed, "f.txt"), {})

    def test_library_json_lives_in_data_dir(self):
        fm.Library().update(self.managed, "f.txt", rating=2)
        self.assertTrue(fm.library_file().is_file())
        data = json.loads(fm.library_file().read_text(encoding="utf-8"))
        self.assertIn("folders", data)
        self.assertIn(fm.norm_root(self.managed), data["folders"])

    def test_corrupt_library_is_backed_up_not_crashed(self):
        fm.library_file().write_text("{ this is not json", encoding="utf-8")
        lib = fm.Library()
        self.assertEqual(lib.entry(self.managed, "f.txt"), {})
        backups = list(self.appdata.glob("library.json.bad-*"))
        self.assertEqual(len(backups), 1)


class TestImageHandling(BaseCase):
    def test_import_cover_rejects_non_image(self):
        bad = self.managed / "note.txt"
        bad.write_text("我不是图片", encoding="utf-8")
        with self.assertRaises(ValueError):
            fm.Library().import_cover(bad, "k")

    def test_cover_path_and_kind_rules(self):
        png = make_png(self.managed / "pic.png")
        (self.managed / "doc.txt").write_text("x", encoding="utf-8")
        lib = fm.Library()
        self.assertIsNone(fm.load_photo(None, 64)[0])
        entries = {e.name: e for e in fm.scan_folder(self.managed)}
        rel = lib.import_cover(png, "k2")
        lib.update(self.managed, "doc.txt", cover=rel)
        cover = lib.cover_path(lib.entry(self.managed, "doc.txt"))
        self.assertTrue(cover and cover.is_file())
        self.assertEqual(entries["pic.png"].kind, "image")
        self.assertEqual(entries["doc.txt"].kind, "doc")


class TestFolderSize(BaseCase):
    """文件夹大小统计：数字要对，而且同样只读。"""

    def test_measure_folder_sums_files_recursively(self):
        (self.managed / "a.bin").write_bytes(b"x" * 1000)
        sub = self.managed / "sub"
        sub.mkdir()
        (sub / "b.bin").write_bytes(b"y" * 2345)
        deep = sub / "deep"
        deep.mkdir()
        (deep / "c.bin").write_bytes(b"z" * 10)
        before = snapshot(self.managed)
        size, truncated = fm.measure_folder(self.managed)
        self.assertEqual(size, 1000 + 2345 + 10)
        self.assertFalse(truncated)
        self.assertEqual(snapshot(self.managed), before, "统计文件夹大小改动了文件！")

    def test_measure_folder_respects_caps(self):
        for i in range(5):
            (self.managed / ("f%d.bin" % i)).write_bytes(b"x" * 10)
        size, truncated = fm.measure_folder(self.managed, cap_files=2)
        self.assertTrue(truncated)
        self.assertLessEqual(size, 50)

    def test_measure_missing_folder_is_safe(self):
        self.assertEqual(fm.measure_folder(self.managed / "不存在"), (0, False))

    def test_folder_size_shows_in_list(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        sub = self.managed / "子文件夹"
        sub.mkdir()
        (sub / "x.bin").write_bytes(b"x" * 4096)
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.managed), reveal=False)
        try:
            root.update()
            item = None
            for it, e in app.item_entry.items():
                if e.is_dir:
                    item = it
            self.assertIsNotNone(item, "列表里应有子文件夹")
            entry = app.item_entry[item]
            app._request_folder_size(entry)
            for _ in range(60):                     # 等后台线程算完
                app._drain_sizes()
                root.update()
                if str(entry.path) in app._size_cache:
                    break
                time.sleep(0.05)
            self.assertIn(str(entry.path), app._size_cache)
            self.assertEqual(app._size_cache[str(entry.path)][0], 4096)
            self.assertEqual(app._size_cell_text(entry), "4.0 KB")
        finally:
            app.on_close()


    def test_folder_size_cache_survives_restart_and_folder_switch(self):
        """换了文件夹再切回来、甚至重启程序，都要能直接用上次的结果（秒显）。"""
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        sub = self.managed / "子文件夹"
        sub.mkdir()
        (sub / "x.bin").write_bytes(b"x" * 4096)
        other = self.tmp / "另一个文件夹"
        other.mkdir()

        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.managed), reveal=False)
        try:
            root.update()
            entry = [e for e in app.visible if e.is_dir][0]
            app._request_folder_size(entry)
            for _ in range(60):
                app._drain_sizes()
                root.update()
                if str(entry.path) in app._size_verified:
                    break
                time.sleep(0.05)
            self.assertIn(str(entry.path), app._size_verified)
            self.assertEqual(app._size_cache[str(entry.path)][0], 4096)
            self.assertTrue(fm.cache_dir().joinpath(fm.SIZE_CACHE_NAME).is_file(),
                            "统计结果没有存盘")

            # 切到别的文件夹再切回来：缓存不能被清掉
            app.set_folder(str(other))
            app.set_folder(str(self.managed))
            root.update()
            key = str(sub)
            self.assertIn(key, app._size_cache, "切换文件夹把缓存清掉了")
            self.assertEqual(app._size_cell_text(app.visible[0]), "4.0 KB")
        finally:
            app.on_close()

        # 模拟"重启程序"：新的 App 应该立刻从磁盘缓存拿到大小，无需等后台线程
        root2 = tk.Tk()
        root2.withdraw()
        app2 = fm.App(root2, folder=str(self.managed), reveal=False)
        try:
            root2.update()
            entry2 = [e for e in app2.visible if e.is_dir][0]
            self.assertIn(str(entry2.path), app2._size_cache, "重启后没有读回缓存")
            self.assertEqual(app2._size_cell_text(entry2), "4.0 KB")
        finally:
            app2.on_close()

    def test_size_cache_file_is_plain_json_inside_data_dir(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        (self.managed / "sub").mkdir()
        (self.managed / "sub" / "a.bin").write_bytes(b"x" * 100)
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.managed), reveal=False)
        try:
            root.update()
            app._request_folder_size([e for e in app.visible if e.is_dir][0])
            for _ in range(60):
                app._drain_sizes()
                root.update()
                if app._size_cache:
                    break
                time.sleep(0.05)
            app._save_size_cache(force=True)
            path = fm.cache_dir() / fm.SIZE_CACHE_NAME
            self.assertTrue(str(path).startswith(str(self.appdata)))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("sizes", data)
            self.assertEqual(data["version"], 1)
        finally:
            app.on_close()


    def test_detail_panel_scrolls_when_window_is_short(self):
        """窗口压矮时，详情面板必须能用滚轮往下滚（否则底部按钮看不见）。"""
        sub = self.managed / "子文件夹"
        sub.mkdir()
        (sub / "x.bin").write_bytes(b"x" * 4096)
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.managed), reveal=False)
        try:
            root.geometry("1000x460")        # 故意压得很矮
            root.update()
            app.update_detail([e for e in app.visible if e.is_dir][0])
            root.update()
            box = app.detail_canvas.bbox("all")
            self.assertIsNotNone(box)
            self.assertGreater(box[3] - box[1], app.detail_canvas.winfo_height(),
                               "面板内容没有超出可视区，测试前提不成立")

            class _Wheel:
                delta = -120

            before = app.detail_canvas.yview()
            for _ in range(4):
                app._on_detail_wheel(_Wheel())
            root.update()
            self.assertNotEqual(before, app.detail_canvas.yview(), "滚轮没有生效")
        finally:
            app.on_close()


    def test_folder_sizes_are_deferred_until_after_reveal(self):
        """窗口还没画到屏幕上之前，绝对不许开始读盘（这正是启动黑屏的根因）。"""
        sub = self.managed / "大文件夹"
        sub.mkdir()
        (sub / "a.bin").write_bytes(b"x" * 10)
        root = tk.Tk()
        app = fm.App(root, folder=str(self.managed), reveal=True)   # 会先把窗口藏起来
        try:
            self.assertFalse(app._sizes_enabled, "窗口还没显示就开始统计了")
            self.assertEqual(app._size_active, 0, "窗口还没显示就有读盘线程了")
            self.assertTrue(list(app._size_wanted), "文件夹没有进入待统计队列")
            app._enable_folder_sizes()
            self.assertTrue(app._sizes_enabled)
            self.assertLessEqual(app._size_active, fm.SIZE_MAX_WORKERS)
        finally:
            app._closing = True
            try:
                app.root.destroy()
            except Exception:
                pass

    def test_sizes_are_collected_after_starting_without_a_folder(self):
        """回归测试：以"没有文件夹"启动时，第一次轮询会空转停下；

        之后打开文件夹，算好的大小**仍然必须被收回来**（曾经会永远显示"计算中…"）。
        """
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        sub = self.managed / "子文件夹"
        sub.mkdir()
        (sub / "a.bin").write_bytes(b"x" * 1000)
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=None, reveal=False)       # 关键：没有文件夹启动
        try:
            deadline = time.time() + 1.2                    # 让第一次空转的轮询跑过去
            while time.time() < deadline:
                root.update()
                time.sleep(0.02)
            app.set_folder(str(self.managed))
            for _ in range(80):
                root.update()
                time.sleep(0.05)
                if app._size_cache:
                    break
            entry = [e for e in app.visible if e.is_dir][0]
            self.assertIn(str(entry.path), app._size_cache,
                          "结果又堆在队列里没人收了（收取定时器没被唤醒）")
            self.assertNotEqual(app._size_cell_text(entry), "计算中…")
        finally:
            app.on_close()

    def test_size_workers_are_capped(self):
        """再多的子文件夹也只能同时跑 SIZE_MAX_WORKERS 个线程。"""
        for i in range(8):
            (self.managed / ("d%d" % i)).mkdir()
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.managed), reveal=False)
        try:
            root.update()
            self.assertLessEqual(app._size_active, fm.SIZE_MAX_WORKERS)
            self.assertGreater(len(app._size_wanted) + app._size_active, 0)
        finally:
            app.on_close()


class TestLaunchTarget(BaseCase):
    """启动项：设了就用启动项，没设就按系统默认方式打开文件本身。"""

    def setUp(self):
        super().setUp()
        self.victim = self.managed / "素材.bin"
        self.victim.write_bytes(b"data")
        self.prog = self.tmp / "player.exe"
        self.prog.write_bytes(b"MZ")           # 只是当个"程序路径"用，不会真的执行

    def _app(self):
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.managed), reveal=False)
        root.update()
        return root, app

    def test_default_is_the_file_itself(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        root, app = self._app()
        try:
            entry = [e for e in app.visible if e.name == "素材.bin"][0]
            mode, target, use_arg = app._launch_target(entry)
            self.assertEqual(mode, "default")
            self.assertEqual(target, str(self.victim))
            self.assertFalse(use_arg)
        finally:
            app.on_close()

    def test_launch_target_wins_when_set(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        root, app = self._app()
        try:
            item = [i for i, e in app.item_entry.items() if e.name == "素材.bin"][0]
            app.tree.selection_set(item)
            root.update()
            entry = app.selected_entry()
            app.lib.update(self.managed, entry.rel, launch=str(self.prog))
            mode, target, use_arg = app._launch_target(entry)
            self.assertEqual(mode, "target")
            self.assertEqual(target, str(self.prog))
            self.assertFalse(use_arg)
            # 带参数模式
            app.lib.update(self.managed, entry.rel, launch=str(self.prog), launch_arg=True)
            self.assertTrue(app._launch_target(entry)[2])
            # 清除后回到默认
            app.clear_launch_program()
            self.assertEqual(app._launch_target(entry)[0], "default")
            self.assertNotIn("launch", app.lib.entry(self.managed, entry.rel))
        finally:
            app.on_close()

    def test_missing_target_falls_back_to_default(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        root, app = self._app()
        try:
            entry = [e for e in app.visible if e.name == "素材.bin"][0]
            app.lib.update(self.managed, entry.rel, launch=str(self.tmp / "不存在.exe"))
            mode, target, _arg = app._launch_target(entry)
            self.assertEqual(mode, "default", "启动项失效时应回退到直接打开文件")
            self.assertEqual(target, str(self.victim))
        finally:
            app.on_close()

    def test_build_launch_command(self):
        f = str(self.victim)
        self.assertEqual(fm.App.build_launch_command(str(self.prog), f, True),
                         [str(self.prog), f])
        self.assertEqual(fm.App.build_launch_command(str(self.prog), f, False),
                         str(self.prog))
        lnk = str(self.tmp / "快捷方式.lnk")
        self.assertEqual(fm.App.build_launch_command(lnk, f, True), lnk)

    def test_setting_launch_target_does_not_touch_files(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        before = snapshot(self.managed)
        root, app = self._app()
        try:
            item = [i for i, e in app.item_entry.items() if e.name == "素材.bin"][0]
            app.tree.selection_set(item)
            root.update()
            entry = app.selected_entry()
            app.lib.update(self.managed, entry.rel, launch=str(self.prog), launch_arg=True)
            app.update_detail(entry)
            root.update()
        finally:
            app.on_close()
        self.assertEqual(snapshot(self.managed), before, "设置启动项改动了用户文件！")


class TestRecentFolders(BaseCase):
    """最近管理的文件夹：只记历史，删记录绝不碰文件夹。"""

    def setUp(self):
        super().setUp()
        self.folders = []
        for i in range(15):
            d = self.tmp / ("目录%02d" % i)
            d.mkdir()
            (d / "keep.txt").write_text("keep", encoding="utf-8")
            self.folders.append(d)

    def test_history_order_dedupe_and_cap(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.folders[0]), reveal=False)
        try:
            for d in self.folders[1:]:
                app._remember_folder(d, save=False)
            app._remember_folder(self.folders[3], save=False)   # 重新访问 → 提到最前
            recents = app.cfg["recent_folders"]
            self.assertEqual(recents[0], str(self.folders[3]))
            self.assertEqual(len(recents), fm.RECENT_FOLDERS_MAX)
            self.assertEqual(len(set(recents)), len(recents), "出现了重复记录")
        finally:
            app.on_close()

    def test_forgetting_a_recent_folder_never_touches_disk(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        target = self.folders[2]
        before = snapshot(target)
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.folders[0]), reveal=False)
        try:
            app._remember_folder(target, save=False)
            self.assertIn(str(target), app.cfg["recent_folders"])
            app.forget_recent_folder(str(target), save=False)
            self.assertNotIn(str(target), app.cfg["recent_folders"])
        finally:
            app.on_close()
        self.assertTrue(target.is_dir(), "删历史记录把文件夹删掉了！")
        self.assertEqual(snapshot(target), before, "删历史记录改动了文件夹内容！")

    def test_recent_list_survives_restart_and_is_plain_paths(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.folders[1]), reveal=False)
        app.on_close()
        cfg = json.loads(fm.config_file().read_text(encoding="utf-8"))
        self.assertIn("recent_folders", cfg)
        self.assertEqual(cfg["recent_folders"][0], str(self.folders[1]))
        # 新的 App 能读到
        root2 = tk.Tk()
        root2.withdraw()
        app2 = fm.App(root2, folder=str(self.folders[0]), reveal=False)
        try:
            self.assertIn(str(self.folders[1]), app2.cfg["recent_folders"])
        finally:
            app2.on_close()


class TestFormatData(BaseCase):
    """"格式化"只清程序自己的数据，绝不碰用户文件。"""

    def setUp(self):
        super().setUp()
        # 保险丝：格式化只允许作用在测试的临时数据目录上
        self.assertTrue(str(fm.get_write_root()).startswith(str(self.tmp)),
                        "测试必须在隔离的数据目录里跑，绝不能碰真实 data 目录")

    def _prepare_app_data(self):
        (self.managed / "重要.txt").write_text("我的数据", encoding="utf-8")
        (self.managed / "子目录").mkdir()
        (self.managed / "子目录" / "内部.bin").write_bytes(b"x" * 100)
        fm.ensure_data_dirs()
        fm.Library().update(self.managed, "重要.txt", rating=5, note="别删")
        (fm.covers_dir() / "cover.png").write_bytes(b"fake")
        (fm.cache_dir() / "thumb.png").write_bytes(b"fake")
        fm.save_config({"last_folder": str(self.managed)})
        return snapshot(self.managed)

    def test_format_removes_app_data_only(self):
        before = self._prepare_app_data()
        self.assertTrue(fm.library_file().is_file())
        removed = fm.format_app_data()
        self.assertGreaterEqual(removed, 4, "应该删掉标注/配置/封面/缩略图")
        self.assertFalse(fm.library_file().exists())
        self.assertFalse(fm.config_file().exists())
        self.assertEqual(list(fm.covers_dir().glob("*")), [])
        self.assertEqual(list(fm.cache_dir().glob("*")), [])
        self.assertEqual(snapshot(self.managed), before, "格式化动了用户文件！")

    def test_format_on_empty_data_dir_is_safe(self):
        self.assertEqual(fm.format_app_data(), 0)
        self.assertTrue(self.managed.is_dir())

    def test_safety_dialog_holds_export_aligned_with_format(self):
        """『导出』在安全设置对话框里，和『格式化』左对齐；主窗口工具栏不再有它。"""
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        self._prepare_app_data()
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.managed), reveal=False)
        try:
            root.update()
            texts = []

            def walk(w, skip_toplevel=True):
                for c in w.winfo_children():
                    if skip_toplevel and isinstance(c, tk.Toplevel):
                        continue
                    try:
                        texts.append(str(c.cget("text")))
                    except Exception:
                        pass
                    walk(c, skip_toplevel)

            walk(app.root)
            self.assertNotIn("导出", texts, "导出不该再占着工具栏（会把绿字挤出去）")

            app.show_safety()
            root.update()
            self.assertEqual(str(app._export_btn.cget("state")), "normal")
            self.assertEqual(str(app._fmt_btn.cget("state")), "disabled",
                             "默认上锁时格式化应不可点")
            fmt_grid = app._fmt_btn.grid_info()
            exp_grid = app._export_btn.grid_info()
            lock_grid = app._lock_btn.grid_info()
            self.assertEqual(int(fmt_grid["column"]), int(exp_grid["column"]),
                             "导出应与格式化同列（左边缘对齐）")
            self.assertNotEqual(int(fmt_grid["row"]), int(exp_grid["row"]))
            # 锁要和『格式化』同一行（垂直居中对齐），不能跨两行居中
            self.assertEqual(int(lock_grid["row"]), int(fmt_grid["row"]),
                             "锁图标应与格式化按钮同一行")
            self.assertEqual(int(lock_grid.get("rowspan", 1) or 1), 1,
                             "锁不该跨行，否则会跑到两行中间")
        finally:
            app.on_close()

    def test_gui_format_needs_lock_and_confirmation(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        before = self._prepare_app_data()
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.managed), reveal=False)
        try:
            app.show_safety()
            root.update()
            self.assertEqual(str(app._fmt_btn.cget("state")), "disabled",
                             "锁打开时『格式化』应该不可点")
            app._toggle_format_lock()                     # 上锁
            root.update()
            self.assertEqual(str(app._fmt_btn.cget("state")), "normal")
            app._toggle_format_lock()                     # 再打开 → 又变灰
            root.update()
            self.assertEqual(str(app._fmt_btn.cget("state")), "disabled")

            app._toggle_format_lock()                     # 上锁后再点
            asked = {"n": 0}
            original = fm.messagebox.askyesno
            try:
                fm.messagebox.askyesno = lambda *a, **k: (asked.__setitem__("n", asked["n"] + 1), False)[1]
                app._ask_format()                         # 用户在确认框里点"否"
                self.assertEqual(asked["n"], 1, "没有弹确认框")
                self.assertTrue(fm.library_file().is_file(), "点了否却把数据删了")
                fm.messagebox.askyesno = lambda *a, **k: True
                fm.messagebox.showinfo = lambda *a, **k: None
                app._ask_format()                         # 点"是"
            finally:
                fm.messagebox.askyesno = original
            self.assertFalse(fm.library_file().exists(), "确认后应该已经格式化")
            self.assertEqual(snapshot(self.managed), before, "格式化动了用户文件！")
        finally:
            app.on_close()


class TestIdentFollowsRename(BaseCase):
    """标注按文件身份跟随改名；拿不到身份时退回按名字。"""

    def setUp(self):
        super().setUp()
        self.file = self.managed / "原名字.txt"
        self.file.write_text("内容", encoding="utf-8")
        self.root = None
        self.app = None

    def tearDown(self):
        if self.app is not None:
            try:
                self.app.on_close()
            except Exception:
                pass
        super().tearDown()

    def _app(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = fm.App(self.root, folder=str(self.managed), reveal=False)
        self.root.update()
        return self.app

    def _wait_ident(self, entry, tries=80):
        app = self.app
        app._request_ident(entry)          # 现实里由"标注时"或刷新时触发
        for _ in range(tries):
            app._drain_idents()
            self.root.update()
            if entry.ident:
                return entry.ident
            time.sleep(0.05)
        return entry.ident

    def test_file_ident_is_available_on_ntfs(self):
        ident = fm.file_ident(self.file)
        self.assertTrue(ident and ":" in ident, "拿不到文件身份：%r" % ident)
        self.assertEqual(fm.file_ident(self.managed / "不存在.txt"), "")

    def test_annotation_follows_rename(self):
        app = self._app()
        entry = [e for e in app.visible if e.name == "原名字.txt"][0]
        ident = self._wait_ident(entry)
        self.assertTrue(ident, "没能取到文件身份")
        app.lib.update(app.folder, entry.rel, ident=ident, rating=4, note="我的标注")

        # 模拟用户在资源管理器里改名
        new_path = self.managed / "改名后.txt"
        self.file.rename(new_path)

        app.refresh()
        self.root.update()
        for _ in range(80):                     # 等后台把身份补齐并归并
            app._drain_idents()
            self.root.update()
            if not app._id_pending and not app._id_wanted:
                break
            time.sleep(0.05)
        app._drain_idents()
        self.root.update()

        new_entry = [e for e in app.visible if e.name == "改名后.txt"][0]
        rec = app._lib_entry(new_entry)
        self.assertEqual(rec.get("rating"), 4, "改名后标注丢了")
        self.assertEqual(rec.get("note"), "我的标注")
        # 记录应该已经搬到新名字下，旧名字不再残留
        self.assertEqual(app.lib.entry(app.folder, "改名后.txt").get("rating"), 4)
        self.assertEqual(app.lib.entry(app.folder, "原名字.txt"), {})

    def test_name_fallback_still_works(self):
        """没有身份信息的历史记录（或网络盘）仍按名字匹配，不能因此失效。"""
        app = self._app()
        app.lib.update(app.folder, "原名字.txt", rating=3)     # 不带 ident
        entry = [e for e in app.visible if e.name == "原名字.txt"][0]
        self.assertEqual(app._lib_entry(entry).get("rating"), 3)

    def test_library_lookup_by_ident(self):
        """名字对不上但身份对得上时，标注依然查得到（这就是改名不丢的机制）。"""
        app = self._app()
        entry = [e for e in app.visible if e.name == "原名字.txt"][0]
        ident = fm.file_ident(self.file)
        self.assertTrue(ident)
        app.lib.update(app.folder, entry.rel, ident=ident, rating=5)

        new_path = self.managed / "新名字.txt"
        self.file.rename(new_path)
        self.assertEqual(fm.file_ident(new_path), ident, "同盘改名后文件身份应当不变")

        # 按身份查得到，按（新）名字查不到 —— 正是期望的行为
        self.assertEqual(app.lib.entry(app.folder, "新名字.txt", ident).get("rating"), 5)
        self.assertIsNone(app.lib.entry(app.folder, "新名字.txt").get("rating"))


class TestExport(BaseCase):
    """导出清单：列要对、按自然顺序排、未评分写"-"、绝不覆盖已有文件。"""

    def test_export_rows_natural_order_and_dash_rating(self):
        out = self.tmp / "清单.txt"
        rows = [("10.txt", "文档", "1 B", "-"),
                ("2.txt", "文档", "2 B", 5),
                ("1.txt", "文档", "3 B", "-")]
        rows.sort(key=lambda r: fm.natural_key(r[0]))
        fm.export_file_list(out, rows)
        lines = out.read_text(encoding="utf-8-sig").strip().splitlines()
        self.assertTrue(lines[0].startswith("命名"), lines[0])
        for word in ("命名", "类型", "大小", "评分"):
            self.assertIn(word, lines[0])
        self.assertEqual([l.split()[0] for l in lines[1:]],
                         ["1.txt", "2.txt", "10.txt"])
        self.assertEqual(lines[1].split()[-1], "-", "未评分应写成横杠")
        self.assertEqual(lines[2].split()[-1], "5")

    def test_export_columns_are_aligned_with_header(self):
        """表头要和下面的每一行对齐（按显示宽度补齐，中文算两格）。"""
        out = self.tmp / "对齐.txt"
        rows = [("很长的中文文件名.bin", "其他文件", "12.3 MB", 4),
                ("a.txt", "文档", "1 B", "-"),
                ("中文.bin", "其他文件", "2.0 GB", "-")]
        fm.export_file_list(out, rows)
        lines = out.read_text(encoding="utf-8-sig").strip().splitlines()
        name_w = max([fm.display_width("命名")] + [fm.display_width(r[0]) for r in rows])
        # 表头
        self.assertEqual(lines[0][:2], "命名")
        self.assertEqual(lines[0][2:2 + (name_w - fm.display_width("命名")) + 2],
                         " " * (name_w - fm.display_width("命名") + 2))
        # 每一行：名字后面补的空格数都要让第二列落在同一显示列上
        for line, row in zip(lines[1:], rows):
            name = row[0]
            self.assertTrue(line.startswith(name), line)
            expect = name_w - fm.display_width(name) + 2
            self.assertEqual(line[len(name):len(name) + expect], " " * expect, line)
        # 第二列的文字必须都落在同一个"显示列"上（表头也一样）
        cols = set()
        for line, row in zip(lines[1:], rows):
            cols.add(fm.display_width(line[:line.index(row[1])]))
        cols.add(fm.display_width(lines[0][:lines[0].index("类型")]))
        self.assertEqual(len(cols), 1, "第二列没有落在同一显示列上：%s" % cols)

    def test_export_never_overwrites_existing_file(self):
        existing = self.tmp / "清单.txt"
        existing.write_text("原有内容，不能动", encoding="utf-8")
        before = existing.read_bytes()
        with self.assertRaises(FileExistsError):
            fm.export_file_list(existing, [("a.txt", "文档", "1 B", "-")])
        self.assertEqual(existing.read_bytes(), before, "导出覆盖了已有文件！")

    def test_unique_path_picks_a_new_name(self):
        p = self.tmp / "x.txt"
        self.assertEqual(fm.unique_path(p), p)
        p.write_text("x", encoding="utf-8")
        self.assertNotEqual(fm.unique_path(p), p)

    def test_natural_key_orders_numbers_correctly(self):
        names = ["10.txt", "2.txt", "1.txt", "a1", "a10", "a2"]
        self.assertEqual(sorted(names, key=fm.natural_key),
                         ["1.txt", "2.txt", "10.txt", "a1", "a2", "a10"])

    def test_gui_export_writes_selection(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        for n in ("b.txt", "a.txt", "10.txt", "2.txt"):
            (self.managed / n).write_text("x", encoding="utf-8")
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.managed), reveal=False)
        try:
            root.update()
            app._select_all(True)
            self.assertEqual(len(app.selected_entries()), 4)
            out_dir = self.tmp / "导出"
            out_dir.mkdir()
            original_ask = fm.filedialog.askdirectory
            original_info = fm.messagebox.showinfo
            try:
                fm.filedialog.askdirectory = lambda *a, **k: str(out_dir)
                fm.messagebox.showinfo = lambda *a, **k: None
                app.export_selected()
            finally:
                fm.filedialog.askdirectory = original_ask
                fm.messagebox.showinfo = original_info
            files = list(out_dir.glob("*.txt"))
            self.assertEqual(len(files), 1, "应该导出一个 txt")
            lines = files[0].read_text(encoding="utf-8-sig").strip().splitlines()
            self.assertTrue(lines[0].startswith("命名"), lines[0])
            self.assertIn("类型", lines[0])
            self.assertIn("评分", lines[0])
            self.assertEqual([l.split()[0] for l in lines[1:]],
                             ["2.txt", "10.txt", "a.txt", "b.txt"])
            # 再导出一次：必须生成第二个文件，第一个的内容不能被改
            first = files[0].read_bytes()
            try:
                fm.filedialog.askdirectory = lambda *a, **k: str(out_dir)
                fm.messagebox.showinfo = lambda *a, **k: None
                app.export_selected()
            finally:
                fm.filedialog.askdirectory = original_ask
                fm.messagebox.showinfo = original_info
            self.assertEqual(len(list(out_dir.glob("*.txt"))), 2)
            self.assertEqual(files[0].read_bytes(), first)
        finally:
            app.on_close()


class TestMultiSelect(BaseCase):
    """多选 / 全选。"""

    class _Click:
        def __init__(self, y, ctrl=False):
            self.y = y
            self.state = 0x0004 if ctrl else 0

    def setUp(self):
        super().setUp()
        for n in ("a.txt", "b.txt", "c.txt", "d.txt"):
            (self.managed / n).write_text("x", encoding="utf-8")
        if not HAS_TK:
            return
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = fm.App(self.root, folder=str(self.managed), reveal=False)
        self.root.update()

    def tearDown(self):
        if getattr(self, "app", None) is not None:
            try:
                self.app.on_close()
            except Exception:
                pass
        super().tearDown()

    def _y_of(self, index: int) -> int:
        return index * fm.ROW_HEIGHT + 5

    def test_ctrl_click_extends_selection(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        app = self.app
        app._on_row_click(self._Click(self._y_of(0)))
        self.assertEqual(len(app.selected_entries()), 1)
        app._on_row_click(self._Click(self._y_of(1), ctrl=True))
        app._on_row_click(self._Click(self._y_of(2), ctrl=True))
        self.assertEqual(len(app.selected_entries()), 3)
        app._on_row_click(self._Click(self._y_of(1), ctrl=True))   # 再点一次取消
        self.assertEqual(len(app.selected_entries()), 2)

    def test_multi_checkbox_and_select_all(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        app = self.app
        app.multi_var.set(True)
        app._on_row_click(self._Click(self._y_of(0)))
        app._on_row_click(self._Click(self._y_of(1)))
        self.assertEqual(len(app.selected_entries()), 2)
        app._select_all(True)
        self.assertEqual(len(app.selected_entries()), 4)
        app._select_all(False)
        self.assertEqual(len(app.selected_entries()), 0)

    def test_double_click_disabled_in_multi_mode(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        app = self.app
        opened = []
        original = app.open_selected
        app.open_selected = lambda: opened.append(1)
        try:
            app.multi_var.set(False)
            app.on_double(self._Click(self._y_of(0)))
            self.assertEqual(len(opened), 1, "非多选时应能双击打开")
            app.multi_var.set(True)
            app.on_double(self._Click(self._y_of(0)))
            self.assertEqual(len(opened), 1, "多选时不应双击打开")
            app.multi_var.set(False)
            app.on_double(self._Click(self._y_of(0), ctrl=True))
            self.assertEqual(len(opened), 1, "按着 Ctrl 时也不应双击打开")
        finally:
            app.open_selected = original

    def test_selection_survives_refresh(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        app = self.app
        app._on_row_click(self._Click(self._y_of(0)))
        app._on_row_click(self._Click(self._y_of(1), ctrl=True))
        names = sorted(e.name for e in app.selected_entries())
        app.refresh()
        self.root.update()
        self.assertEqual(sorted(e.name for e in app.selected_entries()), names)


    def test_shell_open_falls_back_when_startfile_is_blocked(self):
        """受限环境里 os.startfile 会失败，此时应退回启动 explorer（打包后踩过的坑）。"""
        target = self.managed / "素材.txt"
        target.write_text("x", encoding="utf-8")
        real_startfile = fm.os.startfile
        launched = []
        real_popen = fm.subprocess.Popen
        try:
            def boom(_path):
                raise PermissionError("[WinError 5] 拒绝访问。")

            fm.os.startfile = boom
            fm.subprocess.Popen = lambda cmd, **kw: launched.append(cmd) or object()
            self.assertEqual(fm.shell_open(target), "explorer")
            self.assertTrue(launched, "没有退回 explorer")
        finally:
            fm.os.startfile = real_startfile
            fm.subprocess.Popen = real_popen

    def test_shell_open_reports_failure_instead_of_pretending(self):
        """子进程立刻崩掉时必须报错，不能谎报"已打开"（打包后踩过的坑）。"""
        target = self.managed / "素材.txt"
        target.write_text("x", encoding="utf-8")
        real_startfile = fm.os.startfile
        real_popen = fm.subprocess.Popen
        real_via = fm._open_folder_via_shell

        class DeadProc:
            def wait(self, timeout=None):
                return 0xC0000142

        try:
            def boom(_p):
                raise PermissionError("[WinError 5] 拒绝访问。")

            fm.os.startfile = boom
            fm.subprocess.Popen = lambda cmd, **kw: DeadProc()
            fm._open_folder_via_shell = lambda p: False
            with self.assertRaises(OSError) as ctx:
                fm.shell_open(target)
            self.assertIn("0xC0000142", str(ctx.exception))
        finally:
            fm.os.startfile = real_startfile
            fm.subprocess.Popen = real_popen
            fm._open_folder_via_shell = real_via

    def test_shell_open_rejects_missing_path(self):
        with self.assertRaises(FileNotFoundError):
            fm.shell_open(self.managed / "根本不存在.txt")

    def test_shell_open_uses_startfile_when_it_works(self):
        target = self.managed / "素材.txt"
        target.write_text("x", encoding="utf-8")
        real_startfile = fm.os.startfile
        called = []
        try:
            fm.os.startfile = lambda p: called.append(p)
            self.assertEqual(fm.shell_open(target), "startfile")
            self.assertEqual(called, [str(target)])
        finally:
            fm.os.startfile = real_startfile


class TestVersionAndMode(BaseCase):
    """版本号 & 「只读模式 / 修改模式」的接口约定。"""

    def test_version_is_exposed(self):
        self.assertRegex(fm.APP_VERSION, r"^\d+\.\d+\.\d+$")
        self.assertIn(fm.APP_VERSION, fm.APP_TITLE)
        self.assertIn(fm.APP_NAME, fm.APP_TITLE)
        self.assertIn(fm.MODE_LABELS[fm.MODE_READONLY], fm.APP_TITLE)
        self.assertEqual(fm.window_title(fm.MODE_EDIT),
                         fm.APP_TITLE.replace("只读模式", "修改模式"))

    def test_only_readonly_mode_is_available_for_now(self):
        self.assertEqual(fm.get_mode(), fm.MODE_READONLY)
        self.assertEqual(fm.mode_label(), "只读模式")
        with self.assertRaises(NotImplementedError):
            fm.set_mode(fm.MODE_EDIT)          # 修改模式还没做
        self.assertEqual(fm.get_mode(), fm.MODE_READONLY, "失败后模式不该被改掉")
        with self.assertRaises(ValueError):
            fm.set_mode("不存在的模式")
        fm.set_mode(fm.MODE_READONLY)           # 这个允许
        self.assertEqual(fm.get_mode(), fm.MODE_READONLY)

    def test_every_edit_entry_point_is_blocked_in_readonly(self):
        """所有编辑入口都必须被闸门挡住，而且用户文件一个字节都不能变。"""
        f = self.managed / "素材.txt"
        f.write_text("原样", encoding="utf-8")
        (self.managed / "子目录").mkdir()
        before = snapshot(self.managed)
        cases = [
            lambda: fm.rename_entry(f, "新名字.txt"),
            lambda: fm.move_entries([f], self.tmp),
            lambda: fm.delete_entries([f]),
            lambda: fm.create_folder(self.managed, "新目录"),
            lambda: fm.guard_user_write(f, "写文件"),
        ]
        for call in cases:
            with self.assertRaises(fm.EditModeUnavailable):
                call()
        self.assertEqual(snapshot(self.managed), before, "只读模式下动了用户文件！")

    def test_edit_mode_gate_message_mentions_the_action(self):
        with self.assertRaises(fm.EditModeUnavailable) as ctx:
            fm.ensure_edit_mode("重命名文件")
        self.assertIn("重命名文件", str(ctx.exception))
        self.assertIn("只读模式", str(ctx.exception))

    def test_operation_log_helper_is_guarded(self):
        """预留的操作留痕只写程序自己的 data 目录。"""
        fm.record_operation("rename", {"path": "x"})
        log = self.appdata / "oplog.jsonl"
        self.assertTrue(log.is_file())
        self.assertIn("rename", log.read_text(encoding="utf-8"))
        with self.assertRaises(fm.SafetyError):
            fm.guard_app_write(self.managed / "oplog.jsonl")

    def test_safety_dialog_shows_version_and_mode(self):
        if not HAS_TK:
            self.skipTest("当前环境无法创建 Tk 窗口")
        root = tk.Tk()
        root.withdraw()
        app = fm.App(root, folder=str(self.managed), reveal=False)
        try:
            root.update()
            self.assertIn(fm.APP_VERSION, root.title())
            app.show_safety()
            root.update()
            texts = []

            def walk(w):
                for c in w.winfo_children():
                    try:
                        texts.append(str(c.cget("text")))
                    except Exception:
                        pass
                    walk(c)

            walk(app._safety_win)
            blob = "\n".join(texts)
            self.assertIn(fm.APP_VERSION, blob)
            self.assertIn("只读模式", blob)
        finally:
            app.on_close()


class TestSourceSafety(unittest.TestCase):
    """静态审计：源码里不允许出现删除 / 移动 / 重命名 / 解压类调用。"""

    FORBIDDEN_ATTRS = {
        "remove", "unlink", "rmdir", "removedirs", "rmtree", "move", "rename",
        "renames", "truncate", "extractall", "extract", "chmod", "utime",
    }
    #: 只有这一个函数允许删除（而且只删程序自己的 data 目录）
    ALLOWED_DELETE_IN = {"format_app_data"}
    #: 允许在 data 目录之外写文件的函数 —— 只能用 "x" 新建，绝不覆盖已有文件
    CREATE_ONLY_WRITERS = {"export_file_list"}

    def _tree(self) -> ast.Module:
        return ast.parse(Path(fm.__file__).read_text(encoding="utf-8"))

    @staticmethod
    def _call_name(node: ast.Call) -> str:
        fn = node.func
        if isinstance(fn, ast.Attribute):
            return fn.attr
        if isinstance(fn, ast.Name):
            return fn.id
        return ""

    @staticmethod
    def _dotted(node: ast.Call) -> str:
        parts = []
        fn = node.func
        while isinstance(fn, ast.Attribute):
            parts.append(fn.attr)
            fn = fn.value
        if isinstance(fn, ast.Name):
            parts.append(fn.id)
        return ".".join(reversed(parts))

    def _function_spans(self, name: str):
        return [(n.lineno, n.end_lineno) for n in ast.walk(self._tree())
                if isinstance(n, ast.FunctionDef) and n.name == name]

    def test_no_destructive_calls_in_source(self):
        tree = self._tree()
        spans = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in self.ALLOWED_DELETE_IN:
                spans.append((node.lineno, node.end_lineno))
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = self._call_name(node)
                if name in self.FORBIDDEN_ATTRS:
                    if not any(a <= node.lineno <= b for a, b in spans):
                        hits.append((node.lineno, name))
        self.assertEqual(hits, [], "源码出现了危险调用：%s" % hits)

    def test_only_format_function_may_delete_and_it_is_guarded(self):
        """整个程序里只有 format_app_data 能删东西，而且每一步都过 guard_app_write。"""
        src = Path(fm.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "format_app_data":
                found = True
                seg = ast.get_source_segment(src, node) or ""
                self.assertIn("guard_app_write", seg, "格式化函数没有守卫")
                self.assertIn("get_write_root", seg, "格式化函数没有限定在自己的 data 目录内")
        self.assertTrue(found, "找不到 format_app_data")

    def test_os_replace_only_used_for_atomic_json_write(self):
        spans = self._function_spans("atomic_write_text")
        self.assertTrue(spans, "没有找到 atomic_write_text")
        for node in ast.walk(self._tree()):
            if isinstance(node, ast.Call) and self._dotted(node) == "os.replace":
                self.assertTrue(
                    any(a <= node.lineno <= b for a, b in spans),
                    "第 %d 行的 os.replace 不在 atomic_write_text 内" % node.lineno)

    def test_every_file_write_is_guarded(self):
        """所有写文件的地方必须二选一：

        ① 在程序自己的 data 目录内（同一函数里有 guard_app_write）；
        ② 白名单里的"只许新建"函数，且必须用 "x" 模式（文件已存在就报错，绝不覆盖）。
        """
        src = Path(fm.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        problems = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            body_src = ast.get_source_segment(src, fn) or ""
            guarded = "guard_app_write" in body_src
            create_only = fn.name in self.CREATE_ONLY_WRITERS
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and self._call_name(node) == "open"):
                    continue
                modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
                modes += [kw.value.value for kw in node.keywords
                          if kw.arg == "mode" and isinstance(kw.value, ast.Constant)]
                writes = [m for m in modes
                          if isinstance(m, str) and ("w" in m or "a" in m or "x" in m)]
                if not writes:
                    continue
                if guarded:
                    continue
                if create_only:
                    if not all("x" in m and "w" not in m and "a" not in m for m in writes):
                        problems.append((fn.name, node.lineno, "白名单函数必须用 x 模式", modes))
                    continue
                problems.append((fn.name, node.lineno, "未受保护的写操作", modes))
        self.assertEqual(problems, [], "存在不可接受的写操作：%s" % problems)

    def test_only_whitelisted_functions_may_write_outside_data_dir(self):
        src = Path(fm.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        outside = []
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            seg = ast.get_source_segment(src, fn) or ""
            if "guard_app_write" in seg:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and self._call_name(node) == "open":
                    modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
                    modes += [kw.value.value for kw in node.keywords
                              if kw.arg == "mode" and isinstance(kw.value, ast.Constant)]
                    if any(isinstance(m, str) and ("w" in m or "a" in m or "x" in m)
                           for m in modes):
                        outside.append(fn.name)
        self.assertEqual(sorted(set(outside)), sorted(self.CREATE_ONLY_WRITERS),
                         "只有白名单函数可以在 data 目录之外写文件")

    def test_guard_helpers_exist(self):
        src = Path(fm.__file__).read_text(encoding="utf-8")
        self.assertIn("def guard_app_write", src)
        self.assertIn("def ensure_data_dirs", src)


try:
    import tkinter as tk
    _ROOT_PROBE = tk.Tk()
    _ROOT_PROBE.destroy()
    HAS_TK = True
except Exception:  # pragma: no cover
    HAS_TK = False


@unittest.skipUnless(HAS_TK, "当前环境无法创建 Tk 窗口")
class TestGuiIntegration(BaseCase):
    """真正把界面跑起来：列表、缩略图、评分、悬停预览。"""

    def setUp(self):
        super().setUp()
        make_png(self.managed / "阳光.png")
        (self.managed / "说明.txt").write_text("hello", encoding="utf-8")
        (self.managed / "打包.zip").write_text("zip", encoding="utf-8")
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = fm.App(self.root, folder=str(self.managed), reveal=False)

    def tearDown(self):
        try:
            self.app.on_close()
        except Exception:
            pass
        super().tearDown()

    def test_list_rows_and_icons(self):
        self.root.update()
        rows = self.app.tree.get_children()
        # 窗口化列表：树上只保留可见的那些行（行数可能多于文件数，因为有空行占位）
        self.assertGreaterEqual(len(rows), 3)
        self.assertEqual(len(self.app.item_entry), 3, "三个文件应各占一行")
        for item in self.app.item_entry:
            self.assertTrue(self.app.tree.item(item, "image"))
        names = sorted(e.name for e in self.app.item_entry.values())
        self.assertEqual(names, sorted(["说明.txt", "打包.zip", "阳光.png"]))

    def test_status_bar_keeps_the_item_count(self):
        """切换文件夹后，状态栏要是"共 N 项（显示 M 项）…"，不能变成"正在管理：…"。"""
        self.root.update()
        text = self.app.status_var.get()
        self.assertIn("共", text)
        self.assertIn("项", text)
        self.assertNotIn("正在管理", text)
        self.app.set_folder(str(self.managed))      # 再切一次文件夹
        self.root.update()
        again = self.app.status_var.get()
        self.assertIn("共", again, "切文件夹后计数信息被覆盖了")
        self.assertNotIn("正在管理", again)

    def test_only_visible_rows_are_loaded(self):
        """几百个文件也只占几十行——这就是"秒开"的关键。"""
        big = self.managed / "大目录"
        big.mkdir()
        for i in range(300):
            (big / ("f%03d.txt" % i)).write_bytes(b"x")
        self.app.set_folder(str(big))
        self.root.update()
        rows = self.app.tree.get_children()
        self.assertEqual(len(self.app.visible), 300)
        self.assertLess(len(rows), 60, "树上不应出现全部 300 行")
        self.assertEqual(len(self.app.item_entry), len(rows))
        self.assertEqual(self.app.item_entry[rows[0]].name, self.app.visible[0].name)

    def _big_folder(self, n: int = 300):
        big = self.managed / "大目录"
        if not big.is_dir():
            big.mkdir()
            for i in range(n):
                (big / ("f%03d.txt" % i)).write_bytes(b"x")
        self.app.set_folder(str(big))
        self.root.update()
        return big

    def test_scrolling_maps_to_the_right_files(self):
        self._big_folder(300)
        view = self.app._viewport_rows()          # 一屏真正可见的行数（不含缓冲行）
        self.app._set_offset(10 ** 6)            # 一直滚到底
        self.root.update()
        self.assertEqual(self.app._offset, 300 - view)
        rows = self.app.tree.get_children()
        visible_rows = rows[:view]               # 只有这些是"看得见"的
        self.assertEqual(self.app.item_entry[visible_rows[-1]].name,
                         self.app.visible[-1].name,
                         "滚到底之后，最后一项没有落在可视区里")
        self.app._set_offset(0)
        self.root.update()
        rows = self.app.tree.get_children()
        self.assertEqual(self.app.item_entry[rows[0]].name,
                         self.app.visible[0].name)

    def test_last_item_is_reachable_by_scrolling(self):
        """回归：一屏放不下时，最后一项必须能滚进可视区。

        曾经这里差最后 ROW_POOL_EXTRA 项永远滚不出来：滚动上限按"行池大小
        （可见行 + 缓冲行）"算，于是最后 4 项被永久藏在可视区下方。
        """
        big = self.managed / "可滚动目录"
        big.mkdir()
        for i in range(20):
            (big / ("%02d.txt" % i)).write_bytes(b"x")
        self.app.set_folder(str(big))
        self.root.update()
        # 测试环境的窗口是隐藏的（高度恒为 1），这里直接假定"一屏 11 行"
        self.app.tree.winfo_height = lambda: 11 * fm.ROW_HEIGHT
        self.app._ensure_pool()
        self.app.tree.winfo_height = lambda: 11 * fm.ROW_HEIGHT
        self.assertEqual(self.app._viewport_rows(), 11)
        self.assertEqual(len(self.app.tree.get_children()), 11 + fm.ROW_POOL_EXTRA)
        self.app._set_offset(10 ** 6)            # 滚到底
        self.root.update()
        rows = self.app.tree.get_children()
        shown = [self.app.item_entry[r] for r in rows[:11]
                 if r in self.app.item_entry]
        self.assertEqual(shown[-1].name, self.app.visible[-1].name,
                         "滚到底了，最后一项仍然看不见（回归 bug）")

    def test_selection_survives_scrolling_far_away(self):
        self._big_folder(300)
        first = self.app.visible[0]
        self.app._selected_index = 0
        self.app._fill_rows()
        self.root.update()
        self.assertIs(self.app.selected_entry(), first)
        self.app._set_offset(250)                 # 滚到选中项看不见的地方
        self.root.update()
        self.assertIs(self.app.selected_entry(), first, "滚远后选中项丢了")
        self.app._set_offset(0)
        self.root.update()
        self.assertEqual(len(self.app.tree.selection()), 1, "滚回来应重新高亮")

    def test_keyboard_navigation_covers_all_rows(self):
        self._big_folder(300)
        self.app._move_selection(absolute="end")
        self.assertEqual(self.app._selected_index, 299)
        self.app._move_selection(absolute="home")
        self.assertEqual(self.app._selected_index, 0)
        self.app._move_selection(5)
        self.assertEqual(self.app._selected_index, 5)
        row = self.app.tree.selection()
        self.assertEqual(len(row), 1)
        self.assertEqual(self.app.item_entry[row[0]].name,
                         self.app.visible[5].name)

    def test_refresh_does_not_rebuild_one_row_per_file(self):
        """刷新 300 项：最多按高度重算一次行池，绝不"一项一行"地重建整张表。"""
        self._big_folder(300)
        for _ in range(3):
            self.root.update()
        rebuilds = self.app._pool_rebuilds
        self.app.refresh()
        self.root.update()
        self.assertLessEqual(self.app._pool_rebuilds - rebuilds, 1,
                             "刷新引起了多次行池重建")
        rows = self.app.tree.get_children()
        self.assertLess(len(rows), 60, "树上出现了过多行")
        self.assertEqual(self.app.item_entry[rows[0]].name,
                         self.app.visible[0].name)

    def test_rating_and_preview_without_touching_files(self):
        self.root.update()
        before = snapshot(self.managed)
        item = self.app.tree.get_children()[0]
        self.app.tree.selection_set(item)
        self.app.set_rating(5)
        self.app.save_note()
        entry = self.app.item_entry[item]
        # 悬停 = 在右侧详情面板里放大预览（不改变选中状态）
        self.app.preview_in_detail(item)
        self.root.update()
        self.assertIs(self.app._detail_entry, entry, "悬停没有把详情面板切到该文件")
        self.assertIs(self.app.detail_img.cget("image") != "", True)
        viewer = self.app.open_viewer()
        self.root.update()
        if viewer is not None:
            viewer.destroy()
        self.assertEqual(snapshot(self.managed), before, "GUI 操作改动了用户文件！")
        self.assertEqual(int(self.app.lib.entry(self.managed, entry.rel)["rating"]), 5)

    def test_hover_does_not_change_selection(self):
        self.root.update()
        items = self.app.tree.get_children()
        self.app.tree.selection_set(items[0])
        self.root.update()          # 先让选中事件处理完
        selected = self.app.item_entry[items[0]]
        other = self.app.item_entry[items[1]]
        self.app.preview_in_detail(items[1])
        self.root.update()
        self.assertIs(self.app._detail_entry, other)
        self.assertIs(self.app.selected_entry(), selected, "悬停不应改变选中项")
        self.app.on_leave()
        self.root.update()
        self.assertIs(self.app._detail_entry, selected, "鼠标离开后应回到选中项")

    def test_cover_assignment_shows_thumbnail(self):
        self.root.update()
        target = None
        for item, e in self.app.item_entry.items():
            if e.name == "说明.txt":
                target = item
        self.assertIsNotNone(target)
        self.app.tree.selection_set(target)
        self.app.lib.update(self.managed, "说明.txt",
                            cover=self.app.lib.import_cover(
                                self.managed / "阳光.png", "k3"))
        self.app.refresh(keep_selection=True)
        self.root.update()
        row = [i for i, e in self.app.item_entry.items() if e.name == "说明.txt"][0]
        self.assertTrue(self.app.tree.item(row, "image"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
