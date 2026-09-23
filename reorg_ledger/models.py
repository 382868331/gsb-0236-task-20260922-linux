"""公开数据结构。

层级划分（重要的错误语义分界）：

* **入口契约校验**只在本文件做：类型、``int`` 纯度（拒绝 ``bool`` / 浮点 /
  NaN / Infinity）、正数/非空等接口层面的要求，违反时抛
  :class:`~reorg_ledger.errors.InputContractError`。
* **协议语义校验**（双花、锁定高度、coinbase 超额、输入输出金额关系、
  区块内重复交易、coinbase 位置等）不在数据类里做——它们是“区块在祖先状态下
  被证伪”，由引擎抛 :class:`~reorg_ledger.errors.BlockValidationError`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from .errors import InputContractError, NonFiniteNumberError

__all__ = [
    "OutPoint",
    "TxInput",
    "TxOutput",
    "UTXO",
    "Transaction",
    "Block",
]


def _check_int(value: object, path: str, *, min_value: int | None = None) -> int:
    """整数入口契约：拒绝 bool、拒绝浮点（NaN/Inf 单独报错）、拒绝其他类型。"""
    if isinstance(value, bool):
        raise InputContractError(
            f"{path}: bool is not accepted as an integer (got {value!r})"
        )
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NonFiniteNumberError(
                f"{path}: number must be finite, got {value!r}"
            )
        raise InputContractError(
            f"{path}: expected an exact int, got float {value!r} (no silent coercion)"
        )
    if not isinstance(value, int):
        raise InputContractError(
            f"{path}: expected int, got {type(value).__name__}"
        )
    if min_value is not None and value < min_value:
        raise InputContractError(
            f"{path}: expected value >= {min_value}, got {value}"
        )
    return value


def _check_str(value: object, path: str) -> str:
    if not isinstance(value, str):
        raise InputContractError(
            f"{path}: expected str, got {type(value).__name__}"
        )
    if len(value) == 0:
        raise InputContractError(f"{path}: string must be non-empty")
    return value


def _check_seq(value: object, path: str) -> list:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(
        value, Iterable
    ):
        raise InputContractError(
            f"{path}: expected an ordered sequence (list/tuple), "
            f"got {type(value).__name__}"
        )
    return list(value)


@dataclass(frozen=True, slots=True)
class OutPoint:
    """UTXO 的全局定位：创建它的交易 ID + 输出下标（从 0 开始）。"""

    tx_id: str
    index: int

    def __post_init__(self) -> None:
        _check_str(self.tx_id, "OutPoint.tx_id")
        _check_int(self.index, "OutPoint.index", min_value=0)

    def __str__(self) -> str:
        return f"{self.tx_id}:{self.index}"


@dataclass(frozen=True, slots=True)
class TxInput:
    """交易输入：指向一个未花费输出。"""

    prev_tx: str
    prev_index: int

    @property
    def outpoint(self) -> OutPoint:
        return OutPoint(self.prev_tx, self.prev_index)

    def __post_init__(self) -> None:
        _check_str(self.prev_tx, "TxInput.prev_tx")
        _check_int(self.prev_index, "TxInput.prev_index", min_value=0)


@dataclass(frozen=True, slots=True)
class TxOutput:
    """交易输出：正整数金额 + 锁定高度（``lock_height`` 之前不可花费）。"""

    amount: int
    lock_height: int

    def __post_init__(self) -> None:
        _check_int(self.amount, "TxOutput.amount", min_value=1)
        _check_int(self.lock_height, "TxOutput.lock_height", min_value=0)


# UTXO 在账本状态里与 TxOutput 同构（金额 + 锁定高度）。
@dataclass(frozen=True, slots=True)
class UTXO:
    """未花费输出：金额与锁定高度。"""

    amount: int
    lock_height: int

    def __post_init__(self) -> None:
        _check_int(self.amount, "UTXO.amount", min_value=1)
        _check_int(self.lock_height, "UTXO.lock_height", min_value=0)


@dataclass(frozen=True, slots=True)
class Transaction:
    """按序交易。

    * ``inputs`` 为空即 **coinbase 交易**（铸币交易）；非空即为普通交易。
    * 同一交易内输入是否重复，属于协议语义，由引擎验证（可定位的
      ``duplicate-input`` 拒绝），不在构造时静默处理。
    """

    tx_id: str
    inputs: tuple[TxInput, ...]
    outputs: tuple[TxOutput, ...]

    def __post_init__(self) -> None:
        _check_str(self.tx_id, "Transaction.tx_id")
        raw_inputs = _check_seq(self.inputs, "Transaction.inputs")
        raw_outputs = _check_seq(self.outputs, "Transaction.outputs")
        inputs: list[TxInput] = []
        for i, item in enumerate(raw_inputs):
            if not isinstance(item, TxInput):
                raise InputContractError(
                    f"Transaction.inputs[{i}]: expected TxInput, "
                    f"got {type(item).__name__}"
                )
            inputs.append(item)
        outputs: list[TxOutput] = []
        for i, item in enumerate(raw_outputs):
            if not isinstance(item, TxOutput):
                raise InputContractError(
                    f"Transaction.outputs[{i}]: expected TxOutput, "
                    f"got {type(item).__name__}"
                )
            outputs.append(item)
        if len(outputs) == 0:
            raise InputContractError(
                f"Transaction {self.tx_id!r}: outputs must contain at least one "
                f"TxOutput"
            )
        object.__setattr__(self, "inputs", tuple(inputs))
        object.__setattr__(self, "outputs", tuple(outputs))

    @property
    def is_coinbase(self) -> bool:
        return len(self.inputs) == 0


@dataclass(frozen=True, slots=True)
class Block:
    """区块：合成唯一字符串 ID、父 ID（创世为 ``None``）、正整数 work、按序交易。

    不验证 PoW；``work`` 只参与累计工作量选择。
    """

    block_id: str
    parent_id: str | None
    work: int
    transactions: tuple[Transaction, ...]

    def __post_init__(self) -> None:
        _check_str(self.block_id, "Block.block_id")
        if self.parent_id is not None:
            _check_str(self.parent_id, "Block.parent_id")
        if self.parent_id == self.block_id:
            raise InputContractError(
                f"Block {self.block_id!r}: a block cannot be its own parent"
            )
        _check_int(self.work, "Block.work", min_value=1)
        raw_txs = _check_seq(self.transactions, "Block.transactions")
        txs: list[Transaction] = []
        for i, item in enumerate(raw_txs):
            if not isinstance(item, Transaction):
                raise InputContractError(
                    f"Block.transactions[{i}]: expected Transaction, "
                    f"got {type(item).__name__}"
                )
            txs.append(item)
        if len(txs) == 0:
            raise InputContractError(
                f"Block {self.block_id!r}: transactions must contain at least one "
                f"Transaction"
            )
        object.__setattr__(self, "transactions", tuple(txs))

    @property
    def coinbase(self) -> Transaction | None:
        cb = self.transactions[0] if self.transactions[0].is_coinbase else None
        return cb
