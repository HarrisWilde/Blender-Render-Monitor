"""Render Monitor - 纯逻辑工具（不依赖 bpy，可独立单元测试）。"""

from __future__ import annotations

import os
import re

# 文件名模板默认值：{name} = 快照名，{index} = 快照在列表中的顺序（从 1 开始），
# {frame} = 帧号
DEFAULT_FILE_TEMPLATE = "{name} {index}"

# 输出文件非法字符（Windows / Linux / macOS 通用）
_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def sanitize_filename(name: str) -> str:
    """把任意字符串清洗成可安全用作文件名的形式。"""
    cleaned = _INVALID_CHARS.sub("_", name).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)
    if cleaned in ("", ".", ".."):
        cleaned = "shot"
    # 限制长度，避免文件系统问题
    return cleaned[:120]


def format_filename(template: str, shot_name: str, frame: int, index: int = 1) -> str:
    """按模板生成输出文件名（不含扩展名）。

    模板支持 {name}（快照名）、{index}（列表顺序，从 1 开始）、{frame}（帧号）
    占位符。模板缺少任何动态占位符（会导致所有快照输出同名互相覆盖）或格式
    错误时，自动回退默认模板，保证返回安全文件名。
    """
    tpl = (template or DEFAULT_FILE_TEMPLATE).strip()
    if "{name}" not in tpl and "{frame}" not in tpl and "{index}" not in tpl:
        tpl = DEFAULT_FILE_TEMPLATE
    try:
        raw = tpl.format(
            name=sanitize_filename(shot_name),
            frame=int(frame),
            index=int(index),
        )
    except (KeyError, IndexError, ValueError, AttributeError):
        raw = DEFAULT_FILE_TEMPLATE.format(
            name=sanitize_filename(shot_name),
            frame=int(frame),
            index=int(index),
        )
    return sanitize_filename(raw)


def build_output_path(outdir_abs: str, filename: str, ext: str) -> str:
    """拼接输出目录 + 文件名 + 扩展名。扩展名去掉前导点并小写化。"""
    ext = (ext or "png").lstrip(".").lower()
    return os.path.join(outdir_abs, f"{filename}.{ext}")


# ---------------------------------------------------------------------------
# 渲染进度统计（来自 bpy.app.handlers.render_stats 的字符串）
# ---------------------------------------------------------------------------

# Cycles 采样阶段："Remaining: 00:01.33 | Mem: 6M | Sample 96/256"
_SAMPLE_RE = re.compile(r"Sample (\d+)/(\d+)")
_REMAINING_RE = re.compile(r"Remaining:\s*([\d:.]+)")
# 大图分块渲染："Rendered 1/4 Tiles, Sample 80/128"（块切换后采样计数重置）
_TILES_RE = re.compile(r"Rendered (\d+)/(\d+) Tiles")
# 帧完成："Time: 00:01.00 (Saving: 00:00.11)"
_TIME_RE = re.compile(r"Time:\s*([\d:.]+)")


def _parse_timer(value: str):
    """把 "00:01.33" / "01:02:03.45" 解析为秒（float），失败返回 None。"""
    if not value:
        return None
    try:
        parts = [float(p) for p in value.split(":")]
    except ValueError:
        return None
    if not parts:
        return None
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def parse_render_stats(stats: str) -> dict:
    """从 render_stats 字符串解析渲染进度。

    返回 dict（字段缺失则不出现）：
    - samples / samples_total: Cycles 当前块（tile）的当前采样 / 总采样
    - tiles_done / tiles_total: 已渲染完成的分块数 / 分块总数（大图分块渲染）
    - remaining: Blender 预计剩余时间（秒）
    - time: 本帧渲染已用时间（秒，帧完成时的精确值）
    """
    if not stats:
        return {}
    out = {}
    m = _SAMPLE_RE.search(stats)
    if m:
        out["samples"] = int(m.group(1))
        out["samples_total"] = int(m.group(2))
    m = _TILES_RE.search(stats)
    if m:
        out["tiles_done"] = int(m.group(1))
        out["tiles_total"] = int(m.group(2))
    m = _REMAINING_RE.search(stats)
    if m:
        remaining = _parse_timer(m.group(1))
        if remaining is not None:
            out["remaining"] = remaining
    m = _TIME_RE.search(stats)
    if m:
        t = _parse_timer(m.group(1))
        if t is not None:
            out["time"] = t
    return out


def format_duration(seconds) -> str:
    """把秒数格式化为 "MM:SS.cc"（不足一小时）或 "HH:MM:SS"（超过一小时）。"""
    if seconds is None:
        return ""
    try:
        centis = int(round(max(0.0, float(seconds)) * 100))
    except (TypeError, ValueError):
        return ""
    s = centis // 100
    if s >= 3600:
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}.{centis % 100:02d}"


def format_samples(samples, samples_total) -> str:
    """格式化采样进度 "12/256"；无采样信息时返回空串。"""
    try:
        return f"{int(samples)}/{int(samples_total)}"
    except (TypeError, ValueError):
        return ""


def compute_tile_weights(width: int, height: int, tile_size: int) -> list:
    """按 Cycles 行优先（从上到下、从左到右）分块，计算每块像素占总像素的比例。

    用于按真实像素工作量重建渲染整体进度（引擎不直接报告该值）：
    边缘块尺寸小于 tile_size，像素占比随之变小。返回权重列表（和为 1），
    顺序与 Cycles 的 Rendered X/Y Tiles 完成顺序一致。
    """
    tile = max(int(tile_size or 0), 1)
    w = max(int(width or 0), 1)
    h = max(int(height or 0), 1)
    total_px = w * h
    weights = []
    y = 0
    while y < h:
        th = min(tile, h - y)
        x = 0
        while x < w:
            tw = min(tile, w - x)
            weights.append((tw * th) / total_px)
            x += tw
        y += th
    return weights
