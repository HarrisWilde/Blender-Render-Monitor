"""Render Monitor 核心模块单元测试（mock bpy/mathutils，不依赖真实 Blender）。

验证：
1. capture_scene_state 输出可 JSON 序列化（快照数据可持久化）
2. 修改场景后 apply_scene_state 能精确恢复到快照状态（往返一致）

运行：python -m unittest render_monitor.tests.test_core
"""

from __future__ import annotations

import importlib
import json
import sys
import types
import unittest


# ---------------------------------------------------------------------------
# 轻量 mock：mathutils
# ---------------------------------------------------------------------------

class Vector:
    def __init__(self, seq=()):
        self.v = [float(x) for x in seq]

    def __iter__(self):
        return iter(self.v)

    def to_list(self):
        return list(self.v)

    def __eq__(self, other):
        return list(self) == list(other)

    def __repr__(self):
        return f"Vector({self.v})"


class Matrix:
    def __init__(self, rows=()):
        self.rows = [list(row) for row in rows] or [
            [1, 0, 0, 0],
            [0, 1, 0, 0],
            [0, 0, 1, 0],
            [0, 0, 0, 1],
        ]

    def __iter__(self):
        return iter(self.rows)

    def to_list(self):
        return [list(r) for r in self.rows]

    def __eq__(self, other):
        return self.rows == other.rows

    def __repr__(self):
        return f"Matrix({self.rows})"


# ---------------------------------------------------------------------------
# 轻量 mock：bpy 数据对象
# ---------------------------------------------------------------------------

class MockObject:
    def __init__(self, name, type="MESH"):
        self.name = name
        self.type = type
        self.hide_viewport = False
        self.hide_render = False
        self.hide_select = False
        self.parent = None
        self.matrix_local = Matrix()
        # 变换直接数据（模拟真实 Blender 的 location/rotation/scale/delta）
        self.rotation_mode = "XYZ"
        self.location = Vector([0.0, 0.0, 0.0])
        self.rotation_euler = Vector([0.0, 0.0, 0.0])
        self.rotation_quaternion = Vector([1.0, 0.0, 0.0, 0.0])
        self.rotation_axis_angle = Vector([0.0, 1.0, 0.0, 0.0])
        self.scale = Vector([1.0, 1.0, 1.0])
        self.delta_location = Vector([0.0, 0.0, 0.0])
        self.delta_rotation_euler = Vector([0.0, 0.0, 0.0])
        self.delta_scale = Vector([1.0, 1.0, 1.0])
        self.data = None

    def __repr__(self):
        return f"MockObject({self.name})"


class MockCollection:
    def __init__(self, name):
        self.name = name
        self.hide_viewport = False
        self.hide_render = False
        self.objects = []
        self.children = []

    def __repr__(self):
        return f"MockCollection({self.name})"


class MockNodeInput:
    def __init__(self, value):
        self.default_value = value


class MockNode:
    def __init__(self, ntype):
        self.type = ntype
        self.inputs = {}


class MockNodeTree:
    def __init__(self):
        self.nodes = []


class MockWorld:
    def __init__(self, name):
        self.name = name
        self._use_nodes = False
        self.color = Vector([0, 0, 0])
        self.node_tree = None

    @property
    def use_nodes(self):
        return self._use_nodes

    @use_nodes.setter
    def use_nodes(self, value):
        # 模拟真实 Blender：开启 use_nodes 会自动创建含 Background 的默认节点树
        self._use_nodes = bool(value)
        if self._use_nodes and self.node_tree is None:
            tree = MockNodeTree()
            bg = MockNode("BACKGROUND")
            bg.inputs["Color"] = MockNodeInput(Vector([0.8, 0.8, 0.8, 1]))
            bg.inputs["Strength"] = MockNodeInput(1.0)
            tree.nodes.append(bg)
            self.node_tree = tree


class MockLightData:
    def __init__(self, energy=1000.0, color=(1, 1, 1)):
        self.energy = energy
        self.color = Vector(color)


def _make_render_settings():
    img = types.SimpleNamespace(
        file_format="PNG", color_mode="RGB", color_depth="8", compression=15, quality=90
    )
    return types.SimpleNamespace(
        engine="BLENDER_EEVEE",
        resolution_x=1920,
        resolution_y=1080,
        resolution_percentage=100,
        film_transparent=False,
        use_motion_blur=False,
        use_denoising=True,
        file_extension=".png",
        image_settings=img,
    )


