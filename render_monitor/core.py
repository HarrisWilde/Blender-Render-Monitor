"""Render Monitor - 核心：场景状态快照的捕获与恢复（依赖 bpy）。

快照覆盖：物体状态（可见性 + 本地变换 + 灯光参数）、集合开关、环境
（World 背景）、活动相机、渲染设置、当前帧。数据结构为纯 JSON 兼容
dict，可序列化存入 Blender 的 StringProperty 持久化。
"""

from __future__ import annotations

try:
    import bpy
    from mathutils import Matrix, Vector
except ImportError:  # 非 Blender 环境（如单元测试）下允许导入模块
    bpy = None
    Matrix = Vector = None

# 快照格式版本
# v2: 新增 view_layers（集合 exclude 开关）与 active_view_layer 记录
SNAPSHOT_VERSION = 2

# 渲染设置捕获/恢复的属性路径（相对 scene）。
# 恢复时逐个 try/except，属性不存在或引擎不支持时自动跳过（容错）。
RENDER_PROP_PATHS = [
    ("engine", "render.engine"),
    ("resolution_x", "render.resolution_x"),
    ("resolution_y", "render.resolution_y"),
    ("resolution_percentage", "render.resolution_percentage"),
    ("film_transparent", "render.film_transparent"),
    ("use_motion_blur", "render.use_motion_blur"),
    ("use_denoising", "render.use_denoising"),
    # 输出格式
    ("file_format", "render.image_settings.file_format"),
    ("color_mode", "render.image_settings.color_mode"),
    ("color_depth", "render.image_settings.color_depth"),
    ("compression", "render.image_settings.compression"),
    ("quality", "render.image_settings.quality"),
    # Cycles
    ("cycles_samples", "cycles.samples"),
    ("cycles_preview_samples", "cycles.preview_samples"),
    ("cycles_max_bounces", "cycles.max_bounces"),
    ("cycles_device", "cycles.device"),
    ("cycles_use_denoising", "cycles.use_denoising"),
    # EEVEE（Blender 5.x 采样属性为 taa_render_samples）
    ("eevee_taa_render_samples", "eevee.taa_render_samples"),
    ("eevee_use_raytracing", "eevee.use_raytracing"),
    ("eevee_gi_diffuse_bounces", "eevee.gi_diffuse_bounces"),
    ("eevee_use_motion_blur", "eevee.motion_blur_max"),  # 5.x 无布尔开关，记录步数做参考
]


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _get_prop(obj, path):
    cur = obj
    for part in path.split("."):
        cur = getattr(cur, part)
    return cur


def _set_prop(obj, path, value):
    parts = path.split(".")
    target = obj
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)


def _jsonify(value):
    """把 RNA/mathutils 值转成 JSON 友好值。"""
    if hasattr(value, "to_list"):
        return [float(v) for v in value.to_list()]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


def iter_scene_collections(scene):
    """递归产出场景树中的集合（按名称去重）。"""
    seen = set()

    def rec(col):
        if col.name in seen:
            return
        seen.add(col.name)
        yield col
        for child in col.children:
            yield from rec(child)

    yield from rec(scene.collection)


def iter_scene_objects(scene):
    """递归产出场景树中的对象（按名称去重，bpy.data.objects 名称唯一）。"""
    seen = set()

    def rec(col):
        for obj in col.objects:
            if obj.name in seen:
                continue
            seen.add(obj.name)
            yield obj
        for child in col.children:
            yield from rec(child)

    yield from rec(scene.collection)


def iter_layer_collections(view_layer):
    """递归产出 view layer 的 layer_collection 树（含嵌套子集合）。"""
    try:
        root = view_layer.layer_collection
    except AttributeError:
        return

    def rec(lc):
        yield lc
        for child in lc.children:
            yield from rec(child)

    yield from rec(root)


def _active_view_layer_index(scene):
    """返回当前 active view layer 的索引（容错，取不到返回 None）。"""
    try:
        return scene.view_layers.active_index
    except (AttributeError, TypeError):
        try:
            for i, vl in enumerate(scene.view_layers):
                if getattr(vl, "use", True) and vl == getattr(scene.view_layers, "active", None):
                    return i
        except Exception:  # noqa: BLE001
            pass
        return None


# ---------------------------------------------------------------------------
# World（环境）
# ---------------------------------------------------------------------------

