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
from unittest import mock

from .test_core import Matrix, MockData, MockScene, Vector  # noqa: F401


class MockShot:
    """模拟 bpy.types.Scene.rm_shots 里的一个快照条目。"""

    def __init__(self, uid, name, status="PENDING", selected=True):
        self.uid = uid
        self.name = name
        self.status = status
        self.selected = selected
        self.output_path = ""
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


class TestOpsExportIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 注入 mock 模块，使 ops.py 的 import bpy/mathutils 成功
        math = types.ModuleType("mathutils")
        math.Vector = Vector
        math.Matrix = Matrix
        sys.modules["mathutils"] = math

        bpy_mod = types.ModuleType("bpy")
        # ops.py 的 Operator 子类在模块加载时求值基类表达式
        bpy_mod.types = SimpleNamespace(Operator=type("Operator", (), {}))
        bpy_mod.app = SimpleNamespace(
            binary_path="C:/fake/blender.exe",
            timers=SimpleNamespace(
                is_registered=lambda *a, **k: False,
                register=lambda *a, **k: None,
            ),
        )
        bpy_mod.data = MockData()
        bpy_mod.data.filepath = "C:/fake/project.blend"
        bpy_mod.path = SimpleNamespace(abspath=lambda p: p)
        bpy_mod.ops = SimpleNamespace(wm=SimpleNamespace())

        def fake_save(filepath, copy=True):
            os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
            with open(filepath, "wb") as f:
                f.write(b"fake-blend")
            return {"FINISHED"}

        bpy_mod.ops.wm.save_as_mainfile = fake_save
        sys.modules["bpy"] = bpy_mod
        cls.bpy_mod = bpy_mod

        cls.ops = importlib.import_module("render_monitor.ops")
        importlib.reload(cls.ops)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rm_ops_test_")
        self.addCleanup(self._cleanup_tmp)
        self.scene = MockScene("Scene")
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
            log_path="", total=0,
        )

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
        op.report = lambda *a, **k: None

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

    def test_index_is_list_position_for_single_selection(self):
        """「渲染当前」单张时，index = 该快照在列表中的位置。"""
        second = self.scene.rm_shots._shots[1]  # 列表第 2 个（shotB）
        ok, _msg, exported = self._run_export([second.uid])
        self.assertTrue(ok)
        self.assertEqual([e["index"] for e in exported], [2])


if __name__ == "__main__":
    unittest.main()
