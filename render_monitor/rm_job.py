"""Render Monitor - 子进程渲染脚本（后台模式运行）。

主插件通过 `subprocess.Popen` 调用本脚本：
    blender -b <场景副本.blend> -P rm_job.py -- <参数...>

参数（sys.argv 中 "--" 之后）：
    0. scene_name          主场景名（副本中同名）
    1. snapshots_path      快照数据 JSON（列表，每项含 uid/name/data）
    2. outdir              渲染输出目录（绝对路径）
    3. template            输出文件名模板
    4. use_snapshot_frame  "1" / "0"：渲染时是否使用快照记录的帧号
    5. progress_path       进度 JSON 输出路径（主进程轮询）

进度 JSON 结构：
    {"state": "running"|"done", "total": N,
     "shots": [{"uid":..., "name":..., "status": "PENDING|DONE|FAILED",
                "path":..., "error":...}, ...]}

子进程运行在完全独立的 OS 进程中，不触碰主进程场景；
主进程 UI 由 timer 轮询本脚本写出的进度文件来更新。
"""

from __future__ import annotations

import json
import os
import sys
import time

# rm_job.py 以独立脚本方式被 blender -P 执行（无包上下文），
# 需要先把插件包父目录加入 sys.path 才能绝对导入 render_monitor 子模块。
pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if pkg_parent not in sys.path:
    sys.path.insert(0, pkg_parent)
# 子进程可能已加载 Blender 启用的插件（sys.modules 缓存，可能是旧版本），
# 清除后强制导入与 rm_job.py 同目录（同一份安装）的 render_monitor 源码，
# 保证渲染逻辑与插件界面代码版本一致。
for _name in ("render_monitor", "render_monitor.core", "render_monitor.utils"):
    sys.modules.pop(_name, None)

from render_monitor import utils  # noqa: E402


def _write_progress(progress_path, state, total, shots):
    payload = {
        "state": state,
        "total": total,
        "shots": shots,
    }
    # 原子写：先写临时文件再替换，避免主进程读到半截 JSON
    tmp = progress_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, progress_path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# 当前正在渲染的进度状态（render_stats handler 更新，_main 设置）
_stats = {
    "entry": None,       # 正在渲染的 shots 条目（dict）
    "start": None,       # 当前张开始时刻 time.monotonic()
    "last_write": 0.0,   # 上次写进度文件的时间，用于节流
    "progress_path": "",
    "total": 0,
    "shots": [],
}


def _on_render_stats(stats_str):
    """render_stats 回调：把当前张的采样/已用/剩余时间写进进度 JSON。

    该回调在渲染期间（含初始化阶段）被多次调用，字段缺什么更新什么；
    进度文件最多每 0.5 秒写一次，避免高频 IO 拖慢渲染。
    """
    try:
        entry = _stats["entry"]
        if entry is None:
            return
        parsed = utils.parse_render_stats(stats_str)
        if "samples" in parsed:
            # 大图分块渲染时 Sample 是「当前块」的计数，块切换会重置，
            # 直接显示当前值（配合块进度展示整体状态）。
            entry["samples"] = parsed["samples"]
            entry["samples_total"] = parsed["samples_total"]
        if "tiles_done" in parsed:
            entry["tiles_done"] = parsed["tiles_done"]
            entry["tiles_total"] = parsed["tiles_total"]
        # 整体进度：Blender 状态栏进度条的数值 = Cycles 引擎报告的整体渲染
        # 完成度 =（已完成块数 × 每块采样 + 当前块采样）/（总块数 × 每块采样）。
        # 注意：块完成瞬间 stats 的 Sample 是上一块的残留满值（Rendered X/Y
        # 已 +1 而 Sample 仍 = total），此时更新会重复计算导致进度条跳变
        # （如 25% 直接跳 50%），因此只在采样未满（cur < total）时更新，
        # 并用历史最大值防回退。单块渲染退化为采样进度。
        total = entry.get("samples_total") or 0
        cur = entry.get("samples") or 0
        tdone = entry.get("tiles_done") or 0
        ttotal = entry.get("tiles_total") or 1
        if total > 0 and cur < total:
            if ttotal > 1:
                prog = (tdone + cur / total) / ttotal
            else:
                prog = cur / total
            if prog > entry.get("progress", -1.0):
                entry["progress"] = prog
        # 收尾阶段（去噪/合成/保存）检测：此时渲染未结束但不再有采样统计，
        # 采样实际已完成，进度置满。
        if any(
            k in stats_str
            for k in ("Finished", "Denoising", "Finishing", "Reading full buffer")
        ):
            entry["phase"] = "finalize"
            entry["progress"] = 1.0
        has_time = "time" in parsed
        if has_time:  # 帧完成时的精确已用时间
            entry["elapsed"] = parsed["time"]
        # 仅接受采样进行中（samples > 0）的剩余时间估计。实测 Cycles 在
        # 收尾阶段（Finished / 采样计数重置为 0）发出的 Remaining 异常偏大
        # （如实际渲染 3 秒却报剩余 14 秒），必须忽略。
        if "remaining" in parsed and parsed.get("samples", 0) > 0:
            entry["remaining"] = parsed["remaining"]
        now = time.monotonic()
        if _stats["start"] is not None and now - _stats["last_write"] >= 0.5:
            _stats["last_write"] = now
            if not has_time:  # 渲染期间用起止时间差作为实时已用时间
                entry["elapsed"] = now - _stats["start"]
            _write_progress(
                _stats["progress_path"], "running", _stats["total"], _stats["shots"]
            )
    except Exception as exc:  # noqa: BLE001
        # handler 绝不能拖垮渲染；异常写入 stderr（勾选「输出渲染日志」时可见）
        try:
            sys.stderr.write(f"[rm_job] render_stats handler 异常: {exc}\n")
        except Exception:  # noqa: BLE001
            pass