def capture_world(world):
    """捕获 World 状态；无 World 时返回 None。"""
    if world is None:
        return None
    st = {
        "name": world.name,
        "use_nodes": world.use_nodes,
        "color": list(world.color),
        "background": None,
    }
    if world.use_nodes and world.node_tree:
        for node in world.node_tree.nodes:
            if node.type == "BACKGROUND":
                try:
                    st["background"] = {
                        "color": list(node.inputs["Color"].default_value),
                        "strength": node.inputs["Strength"].default_value,
                    }
                except (KeyError, AttributeError):
                    st["background"] = None
                break
    return st


def apply_world(scene, state):
    """恢复 World：把 scene.world 切换到快照记录的 World 并恢复其参数。

    按名称查找，找不到或出错时静默跳过。
    """
    if not state:
        return
    world = bpy.data.worlds.get(state["name"])
    if world is None:
        return
    try:
        scene.world = world
    except Exception:
        pass
    # Blender 5.0 起 World.use_nodes 已废弃（设置无效果、读取恒 True），
    # 保留赋值仅兼容旧版本；背景节点恢复不再依赖该开关。
    try:
        world.use_nodes = bool(state.get("use_nodes", True))
    except Exception:
        pass
    try:
        world.color = Vector(state["color"])
    except Exception:
        pass
    bg = state.get("background")
    if bg and world.node_tree:
        for node in world.node_tree.nodes:
            if node.type == "BACKGROUND":
                try:
                    node.inputs["Color"].default_value = bg["color"]
                    node.inputs["Strength"].default_value = bg["strength"]
                except (KeyError, AttributeError):
                    pass
                break


# ---------------------------------------------------------------------------
# 渲染设置
# ---------------------------------------------------------------------------

def capture_render(scene):
    """捕获渲染设置（属性路径 + 值 列表）。"""
    props = []
    for _key, path in RENDER_PROP_PATHS:
        try:
            value = _get_prop(scene, path)
            props.append([path, _jsonify(value)])
        except (AttributeError, IndexError, KeyError):
            continue
    return {"props": props}


def apply_render(scene, render_state):
    """恢复渲染设置；逐条容错。"""
    for path, value in render_state.get("props", []):
        try:
            _set_prop(scene, path, value)
        except (AttributeError, TypeError, ValueError, IndexError, KeyError):
            continue


# ---------------------------------------------------------------------------
# 集合与对象
# ---------------------------------------------------------------------------

def _capture_object(obj):
    st = {
        "name": obj.name,
        "type": obj.type,
        "hide_viewport": obj.hide_viewport,
        "hide_render": obj.hide_render,
        "hide_select": obj.hide_select,
        "parent": obj.parent.name if obj.parent else None,
        "matrix_local": [list(row) for row in obj.matrix_local],
    }
    # 逐对象渲染可见性（存在才记录，容错）
    for attr in (
        "visible_camera",
        "visible_shadow",
        "visible_glossy",
        "visible_diffuse",
        "visible_transmission",
        "visible_volume_scatter",
    ):
        try:
            st[attr] = getattr(obj, attr)
        except AttributeError:
            pass
    # 灯光参数（环境照明强度/颜色/阴影开关）
    if obj.type == "LIGHT" and obj.data:
        try:
            st["light_data"] = {
                "energy": obj.data.energy,
                "color": list(obj.data.color),
            }
            try:
                st["light_data"]["use_shadow"] = obj.data.use_shadow
            except AttributeError:
                pass
        except AttributeError:
            pass
    return st


def _parent_depth(objs_by_name, name, memo):
    """计算对象在父链中的深度，用于先父后子恢复。"""
    if name in memo:
        return memo[name]
    o = objs_by_name.get(name)
    if not o or not o.get("parent"):
        memo[name] = 0
    else:
        memo[name] = 1 + _parent_depth(objs_by_name, o["parent"], memo)
    return memo[name]


