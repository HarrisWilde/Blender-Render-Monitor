"""Render Monitor 纯逻辑单元测试（不依赖 bpy，用系统 Python 运行）。

运行：python -m unittest render_monitor.tests.test_utils
"""

from __future__ import annotations

import os
import unittest

from ..utils import (
    DEFAULT_FILE_TEMPLATE,
    build_output_path,
    compute_tile_weights,
    format_duration,
    format_filename,
    format_samples,
    parse_render_stats,
    sanitize_filename,
)


class TestSanitizeFilename(unittest.TestCase):
    def test_invalid_chars(self):
        self.assertEqual(sanitize_filename('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_spaces_to_underscore(self):
        self.assertEqual(sanitize_filename("my shot 01"), "my_shot_01")

    def test_empty_and_dots(self):
        self.assertEqual(sanitize_filename(""), "shot")
        self.assertEqual(sanitize_filename("."), "shot")
        self.assertEqual(sanitize_filename(".."), "shot")

    def test_whitespace_only(self):
        self.assertEqual(sanitize_filename("   "), "shot")

    def test_unicode_kept(self):
        self.assertEqual(sanitize_filename("快照-01"), "快照-01")

    def test_length_limited(self):
        self.assertEqual(len(sanitize_filename("x" * 300)), 120)


class TestFormatFilename(unittest.TestCase):
    def test_default_template(self):
        # 默认模板 {name} {index}：空格被清洗为下划线
        self.assertEqual(format_filename(None, "shot A", 12, index=3), "shot_A_3")
        self.assertEqual(format_filename("", "shot", 5, index=1), "shot_1")

    def test_index_placeholder(self):
        # {index} = 列表顺序编号（从 1 开始）
        self.assertEqual(format_filename("{name} {index}", "快照", 10, index=2), "快照_2")
        self.assertEqual(
            format_filename("{name}_{index:02d}", "cube", 10, index=7), "cube_07"
        )

    def test_custom_template(self):
        self.assertEqual(
            format_filename("{name}_{frame:03d}", "cube", 7, index=5), "cube_007"
        )

    def test_unsafe_name_sanitized(self):
        self.assertEqual(format_filename("{name}", "a/b:c", 1, index=1), "a_b_c")

    def test_bad_template_falls_back(self):
        # 缺 {name}/{frame}/{index} 占位符 → 回退默认模板
        out = format_filename("plain-text", "shot", 3, index=2)
        self.assertEqual(out, "shot_2")

    def test_bad_format_spec_falls_back(self):
        out = format_filename("{name}{bad}", "shot", 3, index=2)
        self.assertEqual(out, "shot_2")

    def test_invalid_frame_index_falls_back_safely(self):
        # frame/index 为 None（非法入参）时，回退分支里的 int() 不应再次抛异常
        out = format_filename("{name} {frame}", "shot", None, index=None)
        self.assertEqual(out, "shot_0")

    def test_bad_template_with_bad_frame(self):
        # 模板非法 + frame 非法叠加：回退默认模板也要安全返回
        out = format_filename("plain-text", "shot", None, index=None)
        self.assertEqual(out, "shot_0")


class TestBuildOutputPath(unittest.TestCase):
    def test_ext_lstrip_dot(self):
        self.assertEqual(
            build_output_path("C:/out", "shot", ".png"),
            os.path.join("C:/out", "shot.png"),
        )

    def test_no_ext_defaults_png(self):
        self.assertEqual(
            build_output_path("out", "shot", ""), os.path.join("out", "shot.png")
        )

    def test_full_flow(self):
        name = format_filename(DEFAULT_FILE_TEMPLATE, "相 机 A", 42, index=7)
        path = build_output_path("D:/renders", name, ".PNG")
        self.assertEqual(path, os.path.join("D:/renders", "相_机_A_7.png"))


class TestParseRenderStats(unittest.TestCase):
    def test_cycles_sample_stage(self):
        # Cycles 采样阶段：Blender 直接给出剩余时间与采样进度
        out = parse_render_stats("Remaining: 00:01.33 | Mem: 6M | Sample 96/256")
        self.assertEqual(out["samples"], 96)
        self.assertEqual(out["samples_total"], 256)
        self.assertAlmostEqual(out["remaining"], 1.33, places=2)
        self.assertNotIn("time", out)

    def test_tiled_render(self):
        # 大图分块渲染：解析块进度（块切换后采样计数会重置）
        out = parse_render_stats(
            "Remaining: 00:15.41 | Mem: 306M | Rendered 1/4 Tiles, Sample 128/128"
        )
        self.assertEqual(out["tiles_done"], 1)
        self.assertEqual(out["tiles_total"], 4)
        self.assertEqual(out["samples"], 128)
        self.assertEqual(out["samples_total"], 128)
        # 无分块的 stats 不含 tiles 字段
        self.assertNotIn("tiles_done", parse_render_stats("Mem: 6M | Sample 96/256"))

    def test_cycles_frame_done(self):
        # 帧完成：精确已用时间
        out = parse_render_stats("Time: 00:01.00 (Saving: 00:00.11)")
        self.assertAlmostEqual(out["time"], 1.0, places=2)
        self.assertNotIn("samples", out)

    def test_hour_long_remaining(self):
        out = parse_render_stats("Remaining: 01:02:03.45 | Mem: 100M | Sample 1/64")
        self.assertAlmostEqual(out["remaining"], 3723.45, places=2)

    def test_zero_sample(self):
        out = parse_render_stats("Mem: 1M | Sample 0/256")
        self.assertEqual(out["samples"], 0)
        self.assertEqual(out["samples_total"], 256)

    def test_init_stage_returns_empty(self):
        # 初始化阶段无采样/时间信息
        for s in ("Mem: 0M | Initializing",
                  "Mem: 1M | Updating Objects",
                  "Mem: 6M | Finished"):
            self.assertEqual(parse_render_stats(s), {})

    def test_none_or_empty(self):
        self.assertEqual(parse_render_stats(None), {})
        self.assertEqual(parse_render_stats(""), {})


class TestFormatDurationAndSamples(unittest.TestCase):
    def test_format_duration(self):
        self.assertEqual(format_duration(1.33), "00:01.33")
        self.assertEqual(format_duration(61), "01:01.00")
        self.assertEqual(format_duration(3723.45), "01:02:03")
        self.assertEqual(format_duration(0), "00:00.00")
        self.assertEqual(format_duration(None), "")

    def test_format_samples(self):
        self.assertEqual(format_samples(12, 256), "12/256")
        self.assertEqual(format_samples(None, None), "")
        self.assertEqual(format_samples(0, 0), "0/0")
        self.assertEqual(format_samples("x", 1), "")


class TestComputeTileWeights(unittest.TestCase):
    def test_uniform_grid(self):
        # 1920x1080, tile 960 → 2x2 均匀网格，每块权重 = 像素占比
        weights = compute_tile_weights(1920, 1080, 960)
        self.assertEqual(len(weights), 4)
        self.assertAlmostEqual(sum(weights), 1.0, places=6)
        total = 1920 * 1080
        self.assertAlmostEqual(weights[0], 960 * 960 / total, places=6)
        self.assertAlmostEqual(weights[3], 960 * 120 / total, places=6)

    def test_single_tile(self):
        self.assertEqual(compute_tile_weights(800, 600, 2048), [1.0])

    def test_row_major_order(self):
        # 行优先顺序与 Cycles 的 Rendered X/Y Tiles 一致：1200x800 tile 500 → 3x2
        weights = compute_tile_weights(1200, 800, 500)
        self.assertEqual(len(weights), 6)
        total = 1200 * 800
        self.assertAlmostEqual(weights[0], 500 * 500 / total, places=6)
        self.assertAlmostEqual(weights[2], 200 * 500 / total, places=6)  # 第一行最右小块
        self.assertAlmostEqual(weights[5], 200 * 300 / total, places=6)  # 最小块

    def test_bad_inputs(self):
        self.assertEqual(compute_tile_weights(0, 0, 0), [1.0])


if __name__ == "__main__":
    unittest.main()
