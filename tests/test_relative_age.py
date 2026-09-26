"""相对年龄（min_age）专项测试。

覆盖题目要求的四个核心场景：

1. **恰好到期**：输出创建于高度 c、要求 min_age=m，在高度 c+m 恰好可花；
2. **差一块**：在高度 c+m-1 被拒，错误明确指出还差 1 块，整块回滚无部分消费；
3. **同块链式**：min_age=0 允许同块按依赖顺序链式消费新输出；
4. **重组后年龄缩短**：同一输出 ID 在侧枝更早高度被创建，重组后创建高度按
   区块历史独立重建，年龄随之缩短。

另含：内存里合法不代表放入当前高度合法（提交到不同高度的祖先下被拒）、
无 min_age 的旧两/三参数构造保持行为、UTXO 创建高度独立参考交叉校验。
"""

from __future__ import annotations

import unittest

from reorg_ledger import (
    Block,
    BlockValidationError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
    UTXO,
)
from tests.reference import ReferenceReject, replay


def coinbase(tx_id: str, amount: int = 50, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def spend(tx_id: str, prev_tx: str, amount_out: int, *,
          min_age: int = 0, prev_index: int = 0,
          lock: int = 0) -> Transaction:
    return Transaction(
        tx_id,
        (TxInput(prev_tx, prev_index, min_age),),
        (TxOutput(amount_out, lock),),
    )


def blk(block_id: str, parent: str | None, work: int,
        txs: tuple[Transaction, ...]) -> Block:
    return Block(block_id, parent, work, txs)


GENESIS = blk("G", None, 10, (coinbase("gcb", 50, lock=1),))


def _utxo_triples(ledger: ReorgLedger) -> dict[OutPoint, tuple[int, int, int]]:
    return {
        op: (u.amount, u.lock_height, u.created_height)
        for op, u in ledger.snapshot_utxos().items()
    }


class RelativeAgeTests(unittest.TestCase):
    def test_exactly_mature_is_spendable(self) -> None:
        # gcb:0 创建于高度 1，min_age=2 → 高度 3 恰好可花。
        ledger = ReorgLedger()
        ledger.submit(GENESIS)
        ledger.submit(blk("A", "G", 1, (coinbase("cba"),)))  # 高度 2
        mature = blk(
            "B", "A", 1,  # 高度 3：1 + 2 = 3，恰好到期
            (coinbase("cbc"), spend("tc", "gcb", 50, min_age=2)),
        )
        ledger.submit(mature)
        self.assertEqual(ledger.tip_id, "B")
        self.assertEqual(
            ledger.snapshot_utxos()[OutPoint("tc", 0)].created_height, 3
        )
        chain = [GENESIS] + [ledger.get_block(b) for b in ("A", "B")]
        self.assertEqual(_utxo_triples(ledger), dict(replay(chain)))

    def test_one_block_short_reports_blocks_short_and_rolls_back(self) -> None:
        # 创建于高度 1、min_age=2：高度 2 还差 1 块；整块拒绝、零部分消费。
        ledger = ReorgLedger()
        ledger.submit(GENESIS)
        early = blk(
            "A", "G", 1,
            # 多笔交易：t-ok 先合法花掉 gcb:0；t-young 再花本块 coinbase
            # （创建于高度 2，min_age=1，高度 2 差 1 块）→ 整块回滚。
            (coinbase("cba", 50),
             Transaction("t-ok", (TxInput("gcb", 0, 0),),
                         (TxOutput(49, 0),)),
             Transaction("t-young", (TxInput("cba", 0, 1),),
                         (TxOutput(1, 0),))),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(early)
        self.assertEqual(ctx.exception.reason, "output-too-young")
        self.assertEqual(ctx.exception.blocks_short, 1)
        self.assertEqual(ctx.exception.height, 2)
        self.assertEqual(ctx.exception.tx_id, "t-young")
        self.assertEqual(str(ctx.exception.outpoint), "cba:0")
        self.assertNotIn("A", ledger)
        self.assertEqual(ledger.tip_id, "G")
        utxos = ledger.snapshot_utxos()
        self.assertIn(OutPoint("gcb", 0), utxos)
        self.assertNotIn(OutPoint("t-ok", 0), utxos)
        self.assertNotIn(OutPoint("cba", 0), utxos)
        with self.assertRaises(ReferenceReject) as rctx:
            replay([GENESIS, early])
        self.assertEqual(rctx.exception.reason, "output-too-young")

        # 差多块时报告数量：min_age=4 在高度 2 差 3 块。
        far = blk(
            "A2", "G", 1,
            (coinbase("cba2"), spend("t2", "gcb", 50, min_age=4)),
        )
        with self.assertRaises(BlockValidationError) as ctx2:
            ledger.submit(far)
        self.assertEqual(ctx2.exception.blocks_short, 3)
        self.assertEqual(ctx2.exception.height, 2)

    def test_zero_age_allows_same_block_dependency_chain(self) -> None:
        # coinbase → ta → tb → tc 在同一区块内按序消费，全部 min_age=0。
        ledger = ReorgLedger()
        ledger.submit(GENESIS)
        chained = blk(
            "A", "G", 1,
            (
                coinbase("fresh", 50),
                spend("ta", "fresh", 50, min_age=0),
                spend("tb", "ta", 50, min_age=0),
                spend("tc", "tb", 50, min_age=0),
            ),
        )
        ledger.submit(chained)
        utxos = ledger.snapshot_utxos()
        for gone in ("fresh", "ta", "tb"):
            self.assertNotIn(OutPoint(gone, 0), utxos)
        self.assertIn(OutPoint("tc", 0), utxos)
        self.assertEqual(utxos[OutPoint("tc", 0)].created_height, 2)
        self.assertEqual(
            _utxo_triples(ledger), dict(replay([GENESIS, chained]))
        )

        # min_age=1 的同块新输出不可立刻花（仅 0 允许同块）。
        ledger2 = ReorgLedger()
        ledger2.submit(GENESIS)
        bad_chain = blk(
            "A", "G", 1,
            (
                coinbase("fresh2", 50),
                spend("ta2", "fresh2", 50, min_age=0),
                spend("tb2", "ta2", 50, min_age=1),
            ),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger2.submit(bad_chain)
        self.assertEqual(ctx.exception.reason, "output-too-young")
        self.assertEqual(ctx.exception.blocks_short, 1)
        self.assertNotIn("A", ledger2)

    def test_created_height_rebuilt_on_reorg_and_age_shortens(self) -> None:
        # 主链 G→M1→M2：在高度 3 创建同名输出 shared:0。
        ledger = ReorgLedger()
        ledger.submit(GENESIS)
        m1 = blk("M1", "G", 1, (coinbase("cbm1"),))
        m2 = blk(
            "M2", "M1", 1,
            (coinbase("cbm2"),
             Transaction("shared", (TxInput("gcb", 0, 0),),
                         (TxOutput(50, 0),))),
        )
        ledger.submit(m1)
        ledger.submit(m2)
        self.assertEqual(
            ledger.snapshot_utxos()[OutPoint("shared", 0)].created_height, 3
        )
        # 主链视角：高度 4 花 shared:0 且要求 min_age=2 → 3+2=5，差 1 块。
        too_soon_main = blk(
            "M3", "M2", 1,
            (coinbase("cbm3"), spend("tm", "shared", 50, min_age=2)),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(too_soon_main)
        self.assertEqual(ctx.exception.reason, "output-too-young")
        self.assertEqual(ctx.exception.blocks_short, 1)

        # 侧枝 G→S1（高 work 触发重组）：同一交易 shared 出现在高度 2。
        s1 = blk(
            "S1", "G", 50,
            (coinbase("cbs1"),
             Transaction("shared", (TxInput("gcb", 0, 0),),
                         (TxOutput(50, 0),))),
        )
        result = ledger.submit(s1)
        self.assertTrue(result.reorganized)
        self.assertEqual(ledger.active_path, ("G", "S1"))
        # 创建高度必须按侧枝历史重建为 2，不能沿用主链上的 3。
        self.assertEqual(
            ledger.snapshot_utxos()[OutPoint("shared", 0)].created_height, 2
        )
        self.assertEqual(_utxo_triples(ledger), dict(replay([GENESIS, s1])))
        # 重组后年龄缩短：高度 3 的侧枝续块里 min_age=1（2+1=3）恰好可花。
        s2 = blk(
            "S2", "S1", 1,
            (coinbase("cbs2"), spend("ts", "shared", 50, min_age=1)),
        )
        ledger.submit(s2)
        self.assertEqual(ledger.active_path, ("G", "S1", "S2"))
        self.assertEqual(
            _utxo_triples(ledger), dict(replay([GENESIS, s1, s2]))
        )

    def test_memory_valid_block_may_be_invalid_at_current_height(self) -> None:
        # 同一交易挂到不同高度的祖先下：在高高度合法，在低高度非法；
        # “内存里/别处合法”不能让它在当前候选高度合法。
        ledger = ReorgLedger()
        ledger.submit(GENESIS)
        low = blk(
            "L", "G", 1,
            (coinbase("cbl"), spend("tx", "gcb", 50, min_age=3)),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(low)
        self.assertEqual(ctx.exception.reason, "output-too-young")
        self.assertEqual(ctx.exception.blocks_short, 2)
        # 延伸到高度 4（1+3=4）后同样的交易即合法。
        ledger.submit(blk("A", "G", 1, (coinbase("cba"),)))
        ledger.submit(blk("B", "A", 1, (coinbase("cbb"),)))
        ledger.submit(blk(
            "C", "B", 1,
            (coinbase("cbc"), spend("tx", "gcb", 50, min_age=3)),
        ))
        self.assertEqual(ledger.tip_id, "C")

    def test_legacy_construction_and_absent_min_age_unchanged(self) -> None:
        # 旧接口：TxInput 两参位置构造、UTXO 两参构造继续可用。
        legacy_input = TxInput("gcb", 0)
        self.assertEqual(legacy_input.min_age, 0)
        legacy_utxo = UTXO(50, 0)
        self.assertEqual(legacy_utxo.created_height, 0)
        # 未设置相对锁（默认 0）的交易行为与旧版本一致：跨块与同块均可花。
        ledger = ReorgLedger()
        ledger.submit(GENESIS)
        ledger.submit(blk(
            "A", "G", 1,
            (coinbase("cba"),
             Transaction("ta", (TxInput("gcb", 0),), (TxOutput(40, 0),))),
        ))
        ledger.submit(blk(
            "B", "A", 1,
            (coinbase("cbb"),
             Transaction("tb", (TxInput("ta", 0),), (TxOutput(40, 0),))),
        ))
        chain = [GENESIS, ledger.get_block("A"), ledger.get_block("B")]
        self.assertEqual(_utxo_triples(ledger), dict(replay(chain)))


if __name__ == "__main__":
    unittest.main()
