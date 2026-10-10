"""StrangeUtaGame 版本号。

发布流程：
- 推送一个 git tag，格式 ``SUGv{version}``（如 ``SUGv0.3.2``）。
- 发布资产命名为 ``StrangeUtaGame-v{version}.zip``（main 变体）。

变体（VARIANT）说明：
- ``""``        主版本（Windows + WinRT 日语注音）
- ``"noWinIME"``  Windows 版，不含 WinRT IME 依赖，使用 sudachi-mini 注音
- ``"mac-arm64"`` macOS Apple Silicon 版，不含 WinRT IME 依赖，使用 sudachi-mini 注音
- ``"mac-intel"`` macOS Intel(x86_64) 版，不含 WinRT IME 依赖，使用 sudachi-mini 注音

各变体对应的资产名：
- main:      ``StrangeUtaGame-v{version}.zip``
- noWinIME:  ``StrangeUtaGame-noWinIME-v{version}.zip``
- mac-arm64: ``StrangeUtaGame-mac-arm64-v{version}.zip``
- mac-intel: ``StrangeUtaGame-mac-intel-v{version}.zip``

更新器（``strange_uta_game.updater``）与设置-关于/语言卡片均从此处读取版本号。
请始终使用 :class:`Version`（``packaging.version`` 兼容的语义版本字符串）。
"""

from __future__ import annotations

__version__ = "1.7.2"
# 构建变体标识。build.py 在打包前将此行替换为对应变体值，打包后还原。
# 运行时只读，请勿在应用逻辑中修改。
VARIANT = ""  # "" | "noWinIME" | "mac-arm64" | "mac-intel"

# Git tag 前缀。GitHub Release 的 tag 命名规则为 ``{TAG_PREFIX}{__version__}``。
TAG_PREFIX = "SUGv"

# 仓库标识：用于构造 GitHub / 镜像下载 URL。
REPO_OWNER = "karaoke-studio"
REPO_NAME = "StrangeUtaGame"


def _variant_suffix() -> str:
    """返回变体名中缀，如 ``"-noWinIME"``；main 变体返回空字符串。"""
    return f"-{VARIANT}" if VARIANT else ""


# Release 资产名称模板（含 ``{version}`` 占位）。注意保留前缀 ``v``。
# 由于 VARIANT 在模块加载时已确定，此模板与 VARIANT 同步。
ASSET_NAME_TEMPLATE = (
    "StrangeUtaGame-v{version}.zip" if not VARIANT
    else f"StrangeUtaGame-{VARIANT}-v{{version}}.zip"
)


def current_tag() -> str:
    """返回当前版本对应的 Git tag 字符串。"""
    return f"{TAG_PREFIX}{__version__}"


def current_asset_name() -> str:
    """返回当前版本对应的发布资产文件名（含变体中缀）。"""
    return f"StrangeUtaGame{_variant_suffix()}-v{__version__}.zip"


__all__ = [
    "__version__",
    "VARIANT",
    "TAG_PREFIX",
    "ASSET_NAME_TEMPLATE",
    "REPO_OWNER",
    "REPO_NAME",
    "current_tag",
    "current_asset_name",
]