def capture_scene_state(scene, include_render=True):
    """捕获场景完整状态，返回 JSON 兼容 dict。"""
    state = {
        "version": SNAPSHOT_VERSION,
        "frame_current": scene.frame_current,
        "camera": scene.camera.name if scene.camera else None,
        "world": capture_world(scene.world),
        "collections": [
            {
                "name": col.name,
                "hide_viewport": col.hide_viewport,
                "hide_render": col.hide_render,
            }
            for col in iter_scene_collections(scene)
        ],
        # 视图层级集合开关（outliner 里集合左侧的勾选框 = exclude；
        # 眼睛图标 = hide_viewport），逐 view layer 记录；
        # 同时记录 active view layer，确保渲染时用的是快照对应的视图层
        "view_layers": [
            {
                "name": vl.name,
                "collections": [
                    {
                        "name": lc.collection.name,
                        "exclude": lc.exclude,
                        "hide_viewport": lc.hide_viewport,
                    }
                    for lc in iter_layer_collections(vl)
                ],
            }
            for vl in scene.view_layers
        ],
        "active_view_layer": _active_view_layer_index(scene),
        "objects": [_capture_object(o) for o in iter_scene_objects(scene)],
        "render": capture_render(scene) if include_render else {"props": []},
    }
    return state


def apply_scene_state(scene, state):
    """把快照状态恢复到场景。恢复顺序：帧 → 渲染设置 → 环境 → 集合 → 对象 → 相机。"""
    if not state:
        return

    if "frame_current" in state:
        try:
            scene.frame_current = int(state["frame_current"])
        except (TypeError, ValueError):
            pass

    if "render" in state:
        apply_render(scene, state["render"])

    # 世界环境：切换到快照记录的 World（scene.world = 快照的 world）
    if "world" in state:
        if state["world"] is None:
            try:
                scene.world = None
            except Exception:
                pass
        else:
            apply_world(scene, state["world"])

    for cs in state.get("collections", []):
        col = bpy.data.collections.get(cs["name"])
        if col is None:
            continue
        try:
            col.hide_viewport = cs["hide_viewport"]
        except Exception:
            pass
        try:
            col.hide_render = cs["hide_render"]
        except Exception:
            pass

    # 视图层级集合开关（outliner 勾选框 = exclude / 眼睛 = hide_viewport）
    # 先恢复 active view layer，确保渲染用的是快照对应的视图层
    active_idx = state.get("active_view_layer")
    if active_idx is not None:
        try:
            scene.view_layers.active_index = active_idx
        except Exception:  # noqa: BLE001
            pass
    for vl_state in state.get("view_layers", []):
        try:
            vl = scene.view_layers.get(vl_state["name"])
        except (AttributeError, TypeError):
            continue
        if vl is None:
            continue
        for lc_state in vl_state.get("collections", []):
            for lc in iter_layer_collections(vl):
                if lc.collection.name != lc_state["name"]:
                    continue
                try:
                    lc.exclude = lc_state["exclude"]
                except Exception:
                    pass
                try:
                    lc.hide_viewport = lc_state["hide_viewport"]
                except Exception:
                    pass
                break

    # 对象：先父后子，避免父级变换未恢复导致子级偏移
    objs = state.get("objects", [])
    objs_by_name = {o["name"]: o for o in objs}
    memo = {}
    ordered = sorted(
        objs, key=lambda o: _parent_depth(objs_by_name, o["name"], memo)
    )
    for o in ordered:
        obj = bpy.data.objects.get(o["name"])
        if obj is None:
            continue
        try:
            obj.hide_viewport = o["hide_viewport"]
            obj.hide_render = o["hide_render"]
            obj.hide_select = o["hide_select"]
        except Exception:
            pass
        for attr in (
            "visible_camera",
            "visible_shadow",
            "visible_glossy",
            "visible_diffuse",
            "visible_transmission",
            "visible_volume_scatter",
        ):
            if attr in o:
                try:
                    setattr(obj, attr, o[attr])
                except Exception:
                    pass
        try:
            obj.matrix_local = Matrix(o["matrix_local"])
        except Exception:
            pass
        if "light_data" in o and obj.type == "LIGHT" and obj.data:
            try:
                obj.data.energy = o["light_data"]["energy"]
            except Exception:
                pass
            try:
                obj.data.color = Vector(o["light_data"]["color"])
            except Exception:
                pass
            if "use_shadow" in o["light_data"]:
                try:
                    obj.data.use_shadow = o["light_data"]["use_shadow"]
                except Exception:
                    pass

    # 活动相机（最后设置，避免被其他恢复操作影响）；
    # 快照含 camera 键时始终赋值（含 None，保证"无相机"也被还原）
    if "camera" in state:
        cam_name = state.get("camera")
        cam = bpy.data.objects.get(cam_name) if cam_name else None
        scene.camera = cam if (cam is not None and cam.type == "CAMERA") else None
