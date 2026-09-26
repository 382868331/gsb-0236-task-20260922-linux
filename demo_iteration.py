#!/usr/bin/env python3
"""固定输入演示（本轮迭代）：交易输入的非负相对高度要求。

真实计算、无 sleep、无预录，演示：

1. 正常功能
   * 相对高度要求恰好到期时可花费；
   * 同一块内零要求输入按依赖顺序链式消费新输出；
   * 重组后 UTXO 创建高度随新链恢复（年龄缩短），不沿用旧分支高度。
2. 边界情况：差一块时花费被实际拒绝，错误指出哪个输入、还差几块，
   且失败区块不留下部分消费。
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


def coinbase(tx_id: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def main() -> None:
    started = time.perf_counter()
    ledger = ReorgLedger(subsidy=50, max_blocks=100)

    print("=== 1. 相对高度要求：恰好到期可花费，差一块被拒绝 ===")
    # 创世（高度 1）：补贴 50，锁定到高度 1。
    ledger.submit(Block("G", None, 10, (coinbase("g-cb", 50, lock=1),)))

    # 高度 2：花掉创世币，产出 out=50；同时铸补贴。
    ledger.submit(Block(
        "A", "G", 1,
        (coinbase("a-cb", 50),
         Transaction("a-tx", (TxInput("g-cb", 0),), (TxOutput(50, 0),))),
    ))
    print(f"高度 2 入账 a-tx:0，创建高度 = "
          f"{ledger.snapshot_utxos()[OutPoint('a-tx', 0)].created_height}")

    # 高度 3：以相对要求 2 花费 a-tx:0 —— 创建于 2，3 < 2+2，差一块。
    short_block = Block(
        "B-short", "A", 1,
        (coinbase("bs-cb", 50),
         Transaction("bs-tx", (TxInput("a-tx", 0, 2),), (TxOutput(50, 0),))),
    )
    tip_before, utxos_before = ledger.tip_id, ledger.snapshot_utxos()
    try:
        ledger.submit(short_block)
    except BlockValidationError as exc:
        print(f"差一块被拒绝（BlockValidationError）:")
        print(f"  reason   = {exc.reason}")
        print(f"  定位     = block={exc.block_id}, tx={exc.tx_id}, "
              f"outpoint={exc.outpoint}, height={exc.height}")
        print(f"  说明     = {exc.message}")
    else:
        raise SystemExit("expected relative-height rejection did not happen")
    print(f"失败区块未留下部分消费: "
          f"{ledger.tip_id == tip_before and ledger.snapshot_utxos() == utxos_before}"
          f"（{'B-short' not in ledger and 'bs-cb' not in str(ledger.snapshot_utxos())}）")

    # 空块推进到高度 3，再在高度 4 以相对要求 2 花费：2+2=4，恰好到期。
    ledger.submit(Block("B", "A", 1, (coinbase("b-cb", 50),)))
    ledger.submit(Block(
        "C", "B", 1,
        (coinbase("c-cb", 50),
         Transaction("c-tx", (TxInput("a-tx", 0, 2),), (TxOutput(50, 0),))),
    ))
    print(f"恰好到期（创建 2 + 要求 2 = 高度 4）花费成功，tip={ledger.tip_id}")

    print()
    print("=== 2. 零要求：同一块内按依赖顺序链式消费新输出 ===")
    # 高度 5：d1 花 c-tx:0，d2 同块立刻花 d1:0（零要求允许同块链式）。
    ledger.submit(Block(
        "D", "C", 1,
        (coinbase("d-cb", 50),
         Transaction("d1", (TxInput("c-tx", 0),), (TxOutput(50, 0),)),
         Transaction("d2", (TxInput("d1", 0),), (TxOutput(50, 0),))),
    ))
    print(f"同块链式 d1 -> d2 成功，tip={ledger.tip_id}, "
          f"d2:0 在 UTXO 中: {OutPoint('d2', 0) in ledger.snapshot_utxos()}")

    print()
    print("=== 3. 重组：创建高度随新链恢复，年龄缩短 ===")
    # 侧枝从 B 分叉：B2 不含 c-tx，d2 的祖先 d1/c-tx 在侧枝上不存在；
    # 在侧枝高度 4 重新入账一笔同内容交易 c-tx2（创建高度变为 4）。
    ledger.submit(Block("B2", "B", 1, (coinbase("b2-cb", 50),)))
    # 主链 G,A,B,C,D 累计 work = 14；侧枝到 C2 累计 = 10+1+1+1+30 = 43。
    result = ledger.submit(Block(
        "C2", "B2", 30,
        (coinbase("c2-cb", 50),
         Transaction("c-tx2", (TxInput("a-tx", 0, 2),), (TxOutput(50, 0),))),
    ))
    print(f"重组发生: {result.reorganized}, 新活跃链={list(ledger.active_path)}")
    ledger.submit(Block(
        "D2", "C2", 1,
        (coinbase("d2-cb", 50),
         Transaction("d2-spend", (TxInput("c-tx2", 0, 1),), (TxOutput(50, 0),))),
    ))
    created = ledger.snapshot_utxos()[OutPoint("d2-spend", 0)].created_height
    print(f"d2-spend:0 创建高度 = {created}（随新链重放得到，非旧分支残留）")
    # d2-spend 在重组后的新链上创建于高度 6；在高度 7 以相对要求 2 花费
    # 时 7 < 6+2，差一块 —— 内存里构造得出，不代表放入当前高度区块合法。
    too_young = Block(
        "E-bad", "D2", 1,
        (coinbase("e-cb", 50),
         Transaction("e-tx", (TxInput("d2-spend", 0, 2),), (TxOutput(50, 0),))),
    )
    try:
        ledger.submit(too_young)
    except BlockValidationError as exc:
        print(f"重组后年龄不足被拒绝: reason={exc.reason}")
        print(f"  说明 = {exc.message}")
    else:
        raise SystemExit("expected post-reorg rejection did not happen")
    ledger.submit(Block(
        "E", "D2", 1,
        (coinbase("e2-cb", 50),
         Transaction("e2-tx", (TxInput("d2-spend", 0, 1),), (TxOutput(50, 0),))),
    ))
    print(f"满足要求（创建 6 + 要求 1 = 高度 7）花费成功，tip={ledger.tip_id}")

    elapsed = time.perf_counter() - started
    print()
    print(f"演示完成，实际耗时 {elapsed:.3f} 秒（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
