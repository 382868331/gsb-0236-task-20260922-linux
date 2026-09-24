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
* :class:`PrunedBlockError`     —— 区块体已被最终确认检查点裁剪，只剩块头，
  无法再取完整交易。
* :class:`RequiresHistoryError` —— 深度重组/接块所需的祖先历史已被裁剪：
  既未证明候选链合法也未证明非法，需要外部补全历史；稳定状态码
  ``requires_history``。
* :class:`CheckpointError` / :class:`CheckpointFormatError` —— 检查点导出/
  恢复错误；后者表示落盘字节被篡改、截断或结构自相矛盾。
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


class PrunedBlockError(LedgerError):
    """请求的区块体已被最终确认检查点裁剪（只剩块头，交易不可再取）。"""

    def __init__(self, block_id: str):
        super().__init__(
            f"block {block_id!r} body has been pruned by finalization "
            f"(header only)"
        )
        self.block_id = block_id


class RequiresHistoryError(LedgerError):
    """候选链需要的祖先历史已被裁剪，无法完成验证或深度重组。

    这与“区块被证明非法”严格区分：候选链只是在已知信息下**不可判定**，
    调用方需要取得被裁剪高度的历史块后（在未裁剪的账本上）重试。

    稳定状态码为字面量 ``"requires_history"``（:attr:`status`）。
    """

    #: 稳定短码（供程序分支与跨语言/持久化格式使用）。
    status = "requires_history"

    def __init__(
        self,
        block_id: str,
        anchor_id: str | None,
        anchor_height: int | None,
        fork_id: str | None,
        missing_height: int | None = None,
    ) -> None:
        if anchor_id is None:
            detail = "no finalized checkpoint history is available"
        else:
            detail = (
                f"chain would diverge below finalized anchor "
                f"{anchor_id!r} (height {anchor_height})"
            )
            if fork_id is not None and fork_id != anchor_id:
                detail += f"; known fork point is {fork_id!r}"
            if missing_height is not None:
                detail += f"; history at/before height {missing_height} is required"
        super().__init__(f"{self.status}: block {block_id!r}: {detail}")
        self.block_id = block_id
        self.anchor_id = anchor_id
        self.anchor_height = anchor_height
        self.fork_id = fork_id
        self.missing_height = missing_height


class CheckpointError(LedgerError):
    """检查点导出/恢复的公共基类（IO 失败、未最终化即导出等）。"""


class CheckpointFormatError(CheckpointError):
    """落盘检查点字节被损坏、截断、信封不符或内容自相矛盾。

    加载遇到本异常时不会产生新账本，也不会改动任何既有对象。
    """

    def __init__(self, message: str, *, reason: str = "corrupt-checkpoint") -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason


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
