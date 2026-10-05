"""SoulCore 异常层级。所有失败都 fail closed。"""

from __future__ import annotations


class SoulCoreError(Exception):
    """基类。"""


class ContractViolation(SoulCoreError):
    """身份契约本身不合法（schema / 治理 / 内容寻址失败）。"""


class GovernanceError(SoulCoreError):
    """治理闸门拒绝某次记忆操作。"""


class UnauthorizedOverride(GovernanceError):
    """无人类授权却试图写入偏离核心的记忆。"""
