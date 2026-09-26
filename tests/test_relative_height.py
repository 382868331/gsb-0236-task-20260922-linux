"""相对高度要求（min_relative_height）测试。

覆盖：恰好到期、差一块（错误指出哪个输入、还差几块）、同块链式交易（零要求）、
重组后创建高度随新链恢复（年龄缩短）、失败区块不留下部分消费、旧交易行为不变，
以及与独立参考 replay_heights 的创建高度交叉比对。
"""

import unittest

from reorg_ledger import (
    Block,
    BlockValidationError,
    InputContractError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
)
from tests.reference import ReferenceReject, replay_heights
from tests.test_differential import SUBSIDY, _Builder


def coinbase(tx_id: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def spend(tx_id: str, prev: tuple[str, int], amount_out: int,
          req: int = 0) -> Transaction:
    return Transaction(
        tx_id,
        (TxInput(prev[0], prev[1], req),),
        (TxOutput(amount_out, 0),),
    )


def block(block_id: str, parent: str | None, work: int,
          txs: tuple[Transaction, ...]) -> Block:
    return Block(block_id, parent, work, txs)


def fresh_ledger() -> ReorgLedger:
    """创世（高度 1）：补贴 50，锁定到高度 1（高度 2 起可花）。"""
    ledger = ReorgLedger()
    ledger.submit(block("G", None, 10, (coinbase("gcb", 50, lock=1),)))
    return ledger


class RelativeHeightContractTests(unittest.TestCase):
    def test_default_is_zero_and_old_construction_unchanged(self) -> None:
        tx_in = TxInput("t", 0)
        self.assertEqual(tx_in.min_relative_height, 0)
        self.assertEqual(TxInput("t", 0, 0), tx_in)

    def test_negative_bool_float_rejected(self) -> None:
        with self.assertRaises(InputContractError):
            TxInput("t", 0, -1)
        with self.assertRaises(InputContractError):
            TxInput("t", 0, True)  # type: ignore[arg-type]
        with self.assertRaises(InputContractError):
            TxInput("t", 0, 1.5)  # type: ignore[arg-type]


class MaturityBoundaryTests(unittest.TestCase):
    def test_exact_maturity_and_one_block_short(self) -> None:
        ledger = fresh_ledger()
        # 高度 2：花创世币，创建 a-tx:0（created_height=2）。
        ledger.submit(block(
            "A", "G", 1,
            (coinbase("cba", 50), spend("ta", ("gcb", 0), 50)),
        ))
        self.assertEqual(
            ledger.snapshot_utxos()[OutPoint("ta", 0)].created_height, 2
        )
        # 高度 3 以 req=2 花费：需要 2+2=4，差一块。
        short = block(
            "B", "A", 1,
            (coinbase("cbb", 50), spend("tb", ("ta", 0), 50, req=2)),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(short)
        exc = ctx.exception
        self.assertEqual(exc.reason, "relative-height-not-mature")
        self.assertEqual(exc.block_id, "B")
        self.assertEqual(exc.tx_id, "tb")
        self.assertEqual(str(exc.outpoint), "ta:0")
        self.assertEqual(exc.height, 3)
        # 错误说明指出还差几块。
        self.assertIn("1 block(s) short", exc.message)
        self.assertIn("created at height 2", exc.message)
        self.assertNotIn("B", ledger)
        self.assertEqual(ledger.tip_id, "A")
        # 空块推进到高度 3，高度 4 恰好到期（2+2=4）。
        ledger.submit(block("B2", "A", 1, (coinbase("cbb2", 50),)))
        ledger.submit(block(
            "C", "B2", 1,
            (coinbase("cbc", 50), spend("tc", ("ta", 0), 50, req=2)),
        ))
        self.assertEqual(ledger.tip_id, "C")
        self.assertNotIn(OutPoint("ta", 0), ledger.snapshot_utxos())

    def test_zero_requirement_allows_same_block_chain(self) -> None:
        ledger = fresh_ledger()
        # 同一块内：t1 花创世币，t2 立刻花 t1:0（req=0，按依赖顺序）。
        ledger.submit(block(
            "A", "G", 1,
            (coinbase("cba", 50),
             spend("t1", ("gcb", 0), 50),
             spend("t2", ("t1", 0), 50)),
        ))
        utxos = ledger.snapshot_utxos()
        self.assertNotIn(OutPoint("t1", 0), utxos)
        self.assertIn(OutPoint("t2", 0), utxos)
        self.assertEqual(utxos[OutPoint("t2", 0)].created_height, 2)

    def test_same_block_spend_with_positive_requirement_rejected(self) -> None:
        ledger = fresh_ledger()
        # 同块创建即花但 req=1：创建高度 == 候选高度，差一块。
        bad = block(
            "A", "G", 1,
            (coinbase("cba", 50),
             spend("t1", ("gcb", 0), 50),
             spend("t2", ("t1", 0), 50, req=1)),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(bad)
        self.assertEqual(ctx.exception.reason, "relative-height-not-mature")
        self.assertEqual(ctx.exception.tx_id, "t2")
        self.assertIn("1 block(s) short", ctx.exception.message)
        # 失败区块不留下部分消费：t1 花掉的 gcb:0 必须恢复。
        utxos = ledger.snapshot_utxos()
        self.assertIn(OutPoint("gcb", 0), utxos)
        self.assertNotIn(OutPoint("t1", 0), utxos)
        self.assertNotIn("A", ledger)

    def test_failed_block_leaves_no_partial_consumption(self) -> None:
        ledger = fresh_ledger()
        before = ledger.snapshot_utxos()
        # 第一笔合法花费 + 第二笔相对高度不足：整块拒绝，状态零变化。
        bad = block(
            "A", "G", 1,
            (coinbase("cba", 50),
             spend("t1", ("gcb", 0), 50),
             spend("t2", ("t1", 0), 50, req=3)),
        )
        with self.assertRaises(BlockValidationError):
            ledger.submit(bad)
        self.assertEqual(ledger.snapshot_utxos(), before)
        self.assertEqual(ledger.tip_id, "G")

    def test_reason_matches_independent_reference(self) -> None:
        ledger = fresh_ledger()
        a = block(
            "A", "G", 1,
            (coinbase("cba", 50), spend("ta", ("gcb", 0), 50)),
        )
        ledger.submit(a)
        bad = block(
            "B", "A", 1,
            (coinbase("cbb", 50), spend("tb", ("ta", 0), 50, req=5)),
        )
        with self.assertRaises(BlockValidationError) as ectx:
            ledger.submit(bad)
        g = ledger.get_block("G")
        with self.assertRaises(ReferenceReject) as rctx:
            replay_heights([g, a, bad])
        self.assertEqual(rctx.exception.reason, ectx.exception.reason)
        self.assertEqual(rctx.exception.reason, "relative-height-not-mature")


class ReorgCreatedHeightTests(unittest.TestCase):
    def test_created_height_restored_from_new_branch(self) -> None:
        """重组后 UTXO 创建高度必须按新链重放恢复，年龄随之缩短。

        主链 G,A,A2,A3：shared 在高度 2 创建，高度 4 以 req=2 花费合法。
        侧枝 G,B,B2：shared 在高度 3 才创建；重组后同一花费模板在高度 4
        变为差一块（3+2=5），创建高度不得沿用旧分支的 2。
        """
        ledger = ReorgLedger()
        g = block("G", None, 10, (coinbase("gcb", 50, lock=1),))
        a = block(
            "A", "G", 1,
            (coinbase("cba", 50), spend("shared", ("gcb", 0), 50)),
        )
        a2 = block("A2", "A", 1, (coinbase("cba2", 50),))
        a3 = block(
            "A3", "A2", 1,
            (coinbase("cba3", 50), spend("use", ("shared", 0), 50, req=2)),
        )
        for blk in (g, a, a2, a3):
            ledger.submit(blk)
        # 旧主链上：shared 创建于高度 2，高度 4 恰好 2+2，花费已生效。
        self.assertEqual(ledger.tip_id, "A3")
        self.assertNotIn(OutPoint("shared", 0), ledger.snapshot_utxos())

        # 侧枝：B 在高度 2 仅铸币，B2 在高度 3 才入账同一笔 shared。
        # 主链累计 work = 13；B 单独不反超，B2 到位后才重组。
        b = block("B", "G", 1, (coinbase("cbb", 50),))
        b2 = block(
            "B2", "B", 20,
            (coinbase("cbb2", 50), spend("shared", ("gcb", 0), 50)),
        )
        result = ledger.submit(b)
        self.assertFalse(result.reorganized)
        result2 = ledger.submit(b2)
        self.assertTrue(result2.reorganized)
        self.assertEqual(ledger.active_path, ("G", "B", "B2"))

        # 创建高度随新链恢复为 3，不是旧分支的 2。
        utxos = ledger.snapshot_utxos()
        self.assertEqual(utxos[OutPoint("shared", 0)].created_height, 3)
        # 与独立参考对新链的重放一致。
        expected = replay_heights([g, b, b2])
        self.assertEqual(
            {op: (u.amount, u.lock_height, u.created_height)
             for op, u in utxos.items()},
            dict(expected),
        )

        # 年龄缩短：高度 4 以 req=2 花费现在差一块（3+2=5）。
        too_young = block(
            "C", "B2", 1,
            (coinbase("cbc", 50), spend("uc", ("shared", 0), 50, req=2)),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(too_young)
        self.assertEqual(ctx.exception.reason, "relative-height-not-mature")
        self.assertIn("1 block(s) short", ctx.exception.message)
        # 推进到高度 5 后恰好到期。
        ledger.submit(block("C2", "B2", 1, (coinbase("cbc2", 50),)))
        ledger.submit(block(
            "D", "C2", 1,
            (coinbase("cbd", 50), spend("ud", ("shared", 0), 50, req=2)),
        ))
        self.assertNotIn(OutPoint("shared", 0), ledger.snapshot_utxos())


class DifferentialCreatedHeightTests(unittest.TestCase):
    def test_random_trees_created_height_matches_reference(self) -> None:
        for seed in range(5):
            with self.subTest(seed=seed):
                builder = _Builder(seed)
                tip = builder.genesis_id
                for _ in range(10):
                    tip = builder.extend(tip)
                for _ in range(15):
                    import random
                    base = random.Random(31 + seed + len(builder.blocks)).choice(
                        list(builder.blocks)
                    )
                    builder.extend(base)
                ledger = ReorgLedger(subsidy=SUBSIDY, max_blocks=100)
                by_depth: dict[int, list[str]] = {}
                for bid in builder.blocks:
                    by_depth.setdefault(builder.heights[bid], []).append(bid)
                for depth in sorted(by_depth):
                    for bid in by_depth[depth]:
                        ledger.submit(builder.blocks[bid])
                chain = [
                    builder.blocks[i]
                    for i in builder.chain_ids(ledger.tip_id)
                ]
                expected = replay_heights(chain, subsidy=SUBSIDY)
                actual = {
                    op: (u.amount, u.lock_height, u.created_height)
                    for op, u in ledger.snapshot_utxos().items()
                }
                self.assertEqual(actual, dict(expected))


if __name__ == "__main__":
    unittest.main()
