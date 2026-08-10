"""Render Monitor 子进程渲染脚本单元测试（mock bpy，不依赖真实 Blender）。

验证 rm_job.py 的核心行为：
1. 参数解析（缺少 '--' / 参数不足 → 非 0 退出）
2. 逐个应用快照并渲染；单张失败不中断，进度 JSON 正确记录 DONE/FAILED
3. 渲染未写盘时标记失败
4. 最终进度文件 state=done

运行：python -m unittest render_monitor.tests.test_rm_job
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import types
import unittest

from .test_core import MockData, MockObject, MockNamedCollection, MockScene, Vector  # noqa: F401


class TestRmJob(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 注入 mock 模块（复用 test_core 的基建）
        math = types.ModuleType("mathutils")
        math.Vector = Vector
        math.Matrix = __import__("render_monitor.tests.test_core", fromlist=["Matrix"]).Matrix
        sys.modules["mathutils"] = math

        bpy_mod = types.ModuleType("bpy")
        data = MockData()
        data.scenes = MockNamedCollection("name")
        bpy_mod.data = data
        bpy_mod.context = types.SimpleNamespace()
        bpy_mod.ops = types.SimpleNamespace(render=types.SimpleNamespace(render=None))
        # render_stats handler 注册目标（rm_job._main 会 append）
        bpy_mod.app = types.SimpleNamespace(
            handlers=types.SimpleNamespace(render_stats=[])
        )
        sys.modules["bpy"] = bpy_mod

        cls.bpy_mod = bpy_mod
        cls.rm_job = importlib.import_module("render_monitor.rm_job")
        # core 在 mock 环境下加载
        cls.core = importlib.import_module("render_monitor.core")
        importlib.reload(cls.core)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rm_job_test_")
        self.addCleanup(self._cleanup_tmp)
        # 重置模块级渲染状态，避免用例间泄漏
        self.rm_job._stats.update(
            entry=None, start=None, last_write=0.0, progress_path="",
            total=0, shots=[], tile_weights=[],
        )
        # 重建 scene 与快照
        self.scene = MockScene("Scene")
        self.bpy_mod.data.scenes.add(self.scene)
        self.bpy_mod.context.scene = self.scene
        self.rendered_paths = []
        self.render_error = None

        def fake_render(*args, **kwargs):
            if self.render_error:
                raise self.render_error
            # 模拟渲染：在 scene.render.filepath 处生成文件
            path = self.scene.render.filepath
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"fake-image")
            self.rendered_paths.append(path)

        self.bpy_mod.ops.render.render = fake_render

    def _cleanup_tmp(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_snapshot(self, uid, name, frame=10):
        obj = MockObject(f"{name}_Obj")
        return {
            "uid": uid,
            "name": name,
            "data": {"frame_current": frame, "objects": [{
                "name": obj.name, "type": obj.type, "hide_viewport": False,
                "hide_render": False, "hide_select": False, "parent": None,
                "matrix_local": [list(r) for r in obj.matrix_local],
            }], "collections": [], "view_layers": [], "world": None,
                     "camera": None, "render": {"props": []}},
        }

    def _run(self, snapshots, extra_args=(), template="{name}_{frame:04d}"):
        snap_path = os.path.join(self.tmp, "snapshots.json")
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(snapshots, f, ensure_ascii=False)
        progress = os.path.join(self.tmp, "progress.json")
        outdir = os.path.join(self.tmp, "out")
        old_argv = sys.argv
        sys.argv = ["blender", "-b", "x.blend", "-P", "rm_job.py", "--",
                    "Scene", snap_path, outdir, template, "1",
                    progress] + list(extra_args)
        try:
            code = self.rm_job._main()
        finally:
            sys.argv = old_argv
        return code, progress

    def test_missing_separator(self):
        old_argv = sys.argv
        sys.argv = ["blender", "-b"]
        try:
            self.assertEqual(self.rm_job._main(), 1)
        finally:
            sys.argv = old_argv

    def test_too_few_args(self):
        old_argv = sys.argv
        sys.argv = ["blender", "-b", "--", "only-one"]
        try:
            self.assertEqual(self.rm_job._main(), 1)
        finally:
            sys.argv = old_argv

    def test_all_success(self):
        snaps = [self._make_snapshot("a" * 32, "shotA"), self._make_snapshot("b" * 32, "shotB")]
        code, progress = self._run(snaps)
        self.assertEqual(code, 0)
        with open(progress, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["state"], "done")
        self.assertEqual(payload["total"], 2)
        statuses = [s["status"] for s in payload["shots"]]
        self.assertEqual(statuses, ["DONE", "DONE"])
        self.assertTrue(all(os.path.exists(s["path"]) for s in payload["shots"]))
        # 渲染了两张
        self.assertEqual(len(self.rendered_paths), 2)

    def test_index_in_filename(self):
        # {index} = 列表顺序（从 1 开始），输出文件名按顺序编号
        snaps = [self._make_snapshot("a" * 32, "shotA"), self._make_snapshot("b" * 32, "shotB")]
        code, progress = self._run(snaps, template="{name}_{index}")
        self.assertEqual(code, 0)
        with open(progress, encoding="utf-8") as f:
            payload = json.load(f)
        paths = [s["path"] for s in payload["shots"]]
        self.assertTrue(paths[0].endswith("shotA_1.png"), paths[0])
        self.assertTrue(paths[1].endswith("shotB_2.png"), paths[1])

    def test_one_failure_continues(self):
        snaps = [self._make_snapshot("a" * 32, "shotA"), self._make_snapshot("b" * 32, "shotB")]
        # 仅第一张渲染失败，第二张应正常渲染
        real_render = self.bpy_mod.ops.render.render
        calls = {"n": 0}

        def fail_first(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("mock render boom")
            return real_render(*args, **kwargs)

        self.bpy_mod.ops.render.render = fail_first
        fail_uid = snaps[0]["uid"]
        code, progress = self._run(snaps)
        # 单张失败被捕获，脚本正常结束
        self.assertEqual(code, 0)
        with open(progress, encoding="utf-8") as f:
            payload = json.load(f)
        statuses = {s["uid"]: s for s in payload["shots"]}
        self.assertEqual(statuses[fail_uid]["status"], "FAILED")
        self.assertIn("mock render boom", statuses[fail_uid]["error"])
        self.assertEqual(statuses[snaps[1]["uid"]]["status"], "DONE")

    def test_no_file_written_marks_failed(self):
        snaps = [self._make_snapshot("a" * 32, "shotA")]

        def fake_render_no_write(*args, **kwargs):
            pass  # 不写文件 → 校验失败

        self.bpy_mod.ops.render.render = fake_render_no_write
        code, progress = self._run(snaps)
        self.assertEqual(code, 0)
        with open(progress, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["shots"][0]["status"], "FAILED")
        self.assertIn("未生成输出文件", payload["shots"][0]["error"])

    def _stats_entry(self):
        entry = {"uid": "x" * 32, "name": "shotX", "status": "RENDERING",
                 "path": "", "error": ""}
        progress = os.path.join(self.tmp, "progress_stats.json")
        self.rm_job._stats.update(
            entry=entry, start=1.0, last_write=0.0,
            progress_path=progress, total=1, shots=[entry],
        )
        return entry, progress

    def test_render_stats_updates_entry_and_progress(self):
        # render_stats handler：解析采样/剩余时间，并写入进度 JSON
        entry, progress = self._stats_entry()
        self.rm_job._on_render_stats(
            "Remaining: 00:01.33 | Mem: 6M | Sample 96/256"
        )
        self.assertEqual(entry["samples"], 96)
        self.assertEqual(entry["samples_total"], 256)
        self.assertAlmostEqual(entry["remaining"], 1.33, places=2)
        self.assertGreater(entry["elapsed"], 0.0)  # 已用时间按 start 推算
        with open(progress, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertEqual(payload["shots"][0]["samples"], 96)
        self.assertAlmostEqual(payload["shots"][0]["remaining"], 1.33, places=2)

    def test_render_stats_frame_done_time(self):
        # 帧完成 stats 的精确已用时间应覆盖 elapsed
        entry, _ = self._stats_entry()
        self.rm_job._on_render_stats("Time: 00:01.00 (Saving: 00:00.11)")
        self.assertAlmostEqual(entry["elapsed"], 1.00, places=2)

    def test_render_stats_finalize_remaining_ignored(self):
        # 收尾阶段（Finished / 采样计数重置为 0）的 Remaining 异常偏大，
        # 只接受采样进行中（samples > 0）的估计值
        entry, _ = self._stats_entry()
        self.rm_job._on_render_stats(
            "Remaining: 00:00.37 | Mem: 35M | Sample 432/512"
        )
        self.assertAlmostEqual(entry["remaining"], 0.37, places=2)
        # 无 Sample 的 Finished stats 携带异常 Remaining → 忽略
        self.rm_job._on_render_stats("Remaining: 00:14.04 | Mem: 35M | Finished")
        self.assertAlmostEqual(entry["remaining"], 0.37, places=2)
        # samples=0 的收尾重置 stats 携带异常 Remaining → 忽略
        self.rm_job._on_render_stats("Remaining: 01:05.00 | Mem: 35M | Sample 0/512")
        self.assertAlmostEqual(entry["remaining"], 0.37, places=2)

    def test_render_stats_init_strings_no_crash(self):
        # 初始化阶段的 stats 没有 Sample/Time 信息，不应崩溃也不应写坏数据
        entry, progress = self._stats_entry()
        self.rm_job._on_render_stats("Mem: 1M | Updating Objects")
        with open(progress, encoding="utf-8") as f:
            payload = json.load(f)
        self.assertNotIn("samples", payload["shots"][0])

    def test_tile_switch_updates_samples_and_progress(self):
        # 大图分块渲染：进度条 = 引擎整体进度，只在采样未满时更新；
        # 块完成残留满值不重复计算，避免 25% → 50% 的跳变
        entry, _ = self._stats_entry()
        # 块 1 采样推进（非满）：进度 =（0 + 80/128）/ 4
        self.rm_job._on_render_stats(
            "Remaining: 00:06.41 | Mem: 268M | Rendered 0/4 Tiles, Sample 80/128"
        )
        self.assertAlmostEqual(entry["progress"], (0 + 80 / 128) / 4, places=3)
        # 块 1 采样完成 + 块数 +1 的残留满值：均不更新进度（不跳变）
        self.rm_job._on_render_stats(
            "Remaining: 00:15.41 | Mem: 306M | Rendered 0/4 Tiles, Sample 128/128"
        )
        self.rm_job._on_render_stats(
            "Remaining: 00:15.41 | Mem: 306M | Rendered 1/4 Tiles, Sample 128/128"
        )
        self.assertAlmostEqual(entry["progress"], (0 + 80 / 128) / 4, places=3)
        # 块 2 开始采样：进度 =（1 + 1/128）/ 4，从 0.25 附近平滑上升
        self.rm_job._on_render_stats(
            "Remaining: 00:15.34 | Mem: 268M | Rendered 1/4 Tiles, Sample 1/128"
        )
        self.assertAlmostEqual(entry["progress"], (1 + 1 / 128) / 4, places=3)
        # 同一块内采样推进
        self.rm_job._on_render_stats(
            "Remaining: 00:06.41 | Mem: 268M | Rendered 1/4 Tiles, Sample 80/128"
        )
        self.assertAlmostEqual(entry["progress"], (1 + 80 / 128) / 4, places=3)

    def test_progress_uses_tile_pixel_weights(self):
        # 块大小不均时，进度按已完成像素占比计算（引擎真实进度）
        entry, _ = self._stats_entry()
        weights = self.rm_job.utils.compute_tile_weights(1920, 1080, 960)  # 2x2 不均匀
        self.assertEqual(len(weights), 4)
        self.rm_job._stats.update(tile_weights=weights)
        # 块 1 采样 80/128：进度 = w0 × 80/128（而非等权 80/(128*4)）
        self.rm_job._on_render_stats(
            "Remaining: 00:15.41 | Mem: 306M | Rendered 0/4 Tiles, Sample 80/128"
        )
        self.assertAlmostEqual(
            entry["progress"], weights[0] * 80 / 128, places=3
        )
        # 残留满值不更新 → 块 2 开始：w0 + w1 × 1/128
        self.rm_job._on_render_stats(
            "Remaining: 00:15.41 | Mem: 306M | Rendered 1/4 Tiles, Sample 128/128"
        )
        self.rm_job._on_render_stats(
            "Remaining: 00:15.34 | Mem: 268M | Rendered 1/4 Tiles, Sample 1/128"
        )
        self.assertAlmostEqual(
            entry["progress"], weights[0] + weights[1] * 1 / 128, places=3
        )

    def test_progress_falls_back_to_equal_weight(self):
        # 权重缺失（如引擎布局与计算不符）时回退等权
        entry, _ = self._stats_entry()
        self.rm_job._on_render_stats(
            "Remaining: 00:15.34 | Mem: 268M | Rendered 1/4 Tiles, Sample 1/128"
        )
        self.assertAlmostEqual(entry["progress"], (1 + 1 / 128) / 4, places=3)

    def test_finalize_phase_detected(self):
        # 收尾阶段（去噪/合成/保存）被标记，进度置满
        entry, _ = self._stats_entry()
        self.rm_job._on_render_stats(
            "Remaining: 00:12.36 | Mem: 602M | ViewLayer | Finishing"
        )
        self.assertEqual(entry["phase"], "finalize")
        self.assertEqual(entry["progress"], 1.0)

    def test_no_finalize_during_sampling(self):
        # 采样阶段不会被误标记为收尾
        entry, _ = self._stats_entry()
        self.rm_job._on_render_stats("Remaining: 00:01.33 | Mem: 35M | Sample 96/128")
        self.assertNotIn("phase", entry)


if __name__ == "__main__":
    unittest.main()
