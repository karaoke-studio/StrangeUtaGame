"""启动期高分屏策略。

Qt 6 在 Windows 上默认使用 Per-Monitor DPI Aware V2。用户关闭高分屏
适配时，给 Windows 平台插件传入 ``dpiawareness=0``，让进程以 DPI
Unaware 模式运行：程序按 96 DPI 绘制，最终由 Windows 对整窗位图缩放。

DPI awareness 必须在 ``QApplication`` 创建时确定，因此本模块刻意不导入
任何 Qt 类型，只负责在启动早期构造 QApplication 参数。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

HIGH_DPI_SCALING_KEY = "ui.high_dpi_scaling"
WINDOWS_DPI_UNAWARE_PLATFORM = "windows:dpiawareness=0"


def build_qt_argv(
    argv: Sequence[str],
    *,
    high_dpi_scaling: bool,
    platform: str | None = None,
) -> list[str]:
    """返回供 ``QApplication`` 使用的参数副本。

    Windows 下关闭高分屏适配时，显式选择 DPI Unaware。其它平台以及开关
    开启时保持 Qt 默认行为。始终返回副本，避免 Qt 解析平台参数影响后续
    使用原始 ``sys.argv`` 打开关联文件的逻辑。
    """

    qt_argv = list(argv)
    current_platform = sys.platform if platform is None else platform
    if current_platform != "win32" or high_dpi_scaling:
        return qt_argv

    if not qt_argv:
        qt_argv.append("StrangeUtaGame")
    qt_argv[1:1] = ["-platform", WINDOWS_DPI_UNAWARE_PLATFORM]
    return qt_argv
