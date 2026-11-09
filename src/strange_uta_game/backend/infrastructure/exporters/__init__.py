"""导出器模块。

提供各种格式的歌词导出功能。
"""

from .base import BaseExporter, ExportError, IExporter
from .inline_exporter import InlineExporter
from .kasugamuki_exporter import KasugamukiExporter
from .kasugamuki_romaji_exporter import KirakaraExporter, KasugamukiRomajiExporter
from .lrc_exporter import KRAExporter, LRCExporter, LRCLineExporter, LRCWordExporter
from .nicokara_exporter import NicokaraExporter, NicokaraWithRubyExporter
from .srt_exporter import SRTExporter
from .ttml_exporter import TTMLExporter
from .txt2ass_exporter import ASSDirectExporter, Txt2AssExporter
from .txt_exporter import TXTExporter

# 所有可用的导出器
ALL_EXPORTERS = [
    LRCExporter,
    LRCLineExporter,
    LRCWordExporter,
    KRAExporter,
    TXTExporter,
    SRTExporter,
    Txt2AssExporter,
    ASSDirectExporter,
    NicokaraExporter,
    NicokaraWithRubyExporter,
    InlineExporter,
    KasugamukiExporter,
    KirakaraExporter,
    # 新增交换格式放在末尾：既有格式的顺序/行为保持不变。
    TTMLExporter,
]


def get_exporter_by_name(name: str) -> IExporter:
    """根据名称获取导出器实例

    Args:
        name: 导出器名称 ('LRC (增強型)', 'KRA', 'TXT', 等)

    Returns:
        导出器实例

    Raises:
        ValueError: 找不到对应名称的导出器
    """
    # 向后兼容：旧配置中 "LRC" 映射到 "LRC (增强型)"
    _LEGACY_NAME_MAP = {
        "LRC": "LRC (增强型)",
        "春日向注音（带罗马音）": "Kirakara",
    }
    resolved = _LEGACY_NAME_MAP.get(name, name)

    for exporter_class in ALL_EXPORTERS:
        exporter = exporter_class()
        if exporter.name == resolved:
            return exporter

    raise ValueError(f"未知的导出器: {name}")


def get_exporter_by_extension(ext: str) -> IExporter:
    """根据扩展名获取导出器实例

    Args:
        ext: 文件扩展名 (如 '.lrc', '.txt')

    Returns:
        导出器实例

    Raises:
        ValueError: 找不到对应扩展名的导出器
    """
    ext = ext.lower()

    for exporter_class in ALL_EXPORTERS:
        exporter = exporter_class()
        if exporter.file_extension.lower() == ext:
            return exporter

    raise ValueError(f"不支持的扩展名: {ext}")


def get_all_exporters():
    """获取所有导出器实例"""
    return [exporter_class() for exporter_class in ALL_EXPORTERS]


__all__ = [
    "IExporter",
    "BaseExporter",
    "ExportError",
    "LRCExporter",
    "LRCLineExporter",
    "LRCWordExporter",
    "KRAExporter",
    "TXTExporter",
    "SRTExporter",
    "Txt2AssExporter",
    "ASSDirectExporter",
    "NicokaraExporter",
    "NicokaraWithRubyExporter",
    "InlineExporter",
    "KasugamukiExporter",
    "KasugamukiRomajiExporter",
    "KirakaraExporter",
    "TTMLExporter",
    "ALL_EXPORTERS",
    "get_exporter_by_name",
    "get_exporter_by_extension",
    "get_all_exporters",
]