def _main():
    try:
        marker = sys.argv.index("--")
    except ValueError:
        sys.stderr.write("rm_job: 缺少 '--' 参数分隔符\n")
        return 1
    args = sys.argv[marker + 1:]
    if len(args) < 6:
        sys.stderr.write(f"rm_job: 参数不足（需要 6 个，实际 {len(args)}）: {args}\n")
        return 1

    scene_name, snapshots_path, outdir, template, use_snapshot_frame, progress_path = args[:6]
    use_snapshot_frame = use_snapshot_frame == "1"

    # 让子进程能 import 插件包（rm_job.py 位于 addons/render_monitor/ 下）
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if pkg_parent not in sys.path:
        sys.path.insert(0, pkg_parent)

    import bpy
    from render_monitor import core, utils  # noqa: F401

    # 渲染期间实时回传采样/已用/剩余时间（render_stats 在后台渲染同样触发）
    bpy.app.handlers.render_stats.append(_on_render_stats)

    scene = bpy.data.scenes.get(scene_name) or bpy.context.scene
    with open(snapshots_path, encoding="utf-8") as f:
        snapshots = json.load(f)

    shots = []
    for i, shot in enumerate(snapshots):
        entry = {
            "uid": shot["uid"],
            "name": shot["name"],
            "status": "PENDING",
            "path": "",
            "error": "",
        }
        shots.append(entry)
        try:
            data = shot["data"]
            # 列表顺序编号（从 1 开始），用于文件命名 {index} 占位符
            shot_index = int(shot.get("index", i + 1))
            # 诊断：确认快照版本与视图层数据（写入渲染日志，勾选「输出渲染日志」时可见）
            print(f"[rm_job] 应用快照「{shot['name']}」version={data.get('version')}")
            if "view_layers" not in data:
                print(
                    "[rm_job] 警告: 该快照没有 view_layers 数据（旧版快照），"
                    "集合勾选状态不会被应用——请重新捕获快照"
                )
            frame_before = scene.frame_current
            core.apply_scene_state(scene, data)
            if not use_snapshot_frame:
                scene.frame_current = frame_before
            # 诊断 + 验证：快照声明了视图层数据时，确认 exclude 真的生效
            if "view_layers" in data:
                problems = []
                for vl_state in data["view_layers"]:
                    vl = None
                    try:
                        vl = scene.view_layers.get(vl_state["name"])
                    except Exception:  # noqa: BLE001
                        pass
                    if vl is None:
                        problems.append(f"视图层 {vl_state['name']} 不存在")
                        continue
                    for lc_state in vl_state.get("collections", []):
                        found = False
                        for lc in core.iter_layer_collections(vl):
                            if lc.collection.name != lc_state["name"]:
                                continue
                            found = True
                            if lc.exclude != lc_state["exclude"]:
                                problems.append(
                                    f"{vl_state['name']}/{lc_state['name']}: "
                                    f"exclude={lc.exclude} 期望={lc_state['exclude']}"
                                )
                            break
                        if not found:
                            problems.append(
                                f"{vl_state['name']}/{lc_state['name']} 未找到"
                            )
                if problems:
                    print("[rm_job] 视图层开关验证失败: " + " | ".join(problems))
                else:
                    print("[rm_job] 视图层开关验证通过")
            ext = (scene.render.file_extension or ".png").lstrip(".")
            filename = utils.format_filename(
                template, shot["name"], scene.frame_current, shot_index
            )
            path = utils.build_output_path(outdir, filename, ext)
            os.makedirs(outdir, exist_ok=True)
            # 清理同路径旧文件，避免误判成功
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            scene.render.filepath = path
            # 标记渲染中并立即上报，主进程才能显示"正在渲染：名称"
            entry["status"] = "RENDERING"
            # 初始化实时统计字段（render_stats handler 会持续更新）
            entry.update(
                samples=0, samples_total=0, elapsed=0.0, remaining=None,
                tiles_done=0, tiles_total=1, progress=0.0, phase="",
            )
            _stats.update(
                entry=entry,
                start=time.monotonic(),
                last_write=0.0,
                progress_path=progress_path,
                total=len(snapshots),
                shots=shots,
            )
            _write_progress(progress_path, "running", len(snapshots), shots)
            bpy.ops.render.render(scene=scene.name, write_still=True, use_viewport=False)
            if not (os.path.exists(path) and os.path.getsize(path) > 0):
                raise RuntimeError(
                    f"渲染未生成输出文件（可能被取消或写盘失败）: {path}"
                )
            entry["status"] = "DONE"
            entry["path"] = path
        except Exception as exc:  # noqa: BLE001
            entry["status"] = "FAILED"
            entry["error"] = str(exc)
        _write_progress(progress_path, "running", len(snapshots), shots)

    _write_progress(progress_path, "done", len(snapshots), shots)
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = _main()
    except BaseException:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        # 尽量把致命错误写入进度文件，便于主进程提示
        try:
            marker = sys.argv.index("--")
            args = sys.argv[marker + 1:]
            if len(args) >= 6:
                _write_progress(
                    args[5], "error", 0,
                    [{"uid": "", "name": "致命错误", "status": "FAILED",
                      "path": "", "error": traceback.format_exc()}],
                )
        except (ValueError, OSError):
            pass
        code = 1
    sys.exit(code)
