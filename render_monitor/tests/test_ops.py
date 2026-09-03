"""Render Monitor ops 导出逻辑单元测试（mock bpy，不依赖真实 Blender）。

验证 {index} 占位符的编号语义（回归测试，针对「中断后续跑编号错乱」bug）：
- 批量渲染按**勾选**（shot.selected）过滤后，index 必须是快照在列表中的
  **原始顺序**（从 1 开始），而不是本次渲染队列的重新编号。否则中断后续跑时，
  剩余快照会从 1 重新计数，文件名与首次渲染撞车并被覆盖。
- 「渲染勾选」只渲染勾选的快照（与状态无关）；全选/全不选/反选批量设置勾选。
- 「渲染当前」单张时，index = 该快照在列表中的位置。

运行：python -m unittest render_monitor.tests.test_ops
"""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

from .test_core import Matrix, MockData, MockNamedCollection, MockScene, Vector  # noqa: F401


class MockShot:
    """模拟 bpy.types.Scene.rm_shots 里的一个快照条目。"""

    def __init__(self, uid, name, status="PENDING", selected=True):
        self.uid = uid
        self.name = name
        self.status = status
        self.selected = selected
        self.output_path = ""
        self.error = ""
        self.data_json = json.dumps(
            {
                "version": 2,
                "frame_current": 1,
                "objects": [],
                "collections": [],
                "view_layers": [],
                "world": None,
                "camera": None,
                "render": {"props": []},
            },
            ensure_ascii=False,
        )


class MockShotList:
    """模拟可迭代的 rm_shots 集合（测试只需 __iter__/__len__）。"""

    def __init__(self, shots):
        self._shots = list(shots)

    def __iter__(self):
        return iter(self._shots)

    def __len__(self):
        return len(self._shots)

    def move(self, from_index, to_index):
        self._shots[from_index], self._shots[to_index] = (
            self._shots[to_index],
            self._shots[from_index],
        )


class MockSceneOps(MockScene):
    """在通用 MockScene 上补充 Render Monitor 动态注册的 Scene 属性。"""

    def __init__(self, name="Scene"):
        super().__init__(name)
        self.rm_shots = MockShotList([])
        self.rm_shots_active = 0
        self.rm_output_dir = ""
        self.rm_file_template = ""
        self.rm_use_snapshot_frame = True
        self.rm_write_log = False
        self.rm_render_done = 0
        self.rm_render_failed = 0
        self.rm_render_total = 0
        self.rm_render_current = ""
        self.rm_render_samples = ""
        self.rm_render_samples_cur = 0
        self.rm_render_samples_total = 0
        self.rm_render_tiles_done = 0
        self.rm_render_tiles_total = 1
        self.rm_render_progress = 0.0
        self.rm_render_phase = ""
        self.rm_render_time = ""
        self.rm_render_remaining = ""
        self.rm_last_message = ""


class TestOpsExportIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 注入 mock 模块，使 ops.py 的 import bpy/mathutils 成功
        math = types.ModuleType("mathutils")
        setattr(math, "Vector", Vector)
        setattr(math, "Matrix", Matrix)
        sys.modules["mathutils"] = math

        bpy_mod = types.ModuleType("bpy")
        # ops.py 的 Operator 子类在模块加载时求值基类表达式
        setattr(bpy_mod, "types", SimpleNamespace(Operator=type("Operator", (), {})))
        setattr(
            bpy_mod,
            "app",
            SimpleNamespace(
                binary_path="C:/fake/blender.exe",
                timers=SimpleNamespace(
                    is_registered=lambda *a, **k: False,
                    register=lambda *a, **k: None,
                ),
            ),
        )
        data = MockData()
        setattr(data, "filepath", "C:/fake/project.blend")
        setattr(bpy_mod, "data", data)
        setattr(bpy_mod, "path", SimpleNamespace(abspath=lambda p: p))
        setattr(bpy_mod, "ops", SimpleNamespace(wm=SimpleNamespace()))

        def fake_save(filepath, copy=True):
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            with open(filepath, "wb") as f:
                f.write(b"fake-blend")
            return {"FINISHED"}

        bpy_mod.ops.wm.save_as_mainfile = fake_save
        # _finish_session / _update_ui_from_progress 依赖 context 与 scenes
        setattr(
            bpy_mod,
            "context",
            SimpleNamespace(window_manager=SimpleNamespace(rm_busy=False)),
        )
        setattr(data, "scenes", MockNamedCollection("name"))
        setattr(data, "window_managers", [])  # _tag_redraw 遍历，空即可
        sys.modules["bpy"] = bpy_mod
        cls.bpy_mod = bpy_mod

        cls.ops = importlib.import_module("render_monitor.ops")
        importlib.reload(cls.ops)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rm_ops_test_")
        self.addCleanup(self._cleanup_tmp)
        self.scene = MockSceneOps("Scene")
        self.bpy_mod.data.scenes.add(self.scene)
        self.scene.rm_output_dir = os.path.join(self.tmp, "out")
        self.scene.rm_file_template = "{name}_{index}"
        self.scene.rm_use_snapshot_frame = True
        self.scene.rm_write_log = False
        # 渲染 5 张：前 3 张已完成，后 2 张待渲染（模拟中断后续跑）
        self.scene.rm_shots = MockShotList(
            [
                MockShot("a" * 32, "shotA", status="DONE"),
                MockShot("b" * 32, "shotB", status="DONE"),
                MockShot("c" * 32, "shotC", status="DONE"),
                MockShot("d" * 32, "shotD", status="PENDING"),
                MockShot("e" * 32, "shotE", status="PENDING"),
            ]
        )

    def _cleanup_tmp(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        # 重置模块级会话状态，避免用例间泄漏
        self.ops._active.update(
            process=None, scene_name="", tmpdir="", progress_path="",
            log_path="", total=0, uids=[],
        )
        self.ops._capture_dialog_operator = None

    def _context(self):
        return SimpleNamespace(
            scene=self.scene,
            window_manager=SimpleNamespace(rm_busy=False),
        )

    def _run_export(self, uids):
        """调用真实导出逻辑，返回 (ok, msg, exported_snapshots)。"""
        class FakeProc:
            def poll(self):
                return 0

        with mock.patch(
            "render_monitor.ops.subprocess.Popen", return_value=FakeProc()
        ):
            ok, msg = self.ops._start_subprocess_render(self._context(), uids)
        if not ok:
            return ok, msg, []
        with open(
            os.path.join(self.ops._active["tmpdir"], "snapshots.json"),
            encoding="utf-8",
        ) as f:
            return ok, msg, json.load(f)

    def test_index_is_list_position_for_selected(self):
        """按勾选过滤后，index 仍取列表原始位置（回归：中断后续跑编号不乱）。"""
        # 勾选第 2/4/5 个（shotB/shotD/shotE），不勾选其他
        for s, sel in zip(self.scene.rm_shots._shots, [False, True, False, True, True]):
            s.selected = sel
        uids = [s.uid for s in self.scene.rm_shots if s.selected]
        self.assertEqual(len(uids), 3)
        ok, _msg, exported = self._run_export(uids)
        self.assertTrue(ok)
        self.assertEqual([e["index"] for e in exported], [2, 4, 5])
        self.assertEqual([e["name"] for e in exported], ["shotB", "shotD", "shotE"])

    def test_render_all_renders_only_selected(self):
        """「渲染勾选」只渲染勾选的快照，且与状态无关（已完成/失败/待渲染勾选都渲染）。"""
        shots = self.scene.rm_shots._shots
        shots[2].status = "FAILED"  # shotC 状态改为失败，验证不再有「仅渲染未完成」过滤
        for s, sel in zip(shots, [True, False, False, True, True]):
            s.selected = sel
        op = self.ops.RM_OT_render_all()
        op.report = lambda *a, **k: None  # ty: ignore[invalid-assignment]

        class FakeProc:
            def poll(self):
                return 0

        with mock.patch(
            "render_monitor.ops.subprocess.Popen", return_value=FakeProc()
        ):
            result = op.execute(self._context())
        self.assertEqual(result, {"FINISHED"})
        with open(
            os.path.join(self.ops._active["tmpdir"], "snapshots.json"),
            encoding="utf-8",
        ) as f:
            exported = json.load(f)
        self.assertEqual([e["name"] for e in exported], ["shotA", "shotD", "shotE"])
        self.assertEqual([e["index"] for e in exported], [1, 4, 5])

    def test_select_all_actions(self):
        """全选 / 全不选 / 反选批量设置勾选状态。"""
        shots = self.scene.rm_shots._shots
        for s in shots:
            s.selected = False
        ctx = self._context()

        op = self.ops.RM_OT_select_all()
        op.action = "ALL"
        self.assertEqual(op.execute(ctx), {"FINISHED"})
        self.assertTrue(all(s.selected for s in shots))

        op.action = "NONE"
        self.assertEqual(op.execute(ctx), {"FINISHED"})
        self.assertTrue(all(not s.selected for s in shots))

        op.action = "INVERT"
        self.assertEqual(op.execute(ctx), {"FINISHED"})
        self.assertTrue(all(s.selected for s in shots))
        self.assertEqual(op.execute(ctx), {"FINISHED"})
        self.assertTrue(all(not s.selected for s in shots))

    def test_toggle_shot(self):
        """rm.toggle_shot 按 uid 切换单个快照的勾选状态。"""
        shots = self.scene.rm_shots._shots
        op = self.ops.RM_OT_toggle_shot()
        op.uid = shots[0].uid
        op.execute(self._context())
        self.assertFalse(shots[0].selected)
        op.execute(self._context())
        self.assertTrue(shots[0].selected)
        # 不存在的 uid 静默跳过，不报错
        op.uid = "0" * 32
        self.assertEqual(op.execute(self._context()), {"FINISHED"})

    def test_move_shot_preserves_data_and_active_index(self):
        """上移/下移必须完整保留快照数据（包括 error），并更新活动索引。"""
        shots = self.scene.rm_shots._shots
        shots[0].error = "旧错误"
        shots[1].selected = False
        self.scene.rm_shots_active = 1

        # 下移第 2 个 → 顺序 [A, C, B, D, E]
        self.assertTrue(self.ops._move_shot(self.scene, 1, 1))
        self.assertEqual(self.scene.rm_shots_active, 2)
        self.assertEqual(
            [s.name for s in self.scene.rm_shots._shots],
            ["shotA", "shotC", "shotB", "shotD", "shotE"],
        )
        self.assertEqual(self.scene.rm_shots._shots[2].error, "")

        # 上移第 3 个（原 shotB）回到第 2 位
        self.assertTrue(self.ops._move_shot(self.scene, 2, -1))
        self.assertEqual(self.scene.rm_shots_active, 1)
        self.assertEqual(
            [s.name for s in self.scene.rm_shots._shots],
            ["shotA", "shotB", "shotC", "shotD", "shotE"],
        )

        # 再上移第 2 个到顶部，error 字段也必须跟着原快照走
        self.assertTrue(self.ops._move_shot(self.scene, 1, -1))
        self.assertEqual(
            [s.name for s in self.scene.rm_shots._shots],
            ["shotB", "shotA", "shotC", "shotD", "shotE"],
        )
        self.assertEqual(self.scene.rm_shots._shots[1].error, "旧错误")

    def test_index_is_list_position_for_single_selection(self):
        """「渲染当前」单张时，index = 该快照在列表中的位置。"""
        second = self.scene.rm_shots._shots[1]  # 列表第 2 个（shotB）
        ok, _msg, exported = self._run_export([second.uid])
        self.assertTrue(ok)
        self.assertEqual([e["index"] for e in exported], [2])

    def test_progress_mmap_roundtrip(self):
        """进度回传（v1.5.0）：rm_job 写端与 ops 读端经文件-backed mmap 互通。

        读写都走内存映射（页面缓存），内容为 [长度前缀][JSON payload]。
        """
        import render_monitor.rm_job as rm_job

        progress = os.path.join(self.tmp, "progress.json")
        shots: list[Any] = [
            {"uid": "a" * 32, "name": "shotA", "status": "DONE",
             "path": "p1.png", "error": ""},
            {"uid": "b" * 32, "name": "shotB", "status": "RENDERING",
             "path": "", "error": "", "samples": 80, "samples_total": 128},
        ]
        rm_job._stats.update(
            entry=None, start=None, last_write=0.0, progress_path=progress,
            total=2, shots=shots, mm=None, mm_path="",
        )
        try:
            rm_job._write_progress(progress, "running", 2, shots)
            self.ops._active["progress_path"] = progress
            payload = self.ops._read_progress()
        finally:
            rm_job._close_progress_mmap()
            self.ops._active["progress_path"] = ""
        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["shots"][0]["status"], "DONE")
        self.assertEqual(payload["shots"][1]["samples"], 80)

    def test_progress_mmap_rewrite_shorter(self):
        """多轮写入：第二次 payload 更短时，长度前缀保证读到新内容（旧尾部残留不影响）。"""
        import render_monitor.rm_job as rm_job

        progress = os.path.join(self.tmp, "progress.json")
        rm_job._stats.update(
            entry=None, start=None, last_write=0.0, progress_path=progress,
            total=1, shots=[], mm=None, mm_path="",
        )
        try:
            long_shots: list[Any] = [{
                "uid": "a" * 32, "name": "shotA" + "长" * 50,
                "status": "RENDERING", "path": "x.png",
                "error": "some long error message " * 10,
            }]
            rm_job._write_progress(progress, "running", 1, long_shots)
            short_shots: list[Any] = [{"uid": "a" * 32, "name": "A", "status": "DONE",
                                       "path": "", "error": ""}]
            rm_job._write_progress(progress, "running", 1, short_shots)
            self.ops._active["progress_path"] = progress
            payload = self.ops._read_progress()
        finally:
            rm_job._close_progress_mmap()
            self.ops._active["progress_path"] = ""
        self.assertEqual(payload["shots"][0]["status"], "DONE")
        self.assertEqual(payload["shots"][0]["name"], "A")

    def test_progress_mmap_bad_header_returns_none(self):
        """读端对非法长度前缀（如旧版纯 JSON 残留文件）返回 None，不抛异常。"""
        progress = os.path.join(self.tmp, "bad.json")
        with open(progress, "wb") as f:
            f.write(b"\xff\xff\xff\xff{}")  # 长度 0xFFFFFFFF 远超缓冲 → None
        self.ops._active["progress_path"] = progress
        try:
            self.assertIsNone(self.ops._read_progress())
        finally:
            self.ops._active["progress_path"] = ""

    def test_progress_mmap_read_empty_or_missing(self):
        """读端对不存在/空（子进程尚未写入）的文件返回 None，不抛异常。"""
        self.ops._active["progress_path"] = os.path.join(self.tmp, "nope.json")
        self.assertIsNone(self.ops._read_progress())
        empty = os.path.join(self.tmp, "empty.json")
        with open(empty, "wb"):
            pass
        self.ops._active["progress_path"] = empty
        try:
            self.assertIsNone(self.ops._read_progress())
        finally:
            self.ops._active["progress_path"] = ""

    # ------------------------------------------------------------------
    # 收尾会话状态修正（回归：子进程崩溃/致命错误时状态与计数一致）
    # ------------------------------------------------------------------

    def _setup_finish(self, uid_statuses):
        """构造 rm_shots 与会话状态，返回按 uid 顺序的 shot 列表。"""
        shots = [
            MockShot(uid, f"shot{i}", status=st)
            for i, (uid, st) in enumerate(uid_statuses)
        ]
        self.scene.rm_shots = MockShotList(shots)
        self.ops._active.update(
            process=None, scene_name="Scene", tmpdir=self.tmp,
            progress_path="", log_path="", total=len(shots),
            uids=[s.uid for s in shots],
        )
        return shots

    def test_finish_session_marks_interrupted_as_failed(self):
        """回归：子进程崩溃（有进度文件、state=running）时，正在渲染的
        快照标失败、未开始的保持待渲染；计数与状态一致、进度属性重置。"""
        shots = self._setup_finish([
            ("d" * 32, "DONE"),
            ("e" * 32, "RENDERING"),
            ("f" * 32, "PENDING"),
        ])
        with mock.patch.object(
            self.ops, "_read_progress",
            return_value={"state": "running", "total": 3, "shots": []},
        ):
            self.ops._finish_session(-1, cancelled=False)
        self.assertEqual([s.status for s in shots], ["DONE", "FAILED", "PENDING"])
        self.assertEqual(self.scene.rm_render_done, 1)
        self.assertEqual(self.scene.rm_render_failed, 1)
        self.assertEqual(self.scene.rm_render_progress, 0.0)
        self.assertEqual(self.scene.rm_render_current, "")
        self.assertIn("成功 1", self.scene.rm_last_message)
        self.assertIn("失败 1", self.scene.rm_last_message)

    def test_finish_session_fatal_error_marks_all_failed(self):
        """回归：子进程致命错误（state=error、含 uid="" 假条目）时，
        本次队列里未完成的 PENDING/RENDERING 全部标失败。"""
        shots = self._setup_finish([
            ("d" * 32, "PENDING"),
            ("e" * 32, "RENDERING"),
        ])
        with mock.patch.object(
            self.ops, "_read_progress",
            return_value={"state": "error", "total": 0, "shots": [
                {"uid": "", "name": "致命错误", "status": "FAILED",
                 "path": "", "error": "boom"},
            ]},
        ):
            self.ops._finish_session(1, cancelled=False)
        self.assertEqual([s.status for s in shots], ["FAILED", "FAILED"])
        self.assertEqual(self.scene.rm_render_failed, 2)
        self.assertIn("boom", self.scene.rm_last_message)

    def test_finish_session_cancelled_keeps_state_but_resets_progress(self):
        """用户主动停止（cancelled=True）：不把当前张标失败，但进度属性重置。"""
        shots = self._setup_finish([("e" * 32, "RENDERING")])
        self.scene.rm_render_progress = 0.75  # 残留进度
        with mock.patch.object(
            self.ops, "_read_progress",
            return_value={"state": "running", "total": 1, "shots": []},
        ):
            self.ops._finish_session(-1, cancelled=True)
        self.assertEqual(shots[0].status, "RENDERING")  # 保持，不标失败
        self.assertEqual(self.scene.rm_render_progress, 0.0)  # 但进度重置

    def test_update_ui_uses_active_total(self):
        """回归：子进程回传 total=0（致命错误）时，总数用启动时记录的
        _active["total"]，不被清零。"""
        self.scene.rm_shots = MockShotList([MockShot("d" * 32, "shotD")])
        self.ops._active.update(scene_name="Scene", total=5)
        self.ops._update_ui_from_progress({"state": "error", "total": 0, "shots": []})
        self.assertEqual(self.scene.rm_render_total, 5)

    def test_update_ui_propagates_shot_error(self):
        """渲染失败原因从子进程进度回传后，写入对应快照的 error 字段。"""
        shot = MockShot("d" * 32, "shotD")
        self.scene.rm_shots = MockShotList([shot])
        self.ops._active.update(scene_name="Scene", total=1)
        self.ops._update_ui_from_progress({
            "state": "done", "total": 1, "shots": [
                {"uid": shot.uid, "name": shot.name, "status": "FAILED",
                 "path": "", "error": "mock render boom"},
            ],
        })
        self.assertEqual(shot.status, "FAILED")
        self.assertEqual(shot.error, "mock render boom")

    def test_name_number_helper_uses_active_operator(self):
        """捕获弹窗里的 + 按钮直接修改当前 active_operator 的 shot_name。"""
        capture_op = SimpleNamespace(shot_name="快照001")
        op = self.ops.RM_OT_shot_name_number()
        op.delta = 1
        ctx = SimpleNamespace(active_operator=capture_op, area=None)
        self.assertEqual(op.execute(ctx), {"FINISHED"})
        self.assertEqual(capture_op.shot_name, "快照002")

        op.delta = -1
        self.assertEqual(op.execute(ctx), {"FINISHED"})
        self.assertEqual(capture_op.shot_name, "快照001")

    def test_name_number_helper_falls_back_to_dialog_operator(self):
        """某些情况下 active_operator 可能指向按钮自身，使用 invoke 时保存的引用兜底。"""
        dialog_op = SimpleNamespace(shot_name="快照")
        self.ops._capture_dialog_operator = dialog_op  # ty: ignore[invalid-assignment]
        # active_operator 不是捕获弹窗操作符（例如是按钮操作符本身）
        ctx = SimpleNamespace(active_operator=SimpleNamespace(delta=0), area=None)
        op = self.ops.RM_OT_shot_name_number()
        op.delta = 1
        self.assertEqual(op.execute(ctx), {"FINISHED"})
        self.assertEqual(dialog_op.shot_name, "快照1")


if __name__ == "__main__":
    unittest.main()
