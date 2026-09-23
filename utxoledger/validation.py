"""输入校验：严格类型/取值检查，错误一律可定位，绝不静默纠正。

约定：
* 整数参数必须是真实的 ``int`` 且不是 ``bool``（``bool`` 是 ``int`` 子类，
  按契约显式拒绝，防止 ``True`` 被当成 1）。
* 凡是数值输入，若调用方传入 ``float``，NaN / Infinity 一律拒绝；
  普通 float 同样拒绝（本模型金额只接受整数，不接受隐式取整）。
* 字符串必须是非空 ``str``。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from .errors import (
    DuplicateInputError,
    InvalidBlockError,
    InvalidTransactionError,
    ValidationError,
)
from .types import Block, Input, OutPoint, Output, Transaction

# 接口上限：任意一条链最多 100 个块（含创世块）。
MAX_CHAIN_BLOCKS = 100


def _reject_float(value: Any, *, what: str, location: str) -> None:
    """拒绝浮点数值输入；NaN / Infinity 单独给出明确错误码文案。"""

    if isinstance(value, float):
        if math.isnan(value):
            raise ValidationError(
                f"{what} 必须是整数，不能为 NaN",
                location=location, code="nan_value")
        if math.isinf(value):
            raise ValidationError(
                f"{what} 必须是整数，不能为 Infinity",
                location=location, code="infinity_value")
        raise ValidationError(
            f"{what} 必须是整数，收到 float（本模型不做隐式取整）: {value!r}",
            location=location, code="non_integer_number")


def require_int(value: Any, *, what: str, location: str,
                predicate: Callable[[int], bool] | None = None,
                predicate_msg: str = "") -> int:
    """要求真实 int（拒绝 bool / float / NaN / Infinity），可选取值约束。"""

    _reject_float(value, what=what, location=location)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(
            f"{what} 必须是 int，收到 {type(value).__name__}: {value!r}",
            location=location, code="type_error")
    if predicate is not None and not predicate(value):
        raise ValidationError(
            predicate_msg or f"{what} 取值非法: {value}",
            location=location, code="value_error")
    return value


def require_nonempty_str(value: Any, *, what: str,
                         location: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(
            f"{what} 必须是 str，收到 {type(value).__name__}: {value!r}",
            location=location, code="type_error")
    if value == "":
        raise ValidationError(
            f"{what} 不能为空字符串", location=location, code="empty_string")
    return value


# ---------------------------------------------------------------------------
# 交易 / 输出 / 输入校验
# ---------------------------------------------------------------------------


def validate_output(output: Any, *, location: str) -> Output:
    if not isinstance(output, Output):
        raise ValidationError(
            f"输出必须是 Output 实例，收到 {type(output).__name__}",
            location=location, code="type_error")
    require_int(output.value, what="output.value", location=location,
                predicate=lambda v: v >= 0,
                predicate_msg="output.value 必须为非负整数")
    require_int(output.lock_height, what="output.lock_height",
                location=location, predicate=lambda v: v >= 0,
                predicate_msg="output.lock_height 必须为非负整数")
    return output


def validate_outpoint(outpoint: Any, *, location: str) -> OutPoint:
    if not isinstance(outpoint, OutPoint):
        raise ValidationError(
            f"输入必须引用 OutPoint 实例，收到 {type(outpoint).__name__}",
            location=location, code="type_error")
    require_nonempty_str(
        outpoint.txid, what="outpoint.txid", location=location)
    require_int(outpoint.output_index, what="outpoint.output_index",
                location=location, predicate=lambda v: v >= 0,
                predicate_msg="outpoint.output_index 必须为非负整数")
    return outpoint


def validate_input(inp: Any, *, location: str) -> Input:
    if not isinstance(inp, Input):
        raise ValidationError(
            f"输入必须是 Input 实例，收到 {type(inp).__name__}",
            location=location, code="type_error")
    validate_outpoint(inp.outpoint, location=location)
    return inp


def validate_transaction(tx: Any, *, block_id: str, tx_index: int) -> Transaction:
    """结构性校验（不依赖任何链状态）。"""

    loc = f"block={block_id} tx_index={tx_index}"
    if not isinstance(tx, Transaction):
        raise InvalidTransactionError(
            f"交易必须是 Transaction 实例，收到 {type(tx).__name__}",
            location=loc)
    require_nonempty_str(tx.txid, what="tx.txid", location=loc)

    if not isinstance(tx.inputs, tuple) or not isinstance(tx.outputs, tuple):
        raise InvalidTransactionError(
            "inputs/outputs 必须为 tuple", location=loc)

    for i, inp in enumerate(tx.inputs):
        validate_input(inp, location=f"{loc} tx={tx.txid} input={i}")
    for i, out in enumerate(tx.outputs):
        validate_output(out, location=f"{loc} tx={tx.txid} output={i}")

    # 输入不可重复（同一笔交易内）。
    seen: set[OutPoint] = set()
    for i, inp in enumerate(tx.inputs):
        if inp.outpoint in seen:
            raise DuplicateInputError(
                f"交易 {tx.txid} 的输入 {i} 引用了重复的 OutPoint "
                f"{inp.outpoint.txid}:{inp.outpoint.output_index}",
                location=f"{loc} tx={tx.txid} input={i}")
        seen.add(inp.outpoint)

    if tx.is_coinbase:
        if tx.inputs:
            raise InvalidTransactionError(
                f"coinbase 交易 {tx.txid} 不能有输入",
                location=f"{loc} tx={tx.txid}")
        if not tx.outputs:
            raise InvalidTransactionError(
                f"coinbase 交易 {tx.txid} 至少要有一个输出",
                location=f"{loc} tx={tx.txid}")
    else:
        if not tx.inputs:
            raise InvalidTransactionError(
                f"普通交易 {tx.txid} 至少要有一个输入",
                location=f"{loc} tx={tx.txid}")
        if not tx.outputs:
            raise InvalidTransactionError(
                f"普通交易 {tx.txid} 至少要有一个输出",
                location=f"{loc} tx={tx.txid}")
    return tx


# ---------------------------------------------------------------------------
# 区块校验
# ---------------------------------------------------------------------------


def validate_block(block: Any) -> Block:
    """区块的静态结构校验（不依赖树状态；parent 存在性由 BlockTree 检查）。"""

    if not isinstance(block, Block):
        raise InvalidBlockError(
            f"区块必须是 Block 实例，收到 {type(block).__name__}")
    loc = f"block={block.block_id!r}"
    if not isinstance(block.block_id, str):
        raise InvalidBlockError(
            f"block.block_id 必须是 str，收到 "
            f"{type(block.block_id).__name__}: {block.block_id!r}",
            location=f"block={block.block_id!r}")
    if block.block_id == "":
        raise InvalidBlockError(
            "block.block_id 不能为空字符串", location="block=''")
    if block.parent is not None:
        require_nonempty_str(
            block.parent, what="block.parent",
            location=f"block={block.block_id}")
    require_int(
        block.work, what="block.work",
        location=f"block={block.block_id}",
        predicate=lambda v: v > 0,
        predicate_msg="block.work 必须是正整数")
    if not isinstance(block.transactions, tuple):
        raise InvalidBlockError(
            "transactions 必须为 tuple",
            location=f"block={block.block_id}")

    coinbase_seen = False
    txids_in_block: set[str] = set()
    for i, tx in enumerate(block.transactions):
        validate_transaction(tx, block_id=block.block_id, tx_index=i)
        if tx.txid in txids_in_block:
            raise InvalidTransactionError(
                f"txid {tx.txid} 在同一区块内重复",
                location=f"block={block.block_id} tx_index={i} "
                         f"tx={tx.txid}", code="txid_replay")
        txids_in_block.add(tx.txid)
        if tx.is_coinbase:
            if i != 0:
                raise InvalidTransactionError(
                    f"coinbase 交易 {tx.txid} 必须位于交易列表首位，"
                    f"实际位于 index {i}",
                    location=f"block={block.block_id} tx_index={i}")
            if coinbase_seen:
                raise InvalidTransactionError(
                    "一个区块至多包含一个 coinbase 交易",
                    location=f"block={block.block_id} tx_index={i}")
            coinbase_seen = True
    return block
