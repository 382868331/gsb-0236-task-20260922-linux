"""独立参考实现：给定一条自创世起按序排列的区块列表，从空状态重放。

本模块刻意与 :mod:`reorg_ledger.engine` **不共享任何算法代码**：不复用引擎的
状态机、校验函数或选择逻辑，只把 ``Block`` / ``Transaction`` 等数据类当作
纯数据容器读取。测试用它独立算出期望值，再与被测引擎交叉比对。

参考自身同样区分两类结果：

* :class:`ReferenceReject` —— 链中某块被确定性证伪（带 reason 短码）；
* :class:`ReferenceLimit`  —— 超过调用方给定的块数预算（不判合法性）。

返回的 UTXO 以 ``{OutPoint: (amount, lock_height)}`` 纯字典表示，避免与引擎的
``UTXO`` 状态对象产生结构耦合。
"""

from __future__ import annotations

from reorg_ledger.models import Block, OutPoint, Transaction


class ReferenceError(Exception):
    """参考实现基类。"""


class ReferenceReject(ReferenceError):
    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class ReferenceLimit(ReferenceError):
    def __init__(self, depth: int, limit: int):
        super().__init__(f"chain length {depth} exceeds budget {limit}")
        self.depth = depth
        self.limit = limit


def replay(
    blocks: list[Block],
    subsidy: int = 50,
    *,
    max_blocks: int | None = None,
) -> dict[OutPoint, tuple[int, int]]:
    """从创世重放一条链，返回 tip 处 UTXO 集合。

    任何区块非法都抛 :class:`ReferenceReject`；``max_blocks`` 给定时超长抛
    :class:`ReferenceLimit`。实现为一次性顺序循环（每个区块在工作副本上
    结算，成功才整体替换），与引擎的“临时状态 + 原子提交”相互独立。
    """
    if not blocks:
        raise ReferenceError("reference replay requires at least a genesis block")

    utxos: dict[OutPoint, tuple[int, int]] = {}
    spent_history: set[str] = set()

    for height, block in enumerate(blocks, start=1):
        if max_blocks is not None and height > max_blocks:
            raise ReferenceLimit(height, max_blocks)
        if height == 1:
            if block.parent_id is not None:
                raise ReferenceReject(
                    "bad-genesis", "first block must have parent_id=None"
                )
        else:
            if block.parent_id != blocks[height - 2].block_id:
                raise ReferenceReject(
                    "broken-chain",
                    f"block {block.block_id!r} parent does not match previous "
                    f"block {blocks[height - 2].block_id!r}",
                )

        new_utxos = dict(utxos)
        fees = 0
        coinbase: Transaction | None = None

        for pos, tx in enumerate(block.transactions):
            if tx.tx_id in spent_history:
                raise ReferenceReject(
                    "duplicate-txid",
                    f"tx {tx.tx_id!r} already used earlier on this chain",
                )

            if not tx.inputs:
                if coinbase is not None:
                    raise ReferenceReject(
                        "multiple-coinbase",
                        f"second coinbase {tx.tx_id!r} in block "
                        f"{block.block_id!r}",
                    )
                if pos != 0:
                    raise ReferenceReject(
                        "coinbase-position",
                        f"coinbase {tx.tx_id!r} at index {pos}, must be 0",
                    )
                coinbase = tx
                # 按序入账：后续交易立即可见；超额在块末结算并整块回滚。
                for idx, out in enumerate(tx.outputs):
                    op = OutPoint(tx.tx_id, idx)
                    if op in new_utxos:
                        raise ReferenceReject(
                            "duplicate-txid", f"coinbase output collision at {op}"
                        )
                    new_utxos[op] = (out.amount, out.lock_height)
                spent_history.add(tx.tx_id)
                continue

            local_inputs: set[OutPoint] = set()
            in_sum = 0
            consumed: list[OutPoint] = []
            for tin in tx.inputs:
                op = OutPoint(tin.prev_tx, tin.prev_index)
                if op in local_inputs:
                    raise ReferenceReject(
                        "duplicate-input",
                        f"tx {tx.tx_id!r} spends {op} twice",
                    )
                local_inputs.add(op)
                coin = new_utxos.get(op)
                if coin is None:
                    raise ReferenceReject(
                        "missing-input",
                        f"tx {tx.tx_id!r} spends unknown/spent output {op}",
                    )
                amount, lock = coin
                if lock > height:
                    raise ReferenceReject(
                        "output-locked",
                        f"tx {tx.tx_id!r} spends {op} locked until {lock} "
                        f"at height {height}",
                    )
                in_sum += amount
                consumed.append(op)

            out_sum = 0
            for out in tx.outputs:
                out_sum += out.amount
            if out_sum > in_sum:
                raise ReferenceReject(
                    "output-exceeds-input",
                    f"tx {tx.tx_id!r}: out={out_sum} in={in_sum}",
                )
            fees += in_sum - out_sum

            for op in consumed:
                del new_utxos[op]
            for idx, out in enumerate(tx.outputs):
                op = OutPoint(tx.tx_id, idx)
                if op in new_utxos:
                    raise ReferenceReject(
                        "duplicate-txid", f"output point collision at {op}"
                    )
                new_utxos[op] = (out.amount, out.lock_height)
            spent_history.add(tx.tx_id)

        if coinbase is not None:
            # 按序入账早已发生，这里只做“补贴 + 手续费”铸币上限结算。
            minted = sum(o.amount for o in coinbase.outputs)
            allowance = subsidy + fees
            if minted > allowance:
                raise ReferenceReject(
                    "coinbase-overflow",
                    f"coinbase {coinbase.tx_id!r} mints {minted} > "
                    f"allowance {allowance}",
                )

        utxos = new_utxos

    return utxos


def best_chain(
    valid_blocks: dict[str, Block],
    work_of: dict[str, int],
) -> list[str]:
    """独立选链：在一组“已知合法”的区块中选累计 work 最大链，同分 ID 字典序。

    ``work_of`` 由测试从原始区块数据独立汇总（每块自身 work 沿父链累加），
    本函数只用父指针递归求和，不读取引擎任何状态。
    """
    if not valid_blocks:
        return []

    def chain_from(tip: str) -> list[str]:
        chain: list[str] = []
        cur: str | None = tip
        seen: set[str] = set()
        while cur is not None:
            if cur in seen or cur not in valid_blocks:
                return []  # 断链，不能形成自创世的完整链
            seen.add(cur)
            chain.append(cur)
            cur = valid_blocks[cur].parent_id
        chain.reverse()
        return chain

    def cumwork(chain: list[str]) -> int:
        total = 0
        for bid in chain:
            total += work_of[bid]
        return total

    candidates = [chain_from(tip) for tip in valid_blocks]
    complete = [c for c in candidates if c]
    # 排序键：累计 work 降序；tip（链尾）ID 升序。
    complete.sort(key=lambda c: (-cumwork(c), c[-1]))
    return complete[0]
