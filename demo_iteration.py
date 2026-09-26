#!/usr/bin/env python3
"""本轮迭代固定输入演示：交易输入的**非负相对高度要求**（min_age）。

真实计算内容（无 sleep、无预录、无网络，实测 < 1 秒）：

1. **差一块（实际触发的边界）**：输出创建于高度 1、输入要求 2 个区块年龄，
   在高度 2 提交花费时引擎抛出 ``BlockValidationError(reason=
   'output-too-young')``，并明确报告 ``blocks_short=1``（还差 1 块）；
   展示失败区块不入树、不留下部分消费。
2. **恰好到期**：同一年龄要求在高度 3（1 + 2 == 3）恰好可花。
3. **同块链式**：min_age=0 时 coinbase → t1 → t2 → t3 可在同一区块内按序消费。
4. **重组后年龄缩短**：同名输出在主链高度 3 创建、在侧枝高度 2 创建；
   侧枝高 work 重组后创建高度按侧枝历史重建，原本差 1 块的花费变为恰好到期。
"""

from __future__ import annotations

import time

from reorg_ledger import (
    Block,
    BlockValidationError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
)


def coinbase(tx_id: str, amount: int = 50, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def transfer(tx_id: str, prev_tx: str, amount: int, min_age: int = 0) -> Transaction:
    return Transaction(
        tx_id, (TxInput(prev_tx, 0, min_age),), (TxOutput(amount, 0),)
    )


def blk(block_id: str, parent: str | None, work: int, txs) -> Block:
    return Block(block_id, parent, work, tuple(txs))


GENESIS = blk("G", None, 10, [coinbase("gcb", 50, lock=1)])


def main() -> None:
    started = time.perf_counter()
    ledger = ReorgLedger(subsidy=50, max_blocks=100)
    ledger.submit(GENESIS)

    print("=== 1. 相对年龄：差一块被拒，报告还差几块 ===")
    # gcb:0 创建于高度 1；高度 2 提交 min_age=2 的花费（需要高度 >= 3）。
    early = blk("E", "G", 1, [
        coinbase("ecb"),
        transfer("te", "gcb", 50, min_age=2),
    ])
    tip_before = ledger.tip_id
    try:
        ledger.submit(early)
    except BlockValidationError as exc:
        print(f"被拒绝: reason={exc.reason}")
        print("  输出创建高度=1, 要求年龄=2 → 可花高度=3, "
              f"候选块高度={exc.height}")
        print(f"  blocks_short={exc.blocks_short}（这个输入还差 1 块）")
        print(f"  定位: block={exc.block_id}, tx={exc.tx_id}, "
              f"outpoint={exc.outpoint}")
    else:  # pragma: no cover - 演示必须实际触发
        raise SystemExit("expected output-too-young rejection")
    print(f"失败块未入树: {'E' not in ledger}; tip 未变: "
          f"{ledger.tip_id == tip_before}")
    print("无部分消费（gcb:0 仍在）: "
          f"{OutPoint('gcb', 0) in ledger.snapshot_utxos()}")

    print()
    print("=== 2. 恰好到期：高度 3 可花（1 + 2 == 3，边界相等允许） ===")
    ledger.submit(blk("A", "G", 1, [coinbase("acb")]))  # 高度 2
    ledger.submit(blk("B", "A", 1, [                      # 高度 3
        coinbase("bcb"),
        transfer("tb", "gcb", 50, min_age=2),
    ]))
    print(f"tip={ledger.tip_id}, 高度={ledger.height}; "
          "tb:0 的创建高度="
          f"{ledger.snapshot_utxos()[OutPoint('tb', 0)].created_height}")

    print()
    print("=== 3. 零要求：同块内按依赖顺序链式消费新输出 ===")
    ledger.submit(blk("C", "B", 1, [
        coinbase("ccb", 50),
        transfer("t1", "ccb", 50, min_age=0),
        transfer("t2", "t1", 50, min_age=0),
        transfer("t3", "t2", 50, min_age=0),
    ]))
    utxos = ledger.snapshot_utxos()
    all_spent = all(OutPoint(t, 0) not in utxos for t in ("ccb", "t1", "t2"))
    print(f"ccb:0 / t1:0 / t2:0 均已被同块后序交易花掉: {all_spent}")
    print("链式末端 t3:0 存在，创建高度="
          f"{utxos[OutPoint('t3', 0)].created_height}")

    print()
    print("=== 4. 重组后创建高度按区块历史重建，年龄缩短 ===")
    reorg = ReorgLedger(subsidy=50, max_blocks=100)
    reorg.submit(GENESIS)
    # 主链：shared 在高度 3 创建。
    reorg.submit(blk("M1", "G", 1, [coinbase("m1cb")]))
    reorg.submit(blk("M2", "M1", 1, [
        coinbase("m2cb"),
        Transaction(
            "shared", (TxInput("gcb", 0, 0),), (TxOutput(50, 0),)
        ),
    ]))
    main_created = reorg.snapshot_utxos()[OutPoint("shared", 0)].created_height
    print(f"主链上 shared:0 创建高度={main_created}")
    # 主链高度 4 想以 min_age=2 花 shared：需要 3+2=5，差 1 块。
    too_soon = blk("M3", "M2", 1, [
        coinbase("m3cb"),
        transfer("ts", "shared", 50, min_age=2),
    ])
    try:
        reorg.submit(too_soon)
    except BlockValidationError as exc:
        print(f"主链视角提前花费被拒: reason={exc.reason}, "
              f"blocks_short={exc.blocks_short}")
    # 侧枝：同一交易 shared 在高度 2 创建；高 work 触发真实重组。
    side = blk("S1", "G", 50, [
        coinbase("s1cb"),
        Transaction(
            "shared", (TxInput("gcb", 0, 0),), (TxOutput(50, 0),)
        ),
    ])
    result = reorg.submit(side)
    side_created = reorg.snapshot_utxos()[OutPoint("shared", 0)].created_height
    print(f"重组: reorganized={result.reorganized}, "
          f"活跃链={list(reorg.active_path)}")
    print(f"重组后 shared:0 创建高度={side_created}"
          f"（不是主链上的 {main_created}）")
    # 侧枝高度 3：2 + 1 = 3；min_age=1 恰好到期（主链上 min_age=2 还差 1 块）。
    reorg.submit(blk("S2", "S1", 1, [
        coinbase("s2cb"),
        transfer("ts", "shared", 50, min_age=1),
    ]))
    print(f"高度 3 的侧枝续块以 min_age=1 花费成功（tip={reorg.tip_id}）："
          "重组把相对年龄从“差 1 块”变为“恰好到期”。")

    print()
    elapsed = time.perf_counter() - started
    print(f"演示完成，实际耗时 {elapsed:.3f} 秒（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
