#!/usr/bin/env python3
"""固定输入演示：真实运行一个有界 UTXO 重组账本。

内容
----
1. 正常结果：构造主链 G→A 与更高累计 work 的侧枝 G→B→C，提交侧枝时发生
   **真实重组**，打印新 tip、撤销/激活路径、重组前后的 UTXO 差异。
2. 异常边界：在重组后的链上提交一个双花区块（花费已被侧枝花掉的输出），
   引擎实际抛出 ``BlockValidationError``（证明非法），并展示账本零部分变更；
   另外演示“预算耗尽”（100 块界，这里用小块界展示语义）与“非法输入”
   （bool 被当作整数）两个边界的实际异常类型。

所有数据在本文件内确定性构造，无网络、无 sleep、无预录输出。
"""

from __future__ import annotations

import time

from reorg_ledger import (
    Block,
    BlockValidationError,
    ChainLimitError,
    InputContractError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
)


def coinbase(tx_id: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def transfer(tx_id: str, prev: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (TxInput(prev, 0),), (TxOutput(amount, lock),))


def main() -> None:
    started = time.perf_counter()

    print("=== 1. 正常结果：侧枝累计 work 反超，触发真实重组 ===")
    ledger = ReorgLedger(subsidy=50, max_blocks=100)

    # 创世：50 枚补贴，锁定到高度 1（高度 1 即可花费，边界锁定）。
    genesis = Block("G", None, 10, (coinbase("g-cb", 50, lock=1),))
    ledger.submit(genesis)

    # 主链 A：花掉创世币 50 → 输出 40 + 手续费 10，coinbase 铸 60。
    block_a = Block(
        "A", "G", 2,
        (coinbase("a-cb", 60), transfer("a-tx", "g-cb", 40)),
    )
    ledger.submit(block_a)
    print(f"提交 A 后 tip={ledger.tip_id}, 活跃链={list(ledger.active_path)}, "
          f"累计 work={ledger.cumulative_work}")
    utxos_before = ledger.snapshot_utxos()

    # 侧枝 B（G 的另一子块）：work=20，以祖先状态 G 独立验证：花同一枚创世币。
    block_b = Block(
        "B", "G", 20,
        (coinbase("b-cb", 50), transfer("b-tx", "g-cb", 45)),
    )
    result_b = ledger.submit(block_b)
    print(f"提交 B：became_tip={result_b.became_tip}, "
          f"reorganized={result_b.reorganized}")
    assert result_b.reorg is not None
    print(f"  分叉点      : {result_b.reorg.fork_id}")
    print(f"  撤销(旧→新) : {list(result_b.reorg.deactivated)}")
    print(f"  激活(分叉→尖): {list(result_b.reorg.activated)}")

    # 侧枝继续延伸（同链推进，不再产生撤销）。
    block_c = Block(
        "C", "B", 20,
        (coinbase("c-cb", 50), transfer("c-tx", "b-tx", 45)),
    )
    result_c = ledger.submit(block_c)
    print(f"提交 C：tip={ledger.tip_id}, reorganized={result_c.reorganized}, "
          f"活跃链={list(ledger.active_path)}, 累计 work={ledger.cumulative_work}")

    utxos_after = ledger.snapshot_utxos()
    removed = sorted(map(str, utxos_before.keys() - utxos_after.keys()))
    created = sorted(map(str, utxos_after.keys() - utxos_before.keys()))
    print(f"重组撤销的 UTXO: {removed}")
    print(f"重组新增的 UTXO: {created}")
    print(f"当前 UTXO 数量 : {len(utxos_after)}")
    print(f"a-tx:0（旧链独有）是否已消失: "
          f"{OutPoint('a-tx', 0) not in utxos_after}")
    print(f"c-tx:0（新链独有）是否存在  : "
          f"{OutPoint('c-tx', 0) in utxos_after}")

    print()
    print("=== 2. 实际触发的拒绝/异常边界 ===")

    # 边界 A：双花。a-tx 的币属于旧链，在当前链上不存在；即便直接构造一个
    # 花费 g-cb:0 的区块，该输出在新链状态中也早已被 b-tx 花掉。
    double_spend = Block(
        "D", "C", 1,
        (coinbase("d-cb", 50), transfer("d-tx", "g-cb", 50)),
    )
    tip_before = ledger.tip_id
    path_before = ledger.active_path
    try:
        ledger.submit(double_spend)
    except BlockValidationError as exc:
        print(f"双花区块被拒绝（BlockValidationError）:")
        print(f"  reason   = {exc.reason}")
        print(f"  定位     = block={exc.block_id}, tx={exc.tx_id}, "
              f"height={exc.height}, outpoint={exc.outpoint}")
        print(f"  说明     = {exc.message}")
    else:  # pragma: no cover - 演示路径必须实际触发
        raise SystemExit("expected double-spend rejection did not happen")
    print(f"拒绝后 tip 未变化: {ledger.tip_id == tip_before} "
          f"({ledger.tip_id}), 活跃链未变化: "
          f"{ledger.active_path == path_before}")
    print(f"被拒绝块未入树: {'D' not in ledger}")

    # 边界 B：有界预算耗尽（与“非法”严格区分的另一类异常）。
    small = ReorgLedger(subsidy=50, max_blocks=2)
    small.submit(Block("g0", None, 1, (coinbase("g0-cb", 50, lock=1),)))
    small.submit(Block("g1", "g0", 1, (coinbase("g1-cb", 50),)))
    try:
        small.submit(Block("g2", "g1", 1, (coinbase("g2-cb", 50),)))
    except ChainLimitError as exc:
        print(f"超长链被拒绝（ChainLimitError，预算耗尽≠非法）: {exc}")

    # 边界 C：非法输入契约——bool 不得作为整数，浮点 NaN 单独报错。
    try:
        TxOutput(True, 0)  # type: ignore[arg-type]
    except InputContractError as exc:
        print(f"契约错误（bool 拒绝当整数）: {exc}")

    elapsed = time.perf_counter() - started
    print()
    print(f"演示完成，实际耗时 {elapsed:.3f} 秒（无 sleep、无预录结果）。")


if __name__ == "__main__":
    main()
