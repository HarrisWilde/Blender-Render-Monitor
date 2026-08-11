"""Render Monitor - 操作符：快照管理 + 批量渲染（子进程后台渲染）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid

import bpy

from . import core, utils


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _active_shot(scene):
    idx = scene.rm_shots_active
    if 0 <= idx < len(scene.rm_shots):
        return scene.rm_shots[idx]
    return None


def _shot_by_uid(scene, uid):
    for shot in scene.rm_shots:
        if shot.uid == uid:
            return shot
    return None


def _tag_redraw():
    """触发 3D 视口重绘，让面板进度随 timer 更新。"""
    for wm in bpy.data.window_managers:
        for win in wm.windows:
            for area in win.screen.areas:
                if area.type == "VIEW_3D":
                    area.tag_redraw()


def _plugin_version_str():
    """返回插件版本字符串（如 'v1.4.0'）。

    Blender 4.2+ 扩展模式下，模块内的 bl_info 被扩展系统忽略（版本由
    blender_manifest.toml 生成），因此优先读 manifest；普通 addon 模式
    再用 bl_info 兜底。
    """
    import os

    manifest = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "blender_manifest.toml"
    )
    try:
        import tomllib
    except ImportError:  # Python < 3.11（Blender 4.2+ 自带 3.11+，正常不会走到）
        tomllib = None
    if tomllib is not None:
        try:
            with open(manifest, "rb") as f:
                return "v" + tomllib.load(f)["version"]
        except (OSError, KeyError, TypeError, ValueError):
            pass
    try:
        from . import bl_info

        return "v" + ".".join(map(str, bl_info["version"]))
    except Exception:  # noqa: BLE001
        return "?"


# ---------------------------------------------------------------------------
# 子进程后台渲染
#
# 架构：bpy.ops.render.render() 从 Python 调用会阻塞主线程（UI 冻结）。
# 因此把渲染放到独立的 blender -b 子进程执行，主进程用 bpy.app.timers
# 轮询子进程写出的进度 JSON 来更新 UI。主场景从头到尾零修改。
# ---------------------------------------------------------------------------

# 当前渲染会话的模块级状态（同一时间只允许一个渲染会话）
_active = {
    "process": None,          # subprocess.Popen
    "scene_name": "",
    "tmpdir": "",
    "progress_path": "",
    "log_path": "",
    "total": 0,
}


def _read_progress():
    try:
        with open(_active["progress_path"], encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _update_ui_from_progress(payload):
    """把子进程进度 JSON 反映到快照条目与场景进度属性。"""
    scene = bpy.data.scenes.get(_active["scene_name"])
    if scene is None:
        return
    done = failed = 0
    current = ""
    samples = time_str = remaining_str = ""
    samples_cur = samples_total = 0
    tiles_done = tiles_total = 0
    progress = 0.0
    phase = ""
    for s in payload.get("shots", []):
        shot = _shot_by_uid(scene, s["uid"])
        if shot is None:
            continue
        shot.status = s["status"]
        shot.output_path = s.get("path", "")
        if s["status"] == "DONE":
            done += 1
        elif s["status"] == "FAILED":
            failed += 1
        elif s["status"] == "RENDERING":
            current = s["name"]
            # 当前张的实时统计（采样 / 块进度 / 已用 / 剩余），仅用于面板显示
            samples = utils.format_samples(s.get("samples"), s.get("samples_total"))
            time_str = utils.format_duration(s.get("elapsed"))
            remaining_str = utils.format_duration(s.get("remaining"))
            samples_cur = int(s.get("samples") or 0)
            samples_total = int(s.get("samples_total") or 0)
            tiles_done = int(s.get("tiles_done") or 0)
            tiles_total = int(s.get("tiles_total") or 1)
            progress = float(s.get("progress") or 0.0)
            phase = s.get("phase") or ""
    scene.rm_render_done = done
    scene.rm_render_failed = failed
    scene.rm_render_total = payload.get("total", _active["total"])
    if current:
        scene.rm_render_current = current
    scene.rm_render_samples = samples
    scene.rm_render_samples_cur = samples_cur
    scene.rm_render_samples_total = samples_total
    scene.rm_render_tiles_done = tiles_done
    scene.rm_render_tiles_total = tiles_total
    scene.rm_render_progress = progress
    scene.rm_render_phase = phase
    scene.rm_render_time = time_str
    scene.rm_render_remaining = remaining_str
    _tag_redraw()


def _poll_render_timer():
    """timer 回调：轮询子进程。返回继续间隔（float）或 None（结束并注销）。

    Blender 在回调抛异常时会注销该 timer，因此必须整体兜底，
    确保进程结束时无论发生什么都完成清理，避免 UI 永久卡死。
    """
    proc = _active["process"]
    if proc is None:
        return None
    try:
        payload = _read_progress()
        if payload:
            _update_ui_from_progress(payload)
        if proc.poll() is None:
            return 0.5  # 仍在渲染，0.5 秒后再查
        _finish_session(proc.poll(), cancelled=False)
        return None
    except BaseException:  # noqa: BLE001
        try:
            _finish_session(-2, cancelled=False)
        except BaseException:  # noqa: BLE001
            pass
        return None


def _finish_session(returncode, cancelled=False):
    """子进程结束后收尾：最终状态、报告消息、清理临时文件、解除 busy。

    cancelled=True 时不生成“完成/异常退出”消息（由调用方自行报告，
    如用户主动停止）。清理段独立 try，确保必然执行。
    """
    scene = bpy.data.scenes.get(_active["scene_name"])
    payload = _read_progress()

    # 子进程异常退出（非 0）且没有进度文件时，把未完成项标记为失败
    if returncode != 0 and scene is not None and payload is None:
        for shot in scene.rm_shots:
            if shot.status in ("PENDING", "RENDERING"):
                shot.status = "FAILED"

    if payload:
        done = sum(1 for s in payload["shots"] if s["status"] == "DONE")
        failed = sum(1 for s in payload["shots"] if s["status"] == "FAILED")
        if scene is not None:
            scene.rm_render_done = done
            scene.rm_render_failed = failed
    else:
        done = failed = 0

    if not cancelled:
        log_hint = ""
        if _active["log_path"]:  # 仅勾选了「输出渲染日志」时提示路径
            if returncode != 0:
                log_hint = (
                    f"（渲染进程异常退出，代码 {returncode}，日志：{_active['log_path']}）"
                )
            else:
                log_hint = f"（日志：{_active['log_path']}）"
        elif returncode != 0:
            log_hint = f"（渲染进程异常退出，代码 {returncode}）"
        msg = f"渲染完成：成功 {done}，失败 {failed} {log_hint}".strip()
        if scene is not None:
            scene.rm_last_message = msg
            scene.rm_render_current = ""
            scene.rm_render_samples = ""
            scene.rm_render_samples_cur = 0
            scene.rm_render_samples_total = 0
            scene.rm_render_tiles_done = 0
            scene.rm_render_tiles_total = 1
            scene.rm_render_progress = 0.0
            scene.rm_render_phase = ""
            scene.rm_render_time = ""
            scene.rm_render_remaining = ""
        print(f"[Render Monitor] {msg}")

    # 清理（独立 try，确保必执行）
    try:
        if _active["process"] is not None:
            try:
                _active["process"].wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    _active["process"].kill()
                except Exception:  # noqa: BLE001
                    pass
            _active["process"] = None
        shutil.rmtree(_active["tmpdir"], ignore_errors=True)
        _active.update(tmpdir="", progress_path="", log_path="", total=0,
                       scene_name="")
        bpy.context.window_manager.rm_busy = False
        _tag_redraw()
    except BaseException:  # noqa: BLE001
        pass


def _stop_active():
    """停止当前渲染（用户点击「停止」）。被中断的当前张置回 PENDING 以便重试。"""
    proc = _active["process"]
    if proc is None:
        return
    if proc.poll() is None:  # 仅在仍在运行时终止，避免对已退出进程操作报错
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=8)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
    scene = bpy.data.scenes.get(_active["scene_name"])
    payload = _read_progress()
    if payload and scene is not None:
        for s in payload.get("shots", []):
            if s["status"] == "RENDERING":
                shot = _shot_by_uid(scene, s["uid"])
                if shot is not None:
                    shot.status = "PENDING"
    _finish_session(-1, cancelled=True)
    if scene is not None:
        scene.rm_last_message = "已停止渲染（未完成的快照保持待渲染状态）"


def _start_subprocess_render(context, uids):
    """启动一次子进程渲染会话。返回 (ok, message)。"""
    scene = context.scene
    wm = context.window_manager
    if wm.rm_busy:
        return False, "已有渲染任务在进行中"
    if not uids:
        return False, "没有勾选的快照（请先在列表中勾选要渲染的，或点「全选」）"
    if not bpy.app.binary_path:
        return False, "无法定位 Blender 可执行文件（bpy.app.binary_path 为空，可能是以 Python 模块方式运行）"
    # 未保存文件：// 相对路径无法解析，必须使用绝对路径输出目录
    if not bpy.data.filepath:
        if not scene.rm_output_dir or scene.rm_output_dir.startswith("//"):
            return False, "文件未保存：请在「输出目录」中选择一个绝对路径后再渲染"

    # 1. 导出快照数据（JSON）
    tmpdir = tempfile.mkdtemp(prefix="rm_render_")
    snapshots = []
    skipped = 0
    old_snapshots = 0
    for i, s in enumerate(scene.rm_shots):
        if s.uid in uids:
            # {index} = 快照在列表中的原始顺序（从 1 开始），而不是本次渲染队列
            # 的重新编号：否则「仅渲染未完成」中断后续跑或单张渲染时，编号会从
            # 1 重新计数，导致文件名错乱/覆盖已渲染文件。
            try:
                data = json.loads(s.data_json)
            except ValueError:
                skipped += 1  # 坏快照跳过，不阻断整批
                continue
            if "view_layers" not in data or data.get("version", 1) < 2:
                old_snapshots += 1  # 旧版快照：无集合勾选/视图层数据
            snapshots.append({
                "uid": s.uid,
                "name": s.name,
                "index": i + 1,
                "data": data,
            })
    if not snapshots:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return False, "没有可渲染的快照（数据可能已损坏）"
    snap_path = os.path.join(tmpdir, "snapshots.json")
    try:
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(snapshots, f, ensure_ascii=False)
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return False, f"写入快照数据失败: {exc}"

    # 2. 保存当前状态副本（含未保存修改），copy=True 不改变当前文件
    blend_copy = os.path.join(tmpdir, "scene_copy.blend")
    try:
        result = bpy.ops.wm.save_as_mainfile(filepath=blend_copy, copy=True)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmpdir, ignore_errors=True)
        return False, f"保存场景副本失败: {exc}"
    # 保存失败时 operator 返回 CANCELLED（不抛异常），必须显式检查
    if result != {"FINISHED"} or not os.path.exists(blend_copy):
        shutil.rmtree(tmpdir, ignore_errors=True)
        return False, f"保存场景副本失败: {result}"

    # 3. 启动子进程（blender -b，后台无界面渲染）
    rm_job = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rm_job.py")
    progress = os.path.join(tmpdir, "progress.json")
    outdir = bpy.path.abspath(scene.rm_output_dir)
    try:
        os.makedirs(outdir, exist_ok=True)
    except OSError as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return False, f"无法创建输出目录: {exc}"
    # 日志开关：默认关闭；排查问题时勾选「输出渲染日志」
    log_path = ""
    log_target = subprocess.DEVNULL
    if scene.rm_write_log:
        log_path = os.path.join(outdir, f"rm_render_log_{int(time.time())}.txt")
        try:
            log_target = open(log_path, "w", encoding="utf-8")
        except OSError as exc:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return False, f"无法创建渲染日志文件: {exc}"
    cmd = [
        bpy.app.binary_path,
        "-b", blend_copy,
        "-P", rm_job,
        "--",
        scene.name, snap_path, outdir,
        scene.rm_file_template or utils.DEFAULT_FILE_TEMPLATE,
        "1" if scene.rm_use_snapshot_frame else "0",
        progress,
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=log_target, stderr=log_target)
    except OSError as exc:
        if log_target is not subprocess.DEVNULL:
            log_target.close()
        shutil.rmtree(tmpdir, ignore_errors=True)
        return False, f"启动渲染进程失败: {exc}"
    if log_target is not subprocess.DEVNULL:
        log_target.close()  # 子进程持有句柄，父进程可立即关闭

    _active.update(
        process=proc,
        scene_name=scene.name,
        tmpdir=tmpdir,
        progress_path=progress,
        log_path=log_path,
        total=len(snapshots),
    )
    scene.rm_render_done = 0
    scene.rm_render_failed = 0
    scene.rm_render_total = len(snapshots)
    scene.rm_render_current = "正在启动渲染进程…"
    warn_parts = []
    if old_snapshots:
        warn_parts.append(
            f"{old_snapshots} 个旧版快照（无集合勾选数据），请重新捕获后再渲染"
        )
    if skipped:
        warn_parts.append(f"跳过 {skipped} 个损坏快照")
    scene.rm_last_message = f"已启动后台渲染（{len(snapshots)} 张）" + (
        "；" + "；".join(warn_parts) if warn_parts else ""
    )
    wm.rm_busy = True
    if not bpy.app.timers.is_registered(_poll_render_timer):
        # persistent=True：渲染中加载新文件也不会注销 timer，确保收尾清理
        bpy.app.timers.register(
            _poll_render_timer, first_interval=0.5, persistent=True
        )
    return True, f"已启动后台渲染（{len(snapshots)} 张），可在面板查看进度"


# ---------------------------------------------------------------------------
# 快照管理
# ---------------------------------------------------------------------------

class RM_OT_capture(bpy.types.Operator):
    bl_idname = "rm.capture"
    bl_label = "捕获当前状态为新快照"
    bl_description = "把当前场景状态（物体/集合/环境/相机/渲染设置/帧）保存为新快照"
    bl_options = {"REGISTER"}

    shot_name: bpy.props.StringProperty(name="快照名称", default="")

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def _default_name(self, context):
        return "快照"

    def invoke(self, context, event):
        if not self.shot_name:
            self.shot_name = self._default_name(context)
        return context.window_manager.invoke_props_dialog(self)

    def draw(self, context):
        self.layout.prop(self, "shot_name")

    def execute(self, context):
        scene = context.scene
        state = core.capture_scene_state(scene)
        shot = scene.rm_shots.add()
        shot.uid = uuid.uuid4().hex
        shot.name = self.shot_name.strip() or self._default_name(context)
        shot.data_json = json.dumps(state, ensure_ascii=False)
        shot.status = "PENDING"
        scene.rm_shots_active = len(scene.rm_shots) - 1
        self.report({"INFO"}, f"已捕获快照「{shot.name}」")
        return {"FINISHED"}


class RM_OT_apply(bpy.types.Operator):
    bl_idname = "rm.apply"
    bl_label = "应用选中快照"
    bl_description = "把选中快照的场景状态恢复到当前场景"

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def execute(self, context):
        scene = context.scene
        shot = _active_shot(scene)
        if shot is None:
            self.report({"WARNING"}, "没有选中的快照")
            return {"CANCELLED"}
        try:
            core.apply_scene_state(scene, json.loads(shot.data_json))
            self.report({"INFO"}, f"已应用快照「{shot.name}」")
        except Exception as exc:  # noqa: BLE001
            self.report({"ERROR"}, f"应用快照失败: {exc}")
            return {"CANCELLED"}
        return {"FINISHED"}


class RM_OT_update(bpy.types.Operator):
    bl_idname = "rm.update"
    bl_label = "用当前状态更新快照"
    bl_description = "把当前场景状态重新捕获并覆盖到选中快照"

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def execute(self, context):
        scene = context.scene
        shot = _active_shot(scene)
        if shot is None:
            self.report({"WARNING"}, "没有选中的快照")
            return {"CANCELLED"}
        shot.data_json = json.dumps(core.capture_scene_state(scene), ensure_ascii=False)
        shot.status = "PENDING"
        shot.output_path = ""
        self.report({"INFO"}, f"已更新快照「{shot.name}」")
        return {"FINISHED"}


class RM_OT_delete(bpy.types.Operator):
    bl_idname = "rm.delete"
    bl_label = "删除选中快照"
    bl_description = "删除选中的快照"

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def execute(self, context):
        scene = context.scene
        idx = scene.rm_shots_active
        if not (0 <= idx < len(scene.rm_shots)):
            return {"CANCELLED"}
        scene.rm_shots.remove(idx)
        scene.rm_shots_active = min(max(idx - 1, 0), max(len(scene.rm_shots) - 1, 0))
        return {"FINISHED"}


def _move_shot(scene, idx, direction):
    n = len(scene.rm_shots)
    new_idx = idx + direction
    if not (0 <= idx < n and 0 <= new_idx < n):
        return False
    shots = list(scene.rm_shots)
    shots[idx], shots[new_idx] = shots[new_idx], shots[idx]
    # CollectionProperty 不支持直接交换，用重建方式
    scene.rm_shots.clear()
    for s in shots:
        new = scene.rm_shots.add()
        new.uid, new.name, new.data_json = s.uid, s.name, s.data_json
        new.status, new.output_path = s.status, s.output_path
        new.selected = s.selected
    scene.rm_shots_active = new_idx
    return True


class RM_OT_move_up(bpy.types.Operator):
    bl_idname = "rm.move_up"
    bl_label = "上移"
    bl_description = "把选中快照上移一位（渲染顺序提前）"

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def execute(self, context):
        if not _move_shot(context.scene, context.scene.rm_shots_active, -1):
            self.report({"WARNING"}, "已在列表最上方")
        return {"FINISHED"}


class RM_OT_move_down(bpy.types.Operator):
    bl_idname = "rm.move_down"
    bl_label = "下移"
    bl_description = "把选中快照下移一位（渲染顺序延后）"

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def execute(self, context):
        if not _move_shot(context.scene, context.scene.rm_shots_active, 1):
            self.report({"WARNING"}, "已在列表最下方")
        return {"FINISHED"}


class RM_OT_toggle_shot(bpy.types.Operator):
    bl_idname = "rm.toggle_shot"
    bl_label = "切换渲染勾选"
    bl_description = "勾选/取消勾选该快照（「渲染勾选」只渲染勾选的快照）"

    uid: bpy.props.StringProperty(name="UID", default="")

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def execute(self, context):
        shot = _shot_by_uid(context.scene, self.uid)
        if shot is not None:
            shot.selected = not shot.selected
        return {"FINISHED"}


class RM_OT_select_all(bpy.types.Operator):
    bl_idname = "rm.select_all"
    bl_label = "批量设置渲染勾选"
    bl_description = "全选 / 全不选 / 反选快照的渲染勾选状态（「渲染勾选」只渲染勾选的快照）"

    action: bpy.props.EnumProperty(
        name="操作",
        items=[
            ("ALL", "全选", "勾选全部快照"),
            ("NONE", "全不选", "取消全部快照的勾选"),
            ("INVERT", "反选", "反转每个快照的勾选状态"),
        ],
        default="ALL",
    )

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def execute(self, context):
        for shot in context.scene.rm_shots:
            if self.action == "ALL":
                shot.selected = True
            elif self.action == "NONE":
                shot.selected = False
            else:
                shot.selected = not shot.selected
        return {"FINISHED"}


class RM_OT_clear_done(bpy.types.Operator):
    bl_idname = "rm.clear_done"
    bl_label = "清空已完成"
    bl_description = "删除所有状态为「已完成」的快照"

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def execute(self, context):
        scene = context.scene
        for i in range(len(scene.rm_shots) - 1, -1, -1):
            if scene.rm_shots[i].status == "DONE":
                scene.rm_shots.remove(i)
        scene.rm_shots_active = 0
        return {"FINISHED"}


class RM_OT_clear_all(bpy.types.Operator):
    bl_idname = "rm.clear_all"
    bl_label = "清空全部快照"
    bl_description = "删除当前场景的全部快照"

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def invoke(self, context, event):
        scene = context.scene
        if not scene.rm_shots:
            self.report({"WARNING"}, "当前场景没有快照")
            return {"CANCELLED"}
        # invoke_confirm：弹出确认框，点击「确定」后才执行 execute
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        scene = context.scene
        self.layout.label(
            text=f"将删除场景「{scene.name}」的全部 {len(scene.rm_shots)} 个快照",
            icon="ERROR",
        )
        self.layout.label(text="此操作不可撤销，渲染状态也会一并清除。")

    def execute(self, context):
        scene = context.scene
        total = len(scene.rm_shots)
        scene.rm_shots.clear()
        scene.rm_shots_active = 0
        scene.rm_last_message = f"已清空场景「{scene.name}」的全部 {total} 个快照"
        self.report({"INFO"}, f"已清空 {total} 个快照")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

class RM_OT_render_selected(bpy.types.Operator):
    bl_idname = "rm.render_selected"
    bl_label = "渲染当前快照"
    bl_description = "后台渲染列表中高亮（当前选中）的那一个快照到输出目录"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def invoke(self, context, event):
        scene = context.scene
        if not bpy.data.filepath and (
            not scene.rm_output_dir or scene.rm_output_dir.startswith("//")
        ):
            # 未保存文件：// 相对路径无法解析，先请用户选择输出目录
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def draw(self, context):
        self.layout.label(
            text="文件未保存，请选择输出目录（绝对路径）：", icon="INFO"
        )
        self.layout.prop(context.scene, "rm_output_dir")

    def execute(self, context):
        shot = _active_shot(context.scene)
        if shot is None:
            self.report({"WARNING"}, "没有选中的快照")
            return {"CANCELLED"}
        ok, msg = _start_subprocess_render(context, [shot.uid])
        if not ok:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class RM_OT_render_all(bpy.types.Operator):
    bl_idname = "rm.render_all"
    bl_label = "渲染所有勾选快照"
    bl_description = "按列表顺序后台渲染所有已勾选的快照（未勾选的不渲染；已完成/失败/待渲染勾选后都会渲染）"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return not context.window_manager.rm_busy

    def invoke(self, context, event):
        scene = context.scene
        if not bpy.data.filepath and (
            not scene.rm_output_dir or scene.rm_output_dir.startswith("//")
        ):
            # 未保存文件：// 相对路径无法解析，先请用户选择输出目录
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def draw(self, context):
        self.layout.label(
            text="文件未保存，请选择输出目录（绝对路径）：", icon="INFO"
        )
        self.layout.prop(context.scene, "rm_output_dir")

    def execute(self, context):
        scene = context.scene
        uids = [s.uid for s in scene.rm_shots if s.selected]
        ok, msg = _start_subprocess_render(context, uids)
        if not ok:
            self.report({"ERROR"}, msg)
            return {"CANCELLED"}
        self.report({"INFO"}, msg)
        return {"FINISHED"}


class RM_OT_stop_render(bpy.types.Operator):
    bl_idname = "rm.stop_render"
    bl_label = "停止渲染"
    bl_description = "停止当前后台渲染（被中断的当前张保持待渲染状态，可稍后继续）"

    @classmethod
    def poll(cls, context):
        return context.window_manager.rm_busy

    def execute(self, context):
        _stop_active()
        self.report({"WARNING"}, "已停止渲染")
        return {"FINISHED"}


class RM_OT_diagnose(bpy.types.Operator):
    """环境诊断：弹窗显示快照与当前场景的视图层/集合开关状态。

    用于确认快照是否记录了视图层数据（version、view_layers），
    以及当前场景每个视图层每个集合的 exclude 实际值。
    """

    bl_idname = "rm.diagnose"
    bl_label = "环境诊断"
    bl_description = "显示快照与当前场景的视图层/集合开关状态，辅助检查集合排除设置"
    bl_options = {"REGISTER"}

    report_text: bpy.props.StringProperty(name="诊断结果", default="")

    def _active_shot(self, scene):
        return _active_shot(scene)

    def invoke(self, context, event):
        lines = []
        lines.append(f"插件版本: {_plugin_version_str()}")
        scene = context.scene
        lines.append(f"输出目录: {bpy.path.abspath(scene.rm_output_dir)}")
        lines.append("")

        # 当前场景视图层状态
        lines.append(f"场景「{scene.name}」 视图层数: {len(scene.view_layers)}")
        active_vl = getattr(scene.view_layers, "active", None)
        for vl in scene.view_layers:
            tag = " (active)" if vl == active_vl else ""
            lines.append(f"  [视图层] {vl.name}{tag}")
            try:
                for lc in core.iter_layer_collections(vl):
                    lines.append(
                        f"    集合 {lc.collection.name}: "
                        f"exclude={lc.exclude} hide_vp={lc.hide_viewport}"
                    )
            except Exception as exc:  # noqa: BLE001
                lines.append(f"    遍历失败: {exc}")
        lines.append("")

        # 选中快照
        shot = _active_shot(scene)
        if shot is None:
            lines.append("未选中快照")
        else:
            lines.append(f"快照「{shot.name}」 状态: {shot.status}")
            try:
                meta = json.loads(shot.data_json)
                lines.append(
                    f"  version={meta.get('version')} "
                    f"物体={len(meta.get('objects', []))} "
                    f"集合={len(meta.get('collections', []))}"
                )
                vls = meta.get("view_layers", [])
                lines.append(f"  视图层记录数: {len(vls)}")
                for vl_state in vls:
                    lines.append(f"    [视图层] {vl_state['name']}")
                    for cs in vl_state.get("collections", []):
                        lines.append(
                            f"      集合 {cs['name']}: "
                            f"exclude={cs['exclude']} hide_vp={cs['hide_viewport']}"
                        )
                if not vls:
                    lines.append("  ⚠ 旧版快照：无视图层数据！请删除后重新捕获")
            except ValueError:
                lines.append("  快照数据损坏")
        self.report_text = "\n".join(lines)
        return context.window_manager.invoke_props_dialog(self, width=560)

    def draw(self, context):
        col = self.layout.column(align=True)
        for line in self.report_text.split("\n"):
            if line.strip():
                col.label(text=line)

    def execute(self, context):
        return {"FINISHED"}


OPERATOR_CLASSES = (
    RM_OT_capture,
    RM_OT_apply,
    RM_OT_update,
    RM_OT_delete,
    RM_OT_move_up,
    RM_OT_move_down,
    RM_OT_toggle_shot,
    RM_OT_select_all,
    RM_OT_clear_done,
    RM_OT_clear_all,
    RM_OT_render_selected,
    RM_OT_render_all,
    RM_OT_stop_render,
    RM_OT_diagnose,
)
