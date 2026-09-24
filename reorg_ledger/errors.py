"""错误语义（公开契约）。

所有本模块抛出的异常都继承 :class:`LedgerError`，便于调用方统一捕获。

分类
----
* :class:`InputContractError`   —— 调用方违反接口契约（类型错误、字段越界等），
  属于“非法输入”，错误信息包含可定位的字段路径。
* :class:`NonFiniteNumberError` —— 数值输入为 NaN / Infinity（本模块只接受整数，
  浮点一律拒绝；非有限浮点单独给出明确错误，而不是淹没在类型错误里）。
* :class:`BlockValidationError` —— **证明无解**：区块在其祖先状态上被确定性地证伪
  （双花、超额 coinbase、锁定高度不足、输出超过输入等），带块/交易/OutPoint 定位。
* :class:`OrphanBlockError`     —— 既不能证明合法也不能证明非法：父块缺失。
* :class:`ChainLimitError`      —— **搜索/处理预算耗尽**：链长超过 100 块的接口
  上限，与“区块被证明非法”严格区分。
* :class:`DuplicateBlockError`  —— 同一区块 ID 被重复提交。
* :class:`CheckpointCorruptError` —— 检查点字节流损坏或内部不一致（校验和/指纹
  不符、结构不合法），恢复被拒绝，不产生半成品账本。
"""

from __future__ import annotations


class LedgerError(Exception):
    """本模块所有异常的公共基类。"""


class InputContractError(LedgerError, ValueError):
    """调用方违反公开接口契约（类型、取值范围、结构）。"""


class NonFiniteNumberError(InputContractError):
    """传入了 NaN 或 ±Infinity。"""


class LedgerStateError(LedgerError):
    """对象内部状态被破坏或 API 调用顺序非法（正常使用不会触发）。"""


class DuplicateBlockError(LedgerError):
    """提交了树中已存在的区块 ID。"""

    def __init__(self, block_id: str):
        super().__init__(f"block {block_id!r} already exists in the block tree")
        self.block_id = block_id


class CheckpointCorruptError(LedgerError):
    """检查点数据损坏或内部语义不一致，恢复被拒绝。

    覆盖：字节流不是合法 JSON、顶层结构不符、magic/版本不符、参数不一致、
    校验和不匹配、锚点状态指纹不一致、窗口块无法从锚点重放验证等。
    以 :attr:`kind` 给出稳定短码。恢复失败时不构造任何半成品账本。
    """

    def __init__(self, kind: str, message: str):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message


class OrphanBlockError(LedgerError):
    """父块未知：缺少祖先，无法以祖先状态验证该区块。"""

    def __init__(self, block_id: str, parent_id: str):
        super().__init__(
            f"block {block_id!r} references unknown parent {parent_id!r}"
        )
        self.block_id = block_id
        self.parent_id = parent_id


class ChainLimitError(LedgerError):
    """处理预算耗尽：候选链超过 ``MAX_CHAIN_BLOCKS``（默认 100）块。

    这不是“区块非法”的判定——长链没有被证伪，只是超出了本题的有界接口。
    """

    def __init__(self, block_id: str, depth: int, limit: int):
        super().__init__(
            f"block {block_id!r} would make a chain of {depth} blocks, "
            f"exceeding the bounded limit of {limit}"
        )
        self.block_id = block_id
        self.depth = depth
        self.limit = limit


class BlockValidationError(LedgerError):
    """区块在其祖先状态上被证明非法。

    定位字段尽量填充：``block_id`` / ``height`` / ``tx_id`` / ``outpoint``。
    ``reason`` 是稳定的英文短码，``message`` 是人类可读说明。
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        block_id: str | None = None,
        height: int | None = None,
        tx_id: str | None = None,
        outpoint: object | None = None,
    ) -> None:
        loc = []
        if block_id is not None:
            loc.append(f"block={block_id!r}")
        if height is not None:
            loc.append(f"height={height}")
        if tx_id is not None:
            loc.append(f"tx={tx_id!r}")
        if outpoint is not None:
            loc.append(f"outpoint={outpoint!s}")
        suffix = f" [{', '.join(loc)}]" if loc else ""
        super().__init__(f"{reason}: {message}{suffix}")
        self.reason = reason
        self.message = message
        self.block_id = block_id
        self.height = height
        self.tx_id = tx_id
        self.outpoint = outpoint
