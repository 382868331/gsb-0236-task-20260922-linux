"""错误语义（公开契约）。

所有由本库主动抛出的错误都继承 :class:`LedgerError`，便于调用方一次捕获。

分类原则：
* :class:`ValidationError` 及其子类——输入（区块 / 交易 / 数值）违反契约，
  错误带有可定位的 ``location`` 字符串与稳定的 ``code``。
* :class:`NoSolutionError`——搜索空间内可证明无解（例如区块树为空，
  不存在任何候选 tip）。
* :class:`BudgetExhaustedError`——搜索预算被耗尽，*不*代表无解，只代表
  在给定预算内未能完成枚举；与 :class:`NoSolutionError` 严格区分。
"""

from __future__ import annotations


class LedgerError(Exception):
    """本库所有异常的公共基类。"""


# ---------------------------------------------------------------------------
# 搜索类错误：预算耗尽 vs 证明无解，必须分开
# ---------------------------------------------------------------------------


class NoSolutionError(LedgerError):
    """候选集合可证明为空（无解），与“预算不够”是两回事。"""


class BudgetExhaustedError(LedgerError):
    """tip 搜索访问的节点数超过预算。

    这不表示不存在合法 tip，只表示在给定预算下未能完成枚举；
    调大预算后可能成功。
    """

    def __init__(self, message: str, *, budget: int, visited: int) -> None:
        super().__init__(message)
        self.budget = budget
        self.visited = visited


# ---------------------------------------------------------------------------
# 输入校验类错误
# ---------------------------------------------------------------------------


class ValidationError(LedgerError):
    """所有结构性 / 语义性输入错误的基类。

    :param message: 人类可读说明。
    :param location: 可定位字符串，例如
        ``"block=B2 height=2 tx=T2 tx_index=1 input=0"``。
    :param code: 稳定的机器可读错误码。
    """

    code = "validation_error"

    def __init__(self, message: str, *, location: str | None = None,
                 code: str | None = None) -> None:
        if location:
            full = f"{message} (at {location})"
        else:
            full = message
        super().__init__(full)
        self.message = message
        self.location = location
        if code is not None:
            self.code = code


class InvalidBlockError(ValidationError):
    """区块结构不合法（字段类型、parent 规则、coinbase 位置等）。"""

    code = "invalid_block"


class DuplicateBlockError(ValidationError):
    """提交了区块树中已存在的 block_id。"""

    code = "duplicate_block"


class UnknownParentError(ValidationError):
    """区块的 parent 在区块树中不存在（且不是创世块）。"""

    code = "unknown_parent"


class ChainLimitError(ValidationError):
    """该区块所在链长度超过接口上限（默认 100 块，含创世块）。"""

    code = "chain_limit"


class InvalidTransactionError(ValidationError):
    """交易级语义错误（txid 在链上重放、coinbase 规则等）。"""

    code = "invalid_transaction"


class DuplicateInputError(ValidationError):
    """同一笔交易内出现了重复的输入 OutPoint。"""

    code = "duplicate_input"


class MissingOutPointError(ValidationError):
    """输入引用的 OutPoint 在当前 UTXO 集合中不存在（未曾创建或已被花费）。"""

    code = "missing_outpoint"


class LockHeightError(ValidationError):
    """输入引用的 UTXO 尚未达到锁定高度（``block_height < lock_height``）。"""

    code = "lock_height"

    def __init__(self, message: str, *, location: str | None,
                 block_height: int, lock_height: int) -> None:
        super().__init__(message, location=location, code=self.code)
        self.block_height = block_height
        self.lock_height = lock_height


class ValueBalanceError(ValidationError):
    """非 coinbase 交易输出和大于输入和。"""

    code = "value_balance"

    def __init__(self, message: str, *, location: str | None,
                 input_sum: int, output_sum: int) -> None:
        super().__init__(message, location=location, code=self.code)
        self.input_sum = input_sum
        self.output_sum = output_sum


class CoinbaseExcessError(ValidationError):
    """coinbase 输出总额超过 ``固定补贴 + 本块手续费``。"""

    code = "coinbase_excess"

    def __init__(self, message: str, *, location: str | None,
                 claimed: int, cap: int) -> None:
        super().__init__(message, location=location, code=self.code)
        self.claimed = claimed
        self.cap = cap