class MockScene:
    def __init__(self, name="Scene"):
        self.name = name
        self.frame_current = 1
        self.camera = None
        self.world = None
        self.collection = MockCollection("Master")
        self.view_layers = MockNamedCollection("name")
        self.render = _make_render_settings()
        self.cycles = types.SimpleNamespace(
            samples=64, preview_samples=16, max_bounces=8,
            device="GPU", use_denoising=True,
        )
        self.eevee = types.SimpleNamespace(
            taa_render_samples=16, use_raytracing=True,
            gi_diffuse_bounces=3, motion_blur_max=2,
        )


class MockLayerCollection:
    """模拟 bpy.types.LayerCollection：视图层级集合开关。"""

    def __init__(self, collection, exclude=False, hide_viewport=False):
        self.collection = collection
        self.exclude = exclude
        self.hide_viewport = hide_viewport
        self.children = []


class MockViewLayer:
    def __init__(self, name="ViewLayer"):
        self.name = name
        self.layer_collection = None


class MockNamedCollection:
    """模拟 bpy.data.objects / worlds / collections 的 get()。"""

    active_index = 0  # 模拟 view_layers 的 active_index

    def __init__(self, key):
        self._d = {}
        self.key = key

    def add(self, obj):
        self._d[getattr(obj, self.key)] = obj

    def get(self, name):
        return self._d.get(name)

    def __iter__(self):
        return iter(self._d.values())

    def __len__(self):
        return len(self._d)


class MockData:
    def __init__(self):
        self.objects = MockNamedCollection("name")
        self.worlds = MockNamedCollection("name")
        self.collections = MockNamedCollection("name")


class MockBpy:
    def __init__(self):
        self.data = MockData()


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

class TestCoreStateRoundtrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 注入 mock 模块，使 core.py 的 import bpy/mathutils 成功
        math = types.ModuleType("mathutils")
        math.Vector = Vector
        math.Matrix = Matrix
        sys.modules["mathutils"] = math

        bpy_mod = types.ModuleType("bpy")
        cls.bpy_mock = MockBpy()
        bpy_mod.data = cls.bpy_mock.data
        sys.modules["bpy"] = bpy_mod

        cls.core = importlib.import_module("render_monitor.core")
        importlib.reload(cls.core)  # 让 core 拿到带 mock 的 bpy/mathutils

    def _make_world(self):
        world = MockWorld("World1")
        world.use_nodes = True
        tree = MockNodeTree()
        bg = MockNode("BACKGROUND")
        bg.inputs["Color"] = MockNodeInput(Vector([0.05, 0.05, 0.05, 1]))
        bg.inputs["Strength"] = MockNodeInput(1.0)
        tree.nodes.append(bg)
        world.node_tree = tree
        return world

    def _build_scene(self):
        scene = MockScene()
        data = self.bpy_mock.data

        # 主集合 + 子集合
        sub = MockCollection("Sub")
        data.collections.add(sub)
        scene.collection.children.append(sub)

        # 物体：父 + 子
        parent = MockObject("Parent", "EMPTY")
        child = MockObject("Child", "MESH")
        child.parent = parent
        child.location = Vector([2.0, 0.0, 0.0])
        light = MockObject("Key", "LIGHT")
        light.data = MockLightData(energy=500.0, color=(1, 0.8, 0.6))
        cam = MockObject("Cam", "CAMERA")
        hidden = MockObject("Hidden", "MESH")
        hidden.hide_viewport = True
        hidden.hide_render = True

        for obj in (parent, child, light, cam):
            scene.collection.objects.append(obj)
            data.objects.add(obj)
        sub.objects.append(hidden)
        data.objects.add(hidden)

        scene.world = self._make_world()
        data.worlds.add(scene.world)
        scene.camera = cam
        scene.frame_current = 42

        # 视图层集合开关：Master 正常，Sub 被勾掉（exclude）
        vl = MockViewLayer("ViewLayer")
        master_lc = MockLayerCollection(scene.collection)
        sub_lc = MockLayerCollection(sub, exclude=True, hide_viewport=True)
        master_lc.children.append(sub_lc)
        vl.layer_collection = master_lc
        scene.view_layers.add(vl)

        scene.render.engine = "CYCLES"
        scene.cycles.samples = 256
        scene.render.image_settings.file_format = "OPEN_EXR"
        return scene

    def test_capture_is_json_serializable(self):
        scene = self._build_scene()
        state = self.core.capture_scene_state(scene)
        # 关键：必须能序列化为 JSON（快照会存进 StringProperty）
        dumped = json.dumps(state, ensure_ascii=False)
        loaded = json.loads(dumped)
        self.assertEqual(loaded["frame_current"], 42)
        self.assertEqual(loaded["camera"], "Cam")
        self.assertEqual(loaded["objects"][1]["location"][0], 2.0)
        self.assertEqual(loaded["world"]["background"]["strength"], 1.0)
        # 渲染设置（Cycles 采样）
        prop_map = {p[0]: p[1] for p in loaded["render"]["props"]}
        self.assertEqual(prop_map["cycles.samples"], 256)
        self.assertEqual(prop_map["render.engine"], "CYCLES")

    def test_apply_restores_state(self):
        scene = self._build_scene()
        state = self.core.capture_scene_state(scene)

        # 破坏场景状态
        scene.frame_current = 1
        scene.camera = None
        scene.render.engine = "BLENDER_EEVEE"
        scene.cycles.samples = 8
        scene.world.use_nodes = False
        scene.world.color = Vector([1, 0, 0])
        scene.world.node_tree = None
        for obj in scene.collection.objects:
            obj.location = Vector([0.0, 0.0, 0.0])
            obj.rotation_euler = Vector([0.0, 0.0, 0.0])
            obj.scale = Vector([1.0, 1.0, 1.0])
            obj.hide_viewport = False
            obj.hide_render = False
        sub = scene.collection.children[0]
        sub.hide_viewport = True
        sub.hide_render = True
        sub.objects[0].location = Vector([0.0, 0.0, 0.0])
        scene.render.resolution_x = 640

        # 应用快照恢复
        self.core.apply_scene_state(scene, state)

        # 逐项验证
        self.assertEqual(scene.frame_current, 42)
        self.assertEqual(scene.camera.name, "Cam")
        self.assertEqual(scene.render.engine, "CYCLES")
        self.assertEqual(scene.cycles.samples, 256)
        self.assertEqual(scene.render.resolution_x, 1920)
        # use_nodes 恢复为 True（mock 模拟真实 Blender 自动建节点树）
        self.assertTrue(scene.world.use_nodes)
        # 背景节点恢复
        bg = scene.world.node_tree.nodes[0]
        self.assertEqual(bg.inputs["Strength"].default_value, 1.0)
        self.assertEqual(list(bg.inputs["Color"].default_value), [0.05, 0.05, 0.05, 1])
        # 物体矩阵与可见性
        objs = {o.name: o for o in scene.collection.objects}
        self.assertEqual(list(objs["Child"].location)[0], 2.0)
        self.assertEqual(objs["Key"].data.energy, 500.0)
        self.assertEqual(list(objs["Key"].data.color), [1.0, 0.8, 0.6])
        # 隐藏物体
        hidden = scene.collection.children[0].objects[0]
        self.assertTrue(hidden.hide_viewport)
        self.assertTrue(hidden.hide_render)
        # 集合开关
        self.assertFalse(scene.collection.children[0].hide_viewport)
        self.assertFalse(scene.collection.children[0].hide_render)

    def test_apply_camera_by_name_after_rename_safe(self):
        # 相机被删时不应崩溃
        scene = self._build_scene()
        state = self.core.capture_scene_state(scene)
        scene.camera = None
        del self.bpy_mock.data.objects._d["Cam"]
        self.core.apply_scene_state(scene, state)  # 不应抛异常
        self.assertIsNone(scene.camera)

    def test_apply_camera_none_restores_none(self):
        # 快照录制时无相机 → 恢复后 scene.camera 也应被置空（而非残留旧相机）
        scene = self._build_scene()
        scene.camera = None
        state = self.core.capture_scene_state(scene)
        # 恢复前先给场景设一个相机，再应用无相机快照
        cam = MockObject("NewCam", "CAMERA")
        self.bpy_mock.data.objects.add(cam)
        scene.camera = cam
        self.core.apply_scene_state(scene, state)
        self.assertIsNone(scene.camera)

    def test_missing_world_is_ok(self):
        scene = self._build_scene()
        scene.world = None
        state = self.core.capture_scene_state(scene)
        self.assertIsNone(state["world"])
        self.core.apply_scene_state(scene, state)  # 不应抛异常

    def test_excluded_collection_transform_roundtrip(self):
        """回归（v1.4.4）：exclude 集合中的对象 matrix_local 可能是依赖图未
        评估的默认值（单位矩阵），快照必须用 location/rotation/scale 记录，
        应用后位置旋转不归零。"""
        scene = self._build_scene()
        hidden = scene.collection.children[0].objects[0]
        hidden.location = Vector([7.0, -3.0, 2.0])
        hidden.rotation_euler = Vector([1.0, 0.5, 0.25])
        hidden.matrix_local = Matrix()  # 模拟依赖图未评估 -> 单位矩阵
        state = self.core.capture_scene_state(scene)

        cap = [o for o in state["objects"] if o["name"] == "Hidden"][0]
        self.assertIn("location", cap)  # v3 新格式：直接数据
        self.assertEqual(cap["location"], [7.0, -3.0, 2.0])
        self.assertEqual(cap["rotation"], [1.0, 0.5, 0.25])

        # 破坏后应用，位置旋转应完整恢复（不归零）
        hidden.location = Vector([0.0, 0.0, 0.0])
        hidden.rotation_euler = Vector([0.0, 0.0, 0.0])
        self.core.apply_scene_state(scene, state)
        self.assertEqual(list(hidden.location), [7.0, -3.0, 2.0])
        self.assertEqual(list(hidden.rotation_euler), [1.0, 0.5, 0.25])

    def test_legacy_snapshot_matrix_local_fallback(self):
        """旧快照（v1/v2，无 location/rotation/scale 字段）回退用 matrix_local。"""
        scene = self._build_scene()
        state = self.core.capture_scene_state(scene)
        # 模拟旧格式：移除 v3 字段，仅保留 matrix_local
        for o in state["objects"]:
            o.pop("location", None)
            o.pop("rotation", None)
            o.pop("rotation_mode", None)
            o.pop("scale", None)
            o.pop("delta_location", None)
            o.pop("delta_rotation_euler", None)
            o.pop("delta_scale", None)
            o["matrix_local"] = [
                [1, 0, 0, 5], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]
            ]
        child = [o for o in scene.collection.objects if o.name == "Child"][0]
        child.matrix_local = Matrix()
        self.core.apply_scene_state(scene, state)
        # 回退分支：matrix_local 被恢复（x 平移 5）
        self.assertEqual(child.matrix_local.rows[0][3], 5.0)

    def test_view_layer_exclude_roundtrip(self):
        # 集合的视图层级开关（outliner 勾选框 = exclude）应被捕获并恢复
        scene = self._build_scene()
        state = self.core.capture_scene_state(scene)
        vl_state = state["view_layers"][0]
        self.assertEqual(vl_state["name"], "ViewLayer")
        by_name = {c["name"]: c for c in vl_state["collections"]}
        self.assertTrue(by_name["Sub"]["exclude"])
        self.assertTrue(by_name["Sub"]["hide_viewport"])

        # 破坏：把 Sub 的 exclude 打开
        vl = scene.view_layers.get("ViewLayer")
        sub_lc = [c for c in self.core.iter_layer_collections(vl) if c.collection.name == "Sub"][0]
        sub_lc.exclude = False
        sub_lc.hide_viewport = False
        self.assertFalse(sub_lc.exclude)

        # 应用快照恢复
        self.core.apply_scene_state(scene, state)
        sub_lc = [c for c in self.core.iter_layer_collections(vl) if c.collection.name == "Sub"][0]
        self.assertTrue(sub_lc.exclude)
        self.assertTrue(sub_lc.hide_viewport)
        master_lc = [c for c in self.core.iter_layer_collections(vl) if c.collection.name == "Master"][0]
        self.assertFalse(master_lc.exclude)

    def test_world_switch_restored(self):
        # 场景切换到另一个 World 后，应用快照应切回快照记录的 World
        scene = self._build_scene()
        world1 = scene.world
        self.assertEqual(world1.name, "World1")
        state = self.core.capture_scene_state(scene)

        # 切换到另一个 World（模拟用户换环境）
        world2 = MockWorld("World2")
        world2.use_nodes = True
        self.bpy_mock.data.worlds.add(world2)
        scene.world = world2

        self.core.apply_scene_state(scene, state)
        self.assertIs(scene.world, world1)
        # 背景节点参数也恢复
        bg = world1.node_tree.nodes[0]
        self.assertEqual(bg.inputs["Strength"].default_value, 1.0)


if __name__ == "__main__":
    unittest.main()
