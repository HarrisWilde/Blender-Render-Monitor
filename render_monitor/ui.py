"""Render Monitor - 3D 视图侧边栏 (N 面板) 界面。"""

from __future__ import annotations

import json
import textwrap

import bpy
from bpy.types import Panel, UIList

_STATUS_ICONS = {
    "PENDING": "TIME",
    "RENDERING": "RENDER_STILL",
    "DONE": "CHECKMARK",
    "FAILED": "ERROR",
}


def _draw_wrapped(layout, text, icon="NONE", width=60, max_lines=6):
    """按宽度把长文本折行绘制，避免错误信息在面板里被截断。"""
    lines = []
    for raw_line in str(text).splitlines():
        lines.extend(textwrap.wrap(raw_line, width=width) or [""])
    for line in lines[:max_lines]:
        layout.label(text=line, icon=icon)
    if len(lines) > max_lines:
        layout.label(text=f"…（共 {len(lines)} 行，请查看渲染日志）", icon=icon)


class RM_UL_shots(UIList):
    """快照列表：名称 + 状态 + 输出文件。"""

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {"DEFAULT", "COMPACT"}:
            row = layout.row(align=True)
            # 序号 = 快照在列表中的原始顺序（与文件命名模板 {index} 对应），
            # 不是过滤后的重排号；只作显示，不修改快照名。
            # 注意：UIList 的 data 参数是 template_list 传入的 dataptr（这里是
            # scene），快照集合要取 data.rm_shots。draw_item 每帧渲染，必须
            # 健壮：异常时兜底显示 1，绝不拖垮列表显示。
            index = 1
            try:
                for i, s in enumerate(data.rm_shots):
                    if s.uid == item.uid:
                        index = i + 1
                        break
            except (AttributeError, TypeError):
                pass
            # 序号列：宽度随当前最大序号位数自适应（1 位≈0.7u、2 位≈1.0u、
            # 3 位≈1.3u……），保证 100+ 的 3 位数也不被截断成省略号，
            # 同时位数少时列不浪费；所有行宽度一致，横向严格对齐。
            try:
                max_index = len(data.rm_shots)
            except (AttributeError, TypeError):
                max_index = 0
            digits = len(str(max_index))
            num = row.column(align=True)
            num.ui_units_x = 0.4 + 0.3 * digits
            num.alignment = "RIGHT"
            num.label(text=str(index))
            # 渲染勾选：图标按钮（CHECKBOX_HLT=勾选 / CHECKBOX_DEHLT=未勾选），
            # 点击切换。不用 UIList 行内布尔 prop（其小方框在部分主题下不明显）。
            row.operator(
                "rm.toggle_shot", text="",
                icon="CHECKBOX_HLT" if item.selected else "CHECKBOX_DEHLT",
                emboss=False,
            ).uid = item.uid
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
        # 渲染勾选的批量操作：全选 / 全不选 / 反选
        row = layout.row(align=True)
        row.operator("rm.select_all", text="全选", icon="CHECKBOX_HLT").action = "ALL"
        row.operator("rm.select_all", text="全不选", icon="CHECKBOX_DEHLT").action = "NONE"
        row.operator("rm.select_all", text="反选", icon="ARROW_LEFTRIGHT").action = "INVERT"
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
            row.operator("rm.render_selected", text="渲染当前", icon="RESTRICT_RENDER_OFF")
            # 动态显示勾选数量，所见即所得：渲染勾选（勾选数/总数）
            checked = sum(1 for s in scene.rm_shots if s.selected)
            total = len(scene.rm_shots)
            row.operator("rm.render_all", text=f"渲染勾选（{checked}/{total}）", icon="PLAY")
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
            box.label(text="渲染失败", icon="ERROR")
            error = getattr(shot, "error", "")
            if error:
                _draw_wrapped(box, error, icon="ERROR")
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
