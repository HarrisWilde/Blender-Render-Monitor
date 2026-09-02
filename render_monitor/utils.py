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


def sanitize_relative_path(path: str) -> str:
    """把名称/模板路径清洗成安全的相对路径，保留 `/` 作为子文件夹分隔。

    `\\` 也会先归一化为 `/`；每个路径段单独做文件名清洗，因此 `..`、空段、
    绝对路径等都不会逃出输出目录。返回的路径统一使用 `/` 分隔。
    """
    if not path:
        return "shot"
    normalized = str(path).replace("\\", "/")
    parts = []
    for part in normalized.split("/"):
        clean = sanitize_filename(part)
        if clean not in ("", "."):
            parts.append(clean)
    if not parts:
        return "shot"
    return "/".join(parts)


# 匹配文件名扩展名之前最末尾的一段连续数字，例如 "快照_001" 中的 "001"
_TRAILING_NUMBER_RE = re.compile(r"(\d+)$")
# 纯数字“扩展名”（例如 "快照.1" 的 ".1"）：应视为数字后缀而不是文件扩展名
_NUMERIC_EXT_RE = re.compile(r"\.[0-9]+$")


def adjust_name_number(name: str, delta: int) -> str:
    """像 Blender 文件保存框的 +/- 按钮那样调整名称末尾的数字。

    若名称在扩展名之前已有数字（例如 "快照_001"），则对这段数字加/减；
    "快照.1" 这类末尾是纯数字的也会视为数字后缀而不是扩展名，按 "快照.2"
    递增。并尽量保留原有前导零位数；若没有数字，则在名称末尾追加 0/1/2……。
    与 Blender 的 FILE_OT_filenum 逻辑保持一致：减少时跨过 100→99、10→9
    会自然减少一位前导零；当 `快照1` 或 `快照.1` 再减时会去掉数字，且结果
    不会小于 0。
    """
    if not name:
        return name
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        return name

    base, ext = os.path.splitext(name)
    if _NUMERIC_EXT_RE.fullmatch(ext or ""):
        # "快照.1" 这类名称：.1 是数字后缀，不是扩展名
        head = base + "."
        digits = ext[1:]
        pic = int(digits)
        num_len = len(digits)
        tail = ""
    else:
        match = _TRAILING_NUMBER_RE.search(base)
        if match:
            head = base[:match.start()]
            digits = match.group(1)
            pic = int(digits)
            num_len = len(digits)
            tail = ext
        else:
            head = base
            pic = 0
            num_len = 0
            tail = ext

    # 从 100 减到 99 / 从 10 减到 9 时，去掉多余的前导零位数
    if delta < 0 and num_len > 0:
        exp = 10 ** (num_len - 1)
        if pic >= exp and pic + delta < exp:
            num_len -= 1

    pic += delta
    if pic < 0:
        pic = 0

    if num_len:
        number = str(pic).zfill(num_len)
    elif pic:
        number = str(pic)
    else:
        # 等价于 C 的 printf("%.0d", 0)：宽度 0 且值为 0 时不输出数字
        number = ""
    # 数字被完全移除时，顺带去掉分隔点：快照.1 → 快照，shot.1.png → shot.png
    if not number and head.endswith("."):
        head = head[:-1]
    return head + number + tail


def format_filename(template: str, shot_name: str, frame: int, index: int = 1) -> str:
    """按模板生成输出文件名（不含扩展名，可含子文件夹相对路径）。

    模板支持 {name}（快照名）、{index}（列表顺序，从 1 开始）、{frame}（帧号）
    占位符。快照名里的 `/` 或 `\\` 会作为子文件夹分隔符保留；模板缺少任何
    动态占位符（会导致所有快照输出同名互相覆盖）或格式错误时，自动回退默认
    模板，保证返回安全相对路径。
    """
    tpl = (template or DEFAULT_FILE_TEMPLATE).strip()
    if "{name}" not in tpl and "{frame}" not in tpl and "{index}" not in tpl:
        tpl = DEFAULT_FILE_TEMPLATE
    # 先把 frame/index 归一化成安全 int：入参为 None 或非数字时置 0，
    # 避免回退分支里的 int() 再次抛出未捕获异常。
    try:
        frame = int(frame)
        index = int(index)
    except (TypeError, ValueError):
        frame = index = 0
    safe_name = sanitize_relative_path(shot_name)
    try:
        raw = tpl.format(
            name=safe_name,
            frame=frame,
            index=index,
        )
    except (KeyError, IndexError, ValueError, AttributeError):
        raw = DEFAULT_FILE_TEMPLATE.format(
            name=safe_name,
            frame=frame,
            index=index,
        )
    return sanitize_relative_path(raw)


def build_output_path(outdir_abs: str, filename: str, ext: str) -> str:
    """拼接输出目录 + 文件名 + 扩展名。扩展名去掉前导点并小写化。"""
    ext = (ext or "png").lstrip(".").lower()
    return os.path.join(outdir_abs, f"{filename}.{ext}")


# ---------------------------------------------------------------------------
# 进度共享内存（文件-backed mmap）
# ---------------------------------------------------------------------------
# 子进程（rm_job.py）与主进程（ops.py）通过同一个文件的内存映射交换渲染进度，
# 读写走内核页面缓存、不产生真实磁盘 IO（映射文件本身仍落在磁盘上，保留
# "子进程崩溃后主进程仍能读取最后状态"的现场语义）。缓冲布局：
#   [0:4]  大端 unsigned int：payload 长度（写端最后写，读端看到新长度时
#          payload 必然已完整写入，天然原子）
#   [4:4+N] JSON payload（{"state", "total", "shots"}）
# 单写者单读者，无需锁。
PROGRESS_MMAP_SIZE = 1 << 20  # 1 MiB（进度 JSON 通常几十 KB，留足余量）
PROGRESS_HEADER = 4           # 长度前缀字节数（struct 大端 ">I"）


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
