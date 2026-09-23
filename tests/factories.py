"""测试用区块 / 交易构造小工具（只依赖公开数据结构）。"""

from __future__ import annotations

import os
import sys

# 允许直接以脚本方式运行测试文件时也能找到被测库与参考实现。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_THIS = os.path.dirname(os.path.abspath(__file__))
if _THIS not in sys.path:
    sys.path.insert(0, _THIS)

from utxoledger import Block, Input, OutPoint, Output, Transaction  # noqa: E402


def coinbase(txid: str, value: int = 50, lock_height: int = 0) -> Transaction:
    return Transaction(
        txid=txid,
        inputs=(),
        outputs=(Output(value=value, lock_height=lock_height),),
        is_coinbase=True,
    )


def tx(txid: str, inputs, outputs) -> Transaction:
    """inputs: [(spend_txid, out_idx), ...]；outputs: [(value, lock), ...]。"""

    return Transaction(
        txid=txid,
        inputs=tuple(Input(OutPoint(t, i)) for t, i in inputs),
        outputs=tuple(Output(value=v, lock_height=lh) for v, lh in outputs),
    )


def block(block_id: str, parent: str | None, work: int,
          transactions=()) -> Block:
    return Block(
        block_id=block_id,
        parent=parent,
        work=work,
        transactions=tuple(transactions),
    )


def genesis(block_id: str = "G", txid: str = "cbG", value: int = 50,
            lock_height: int = 0, work: int = 1) -> Block:
    return block(block_id, None, work,
                 [coinbase(txid, value, lock_height)])
