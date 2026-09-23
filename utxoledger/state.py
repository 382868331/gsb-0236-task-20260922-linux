"""UTXO 状态转移引擎：在给定祖先状态上执行一个区块，并记录撤销日志。

执行语义（公开契约的一部分）
============================

* 区块在链上高度 ``height`` 执行（创世高度 0）。花费输入时要求
  ``height >= output.lock_height``。
* 普通交易按列表顺序依次执行，前一笔交易创建的输出可被同块后序交易花费
  （仍受锁定高度约束）；因此同块内二次花费同一 OutPoint 必然失败
  （第一次花费后该 OutPoint 已离开 UTXO 集合）。
* 交易输入必须在执行起点的 UTXO 集合中存在；输出和 <= 输入和，
  差额即手续费。
* coinbase 交易必须位于交易列表首位，但其金额上限取决于整块普通交易的
  手续费总和，因此执行上采用“先顺序执行普通交易并累计手续费，再校验
  coinbase 额度、最后落账 coinbase 输出”的两段式处理。由此 coinbase
  输出不能在同块内被花费（与主流链的成熟度约束方向一致，属于刻意设计，
  见 README“设计取舍”）。
* txid 在同一条链上唯一：曾出现过的 txid 不得重放。

所有函数直接修改传入的 ``state`` 字典 / ``used_txids`` 集合；调用方负责
在临时副本上执行，失败时丢弃副本，从而保证不留下部分变更。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    CoinbaseExcessError,
    InvalidTransactionError,
    LockHeightError,
    MissingOutPointError,
    ValueBalanceError,
)
from .types import Block, OutPoint, Output, Transaction


@dataclass(frozen=True, slots=True)
class Journal:
    """区块执行的撤销日志。

    * ``added``：本块按执行顺序新建的 OutPoint；撤销时逆序弹出。
    * ``removed``：本块花费掉的 OutPoint -> 原 Output；撤销时原样恢复。
    """

    added: tuple[OutPoint, ...]
    removed: tuple[tuple[OutPoint, Output], ...]


def _loc(block: Block, tx: Transaction, extra: str = "") -> str:
    s = f"block={block.block_id} tx={tx.txid}"
    return f"{s} {extra}" if extra else s


def apply_block(state: dict[OutPoint, Output],
                used_txids: set[str],
                block: Block,
                height: int,
                subsidy: int) -> Journal:
    """在 ``state`` 上执行 ``block``（就地修改），返回撤销日志。

    任何契约违例都会抛出 :class:`~utxoledger.errors.ValidationError` 子类，
    此时 ``state`` / ``used_txids`` 可能已被部分修改——调用方必须在
    临时副本上调用本函数，失败时丢弃副本。
    """

    added: list[OutPoint] = []
    removed: list[tuple[OutPoint, Output]] = []
    fees = 0

    coinbase = block.transactions[0] if (
        block.transactions and block.transactions[0].is_coinbase) else None

    # --- 第一段：按序列表顺序执行普通交易 ---
    for tx_index, tx in enumerate(block.transactions):
        if tx.is_coinbase:
            continue  # 第二段统一处理

        if tx.txid in used_txids:
            raise InvalidTransactionError(
                f"txid {tx.txid} 在该链上已出现过，禁止重放",
                location=_loc(block, tx), code="txid_replay")

        input_sum = 0
        for in_index, inp in enumerate(tx.inputs):
            op = inp.outpoint
            prior = state.get(op)
            if prior is None:
                raise MissingOutPointError(
                    f"输入引用的 OutPoint {op.txid}:{op.output_index} "
                    "在当前 UTXO 集合中不存在（未曾创建或已被花费）",
                    location=_loc(block, tx, f"input={in_index}"))
            if height < prior.lock_height:
                raise LockHeightError(
                    f"OutPoint {op.txid}:{op.output_index} 锁定至高度 "
                    f"{prior.lock_height}，当前高度 {height} 不可花费",
                    location=_loc(block, tx, f"input={in_index}"),
                    block_height=height, lock_height=prior.lock_height)
            input_sum += prior.value
            del state[op]
            removed.append((op, prior))

        output_sum = 0
        for out_index, out in enumerate(tx.outputs):
            output_sum += out.value
            op = OutPoint(tx.txid, out_index)
            state[op] = out
            added.append(op)

        if output_sum > input_sum:
            raise ValueBalanceError(
                f"交易 {tx.txid} 输出和 {output_sum} 大于输入和 {input_sum}",
                location=_loc(block, tx),
                input_sum=input_sum, output_sum=output_sum)
        fees += input_sum - output_sum
        used_txids.add(tx.txid)

    # --- 第二段：coinbase 额度校验与落账（输出最后进入集合） ---
    if coinbase is not None:
        if coinbase.txid in used_txids:
            raise InvalidTransactionError(
                f"coinbase txid {coinbase.txid} 在该链上已出现过，禁止重放",
                location=_loc(block, coinbase), code="txid_replay")
        claimed = sum(out.value for out in coinbase.outputs)
        cap = subsidy + fees
        if claimed > cap:
            raise CoinbaseExcessError(
                f"coinbase 输出总额 {claimed} 超过上限 "
                f"补贴 {subsidy} + 手续费 {fees} = {cap}",
                location=_loc(block, coinbase),
                claimed=claimed, cap=cap)
        for out_index, out in enumerate(coinbase.outputs):
            op = OutPoint(coinbase.txid, out_index)
            state[op] = out
            added.append(op)
        used_txids.add(coinbase.txid)

    return Journal(added=tuple(added), removed=tuple(removed))


def undo_block(state: dict[OutPoint, Output],
               used_txids: set[str],
               block: Block,
               journal: Journal) -> None:
    """按 :class:`Journal` 精确撤销一个区块（就地修改）。

    用于重组的“撤销旧链”阶段。本函数只操作由 :func:`apply_block`
    产生的日志，正常使用下不会失败。
    """

    for op in reversed(journal.added):
        del state[op]
    for op, prior in journal.removed:
        state[op] = prior
    for tx in block.transactions:
        used_txids.discard(tx.txid)
