"""Render Monitor - 3D 视图侧边栏 (N 面板) 界面。"""

from __future__ import annotations

import json

import bpy
from bpy.types import Panel, UIList

_STATUS_ICONS = {
    "PENDING": "TIME",
    "RENDERING": "RENDER_STILL",
    "DONE": "CHECKMARK",
    "FAILED": "ERROR",
}


class RM_UL_shots(UIList):
    """快照列表：名称 + 状态 + 输出文件。"""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            row.prop(item, "name", text="", emboss=False,
                     icon=_STATUS_ICONS.get(item.status, "TIME"))
            if item.status == "DONE" and item.output_path:
                row.label(text="", icon="FILE_TICK")
        elif self.layout_type == "GRID":
            layout.alignment = "CENTER"
            layout.label(text="", icon=_STATUS_ICONS.get(item.status, "TIME"))


class RM_PT_panel(Panel):
    bl_idname = "RM_PT_panel"
    bl_label = "Render Monitor"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Render Monitor"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        scene = context.scene
        layout = self.layout

        # ---- 快照列表（显示所属场景，便于区分多场景下的独立快照）----
        layout.label(text=f"场景：{scene.name}（共 {len(scene.rm_shots)} 个快照）",
                     icon="SCENE_DATA")
        row = layout.row()
        row.template_list("RM_UL_shots", "", scene, "rm_shots", scene, "rm_shots_active")
        col = row.column(align=True)
        col.operator("rm.move_up", text="", icon="TRIA_UP")
        col.operator("rm.move_down", text="", icon="TRIA_DOWN")
        col.separator()
        col.operator("rm.delete", text="", icon="X")

        # ---- 管理按钮 ----
        row = layout.row(align=True)
        row.operator("rm.capture", text="捕获快照", icon="ADD")
        row.operator("rm.apply", text="应用", icon="IMPORT")
        row = layout.row(align=True)
        row.operator("rm.update", text="更新选中", icon="FILE_REFRESH")
        row.operator("rm.clear_done", text="清空已完成", icon="TRASH")
        row = layout.row(align=True)
        row.operator("rm.clear_all", text="清空全部快照", icon="X")

        # ---- 渲染设置 ----
        box = layout.box()
        box.label(text="批量渲染（后台执行，UI 不冻结）", icon="RENDER_STILL")
        box.prop(scene, "rm_output_dir")
        box.prop(scene, "rm_file_template")
        row = box.row()
        row.prop(scene, "rm_only_pending")
        row.prop(scene, "rm_use_snapshot_frame")
        row = box.row()
        row.prop(scene, "rm_write_log")

        # ---- 渲染按钮 ----
        if context.window_manager.rm_busy:
            row = box.row(align=True)
            row.operator("rm.stop_render", text="停止渲染", icon="CANCEL")
            # 进度
            total = max(scene.rm_render_total, 1)
            done = scene.rm_render_done + scene.rm_render_failed
            row2 = box.row()
            row2.progress(factor=min(done / total, 1.0))
            row2.label(text=f"{done}/{scene.rm_render_total}")
            box.label(text=f"正在渲染：{scene.rm_render_current or '…'}", icon="SORTTIME")
            # 当前张实时统计：整体进度条 / 采样（含块进度）/ 已用时间 / 剩余
            if scene.rm_render_samples_total:
                sample_text = f"采样 {scene.rm_render_samples or '—'}"
                # 大图分块渲染时显示块进度，避免"采样到头但还有块在渲染"的误解
                if scene.rm_render_tiles_total > 1:
                    cur_block = min(
                        scene.rm_render_tiles_done + 1, scene.rm_render_tiles_total
                    )
                    sample_text += f" · 块 {cur_block}/{scene.rm_render_tiles_total}"
                srow = box.row(align=True)
                srow.progress(factor=min(scene.rm_render_progress, 1.0), text=sample_text)
            else:
                box.row(align=True).label(text="采样 —", icon="TIME")
            stat_row = box.row(align=True)
            stat_row.label(text=f"已用 {scene.rm_render_time or '—'}")
            if scene.rm_render_phase == "finalize":
                # 全部块渲染完成，正在去噪/合成/保存（无采样统计，剩余时间不可用）
                stat_row.label(text="收尾中…", icon="TIME")
            else:
                stat_row.label(text=f"剩余 {scene.rm_render_remaining or '—'}")
        else:
            row = box.row(align=True)
            row.operator("rm.render_selected", text="渲染选中", icon="RESTRICT_RENDER_OFF")
            row.operator("rm.render_all", text="渲染全部", icon="PLAY")
        row = box.row()
        row.operator("rm.diagnose", text="环境诊断", icon="INFO")

        # ---- 状态/信息 ----
        shot = None
        idx = scene.rm_shots_active
        if 0 <= idx < len(scene.rm_shots):
            shot = scene.rm_shots[idx]
        if shot is not None and shot.status == "DONE" and shot.output_path:
            box.label(text="输出：" + shot.output_path, icon="FILE_TICK")
        elif shot is not None and shot.status == "FAILED":
            box.label(text="上次渲染失败", icon="ERROR")
        if shot is not None:
            # 旧版快照（无视图层数据）提醒重新捕获
            try:
                meta = json.loads(shot.data_json)
                if not meta.get("view_layers"):
                    box.label(
                        text="⚠ 旧版快照：无集合勾选数据，请重新捕获",
                        icon="ERROR",
                    )
            except (ValueError, TypeError):
                box.label(text="快照数据损坏", icon="ERROR")
        if scene.rm_last_message:
            box.label(text=scene.rm_last_message, icon="INFO")


UI_CLASSES = (
    RM_UL_shots,
    RM_PT_panel,
)
