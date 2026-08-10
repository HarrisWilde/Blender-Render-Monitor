"""Render Monitor 渲染监视器 - Blender 插件入口。

功能：像 KeyShot 的 Render Monitor 一样，把当前场景的状态（物体隐藏/显示、
位移/旋转/缩放、集合开关、环境 World、活动相机、渲染设置、当前帧）捕获为
"快照"，逐个应用并批量渲染到指定路径；渲染结束后自动恢复场景原状，不打扰
正在进行的其他工作。

安装：使用项目根目录的 render_monitor.zip —— zip 内第一层是 render_monitor/
文件夹（内含 __init__.py），这是 Blender 多文件（包）插件的标准结构；
在 Blender 偏好设置 → 插件 中安装该 zip 即可。
"""

from __future__ import annotations

bl_info = {
    "name": "Render Monitor 渲染监视器",
    "author": "Render Monitor Contributors",
    "version": (1, 2, 0),
    "blender": (5, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Render Monitor",
    "description": "把场景状态记录为快照并批量后台渲染（UI 不冻结），类似 KeyShot 的 Render Monitor",
    "warning": "",
    "doc_url": "",
    "category": "Render",
}

try:
    import bpy
    from bpy.props import (
        BoolProperty,
        CollectionProperty,
        EnumProperty,
        IntProperty,
        StringProperty,
    )
    from bpy.types import Panel, PropertyGroup, UIList
except ImportError:  # 非 Blender 环境（如单元测试）下允许导入包
    bpy = None
    BoolProperty = CollectionProperty = EnumProperty = IntProperty = StringProperty = None
    Panel = PropertyGroup = UIList = None

from . import core  # noqa: E402


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

def _status_items():
    return [
        ("PENDING", "待渲染", "尚未渲染"),
        ("RENDERING", "渲染中", "正在渲染该快照"),
        ("DONE", "已完成", "渲染成功"),
        ("FAILED", "失败", "渲染出错"),
    ]


if bpy is not None:

    class RMShot(PropertyGroup):
        """单个快照条目：名称/状态/输出文件 + 完整快照数据（JSON 字符串）。"""

        uid: StringProperty(name="UID", default="")
        name: StringProperty(name="名称", default="快照")
        data_json: StringProperty(name="快照数据", default="{}")
        status: EnumProperty(name="状态", items=_status_items(), default="PENDING")
        output_path: StringProperty(name="输出文件", default="")


# ---------------------------------------------------------------------------
# 场景属性
# ---------------------------------------------------------------------------

def register_scene_props():
    bpy.types.Scene.rm_shots = CollectionProperty(type=RMShot)
    bpy.types.Scene.rm_shots_active = IntProperty(name="活动快照", default=0, min=0)
    bpy.types.Scene.rm_output_dir = StringProperty(
        name="输出目录",
        subtype="DIR_PATH",
        default="//",
        description="渲染输出目录，默认输出到当前 .blend 文件所在目录；支持 // 相对路径，未保存的文件请选择绝对路径",
    )
    bpy.types.Scene.rm_write_log = BoolProperty(
        name="输出渲染日志",
        default=False,
        description="在输出目录生成 rm_render_log_*.txt（含快照版本与视图层验证结果），排查问题时再勾选",
    )
    bpy.types.Scene.rm_file_template = StringProperty(
        name="文件命名模板",
        default="{name} {index}",
        description="输出文件名模板：{name}（快照名）、{index}（列表顺序，从 1 开始）、{frame}（帧号）",
    )
    bpy.types.Scene.rm_only_pending = BoolProperty(
        name="仅渲染未完成",
        default=True,
        description="批量渲染时跳过状态为「已完成」的快照",
    )
    bpy.types.Scene.rm_use_snapshot_frame = BoolProperty(
        name="使用快照帧",
        default=True,
        description="渲染时把场景切到快照记录的帧号（关闭则用当前帧）",
    )
    bpy.types.WindowManager.rm_busy = BoolProperty(
        name="渲染进行中",
        default=False,
        description="Render Monitor 正在渲染，禁止并发启动其他渲染任务",
    )
    bpy.types.Scene.rm_render_done = IntProperty(name="已完成", default=0, min=0)
    bpy.types.Scene.rm_render_failed = IntProperty(name="失败", default=0, min=0)
    bpy.types.Scene.rm_render_total = IntProperty(name="总计", default=0, min=0)
    bpy.types.Scene.rm_render_current = StringProperty(name="当前渲染", default="")
    bpy.types.Scene.rm_render_samples = StringProperty(name="当前采样", default="")
    bpy.types.Scene.rm_render_samples_cur = IntProperty(name="当前采样数", default=0, min=0)
    bpy.types.Scene.rm_render_samples_total = IntProperty(name="总采样数", default=0, min=0)
    bpy.types.Scene.rm_render_time = StringProperty(name="已用时间", default="")
    bpy.types.Scene.rm_render_remaining = StringProperty(name="预计剩余", default="")
    bpy.types.Scene.rm_last_message = StringProperty(name="最近消息", default="")


def unregister_scene_props():
    # 逐项容错，避免注册中途失败时注销流程被阻断
    for attr in (
        "rm_shots",
        "rm_shots_active",
        "rm_output_dir",
        "rm_file_template",
        "rm_only_pending",
        "rm_use_snapshot_frame",
        "rm_write_log",
        "rm_render_done",
        "rm_render_failed",
        "rm_render_total",
        "rm_render_current",
        "rm_render_samples",
        "rm_render_samples_cur",
        "rm_render_samples_total",
        "rm_render_time",
        "rm_render_remaining",
        "rm_last_message",
    ):
        try:
            delattr(bpy.types.Scene, attr)
        except AttributeError:
            pass
    try:
        del bpy.types.WindowManager.rm_busy
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# 注册
# ---------------------------------------------------------------------------

def register():
    # 官方示例：先注册类，再挂 CollectionProperty 到 Scene
    bpy.utils.register_class(RMShot)
    register_scene_props()
    from . import ops, ui  # noqa: F401

    for cls in ops.OPERATOR_CLASSES + ui.UI_CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    from . import ops, ui  # noqa: F401

    # 渲染进行中卸载插件：终止子进程、注销 timer、解除 busy，避免遗留孤儿子进程
    try:
        ops._stop_active()
    except Exception:  # noqa: BLE001
        pass
    try:
        bpy.app.timers.unregister(ops._poll_render_timer)
    except Exception:  # noqa: BLE001
        pass

    for cls in reversed(ops.OPERATOR_CLASSES + ui.UI_CLASSES):
        bpy.utils.unregister_class(cls)
    unregister_scene_props()
    try:
        bpy.utils.unregister_class(RMShot)
    except Exception:
        pass


if __name__ == "__main__":
    if bpy is not None:
        register()
