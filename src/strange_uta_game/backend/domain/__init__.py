"""领域层模块。

领域层定义核心业务概念和业务规则，是整个应用的核心。
它不依赖任何外部框架或库，纯 Python 实现。

数据层次（自底向上）：
  Ruby → Character → Word → Sentence → Project
"""

from .models import (
    DomainError,
    ValidationError,
    RubyMoraDegradeError,
    TimeTagType,
    PUNCTUATION_SET,
    RubyPart,
    Ruby,
    Character,
    Word,
)

from .entities import (
    Singer,
    AxisGroup,
    AuxTrack,
    Sentence,
)

from .project import (
    ProjectMetadata,
    Project,
)

__all__ = [
    # 错误
    "DomainError",
    "ValidationError",
    "RubyMoraDegradeError",
    # 枚举
    "TimeTagType",
    "PUNCTUATION_SET",
    # 数据结构（自底向上）
    "RubyPart",
    "Ruby",
    "Character",
    "Word",
    # 实体
    "Singer",
    "AxisGroup",
    "AuxTrack",
    "Sentence",
    # 聚合根
    "ProjectMetadata",
    "Project",
]
