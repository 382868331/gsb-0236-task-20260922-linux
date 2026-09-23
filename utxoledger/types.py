"""公开数据结构（冻结 dataclass，可哈希）。

模型概览
========

* :class:`OutPoint` —— ``(txid, output_index)``，唯一标识一个交易输出。
* :class:`Output`  —— UTXO 的内容：整数金额 ``value`` 与锁定高度
  ``lock_height``。该输出只能在链上高度 ``>= lock_height`` 的区块中被花费。
* :class:`Input`   —— 对某个 OutPoint 的引用（本模型不做脚本/签名验证）。
* :class:`Transaction` —— 有序输入列表 + 有序输出列表。
  ``is_coinbase`` 为真时必须没有输入。
* :class:`Block`   —— ``block_id``、``parent``（创世为 ``None``）、
  正整数 ``work``、按序执行的交易列表。
* :class:`UTXO`    —— 账本视图中的条目：``(outpoint, output, coinbase)``。
* :class:`Reorg`   —— 一次 tip 切换的撤销 / 重放路径。

所有金额均为 Python 任意精度非负整数；本模型不出现浮点数，因此不存在
浮点误差口径问题（README 中明确说明）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "OutPoint",
    "Output",
    "Input",
    "Transaction",
    "Block",
    "UTXO",
    "Reorg",
]


@dataclass(frozen=True, slots=True)
class OutPoint:
    """交易输出的全局引用。"""

    txid: str
    output_index: int


@dataclass(frozen=True, slots=True)
class Output:
    """UTXO 内容。

    :param value: 非负整数金额。
    :param lock_height: 锁定高度；只能在链上高度
        ``block_height >= lock_height`` 的区块中被花费。创世高度为 0。
    """

    value: int
    lock_height: int


@dataclass(frozen=True, slots=True)
class Input:
    """交易输入：引用一个已存在的 OutPoint。"""

    outpoint: OutPoint


@dataclass(frozen=True, slots=True)
class Transaction:
    """交易。

    * 普通交易：至少一个输入；按列表顺序花费输入、创建输出。
      契约要求输入不可重复，且输出和 <= 输入和（差额作为手续费）。
    * coinbase 交易：``is_coinbase=True``，输入必须为空；输出总额不得
      超过 ``固定补贴 + 本块所有普通交易手续费之和``。
    """

    txid: str
    inputs: tuple[Input, ...] = ()
    outputs: tuple[Output, ...] = ()
    is_coinbase: bool = False


@dataclass(frozen=True, slots=True)
class Block:
    """区块。

    :param block_id: 合成的唯一字符串 ID（不验证 PoW，也不要求哈希格式）。
    :param parent: 父块 ID；创世块为 ``None``。
    :param work: 正整数工作量；链的累计 work 为各块 work 之和。
    :param transactions: 按序执行的交易。每块至多一个 coinbase 交易，
        若存在则必须位于交易列表首位。
    """

    block_id: str
    parent: str | None
    work: int
    transactions: tuple[Transaction, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class UTXO:
    """账本视图中的一个未花费输出。"""

    outpoint: OutPoint
    output: Output

    @property
    def value(self) -> int:
        return self.output.value

    @property
    def lock_height(self) -> int:
        return self.output.lock_height


@dataclass(frozen=True, slots=True)
class Reorg:
    """一次 tip 切换路径。

    * ``old_tip`` 为 ``None`` 表示从“无 tip”初始激活（仅发生在添加创世块时）。
    * ``deactivated`` / ``activated`` 均为从分叉点向外的顺序：
      撤销路径是 old tip 向下走到共同祖先，重放路径是共同祖先向上走到 new tip。
    """

    old_tip: str | None
    new_tip: str
    deactivated: tuple[str, ...]
    activated: tuple[str, ...]

    @property
    def is_reorg(self) -> bool:
        """True 表示发生了真实重组（撤销了至少一个旧块）。"""

        return bool(self.deactivated)
