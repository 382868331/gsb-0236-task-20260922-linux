"""孤块缓冲专项测试。

覆盖：孙块先到后级联接入、父块 double spend 导致整棵后代 ancestor_invalid、
两条孤块链竞争、缓冲满按接收序淘汰最早整棵子树、同内容幂等/同 ID 异内容
拒绝、小规模全排列与父先到结果对照。
"""

import itertools
import unittest

from reorg_ledger import (
    AncestorInvalidError,
    Block,
    BlockConflictError,
    BlockValidationError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
)
from tests.reference import best_chain, replay


def coinbase(tx_id: str, amount: int = 50, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def spend(tx_id: str, prev: tuple[str, int], amount_out: int,
          lock: int = 0) -> Transaction:
    return Transaction(
        tx_id, (TxInput(prev[0], prev[1]),), (TxOutput(amount_out, lock),)
    )


def blk(block_id: str, parent: str | None, work: int,
        txs: tuple[Transaction, ...]) -> Block:
    return Block(block_id, parent, work, txs)


GENESIS = blk("G", None, 10, (coinbase("gcb", 50, lock=1),))


def utxo_view(ledger: ReorgLedger) -> dict[OutPoint, tuple[int, int]]:
    return {
        op: (u.amount, u.lock_height)
        for op, u in ledger.snapshot_utxos().items()
    }


class OrphanBufferingTests(unittest.TestCase):
    def test_grandchild_arrives_first_then_recursive_connect(self) -> None:
        ledger = ReorgLedger()
        grand = blk("C", "P", 5, (coinbase("cbc", 50),))
        buffered = ledger.submit(grand)
        self.assertTrue(buffered.buffered)
        self.assertEqual(ledger.buffered_orphan_ids(), ("C",))
        self.assertIsNone(ledger.tip_id)
        self.assertEqual(ledger.cumulative_work, 0)

        ledger.submit(GENESIS)
        parent = blk("P", "G", 5, (coinbase("cbp", 50),))
        ledger.submit(parent)
        self.assertTrue(ledger.is_verified("C"))
        self.assertEqual(ledger.active_path, ("G", "P", "C"))
        self.assertEqual(ledger.orphan_count(), 0)

    def test_buffered_grandchild_skips_utxo_checks_until_parent_ready(self) -> None:
        # 孙块花一个尚不存在的输出；缓冲期只做结构校验，不提前应用/证伪。
        ledger = ReorgLedger()
        grand = blk(
            "C", "P", 5,
            (coinbase("cbc", 50), spend("tc", ("future-coin", 0), 10)),
        )
        result = ledger.submit(grand)
        self.assertTrue(result.buffered)
        # 父块最终补上 future-coin，孙块接入成功。
        ledger.submit(GENESIS)
        parent = blk(
            "P", "G", 5,
            (coinbase("cbp", 50),
             Transaction("future-coin", (TxInput("gcb", 0),),
                         (TxOutput(10, 0),))),
        )
        ledger.submit(parent)
        self.assertEqual(ledger.active_path, ("G", "P", "C"))

    def test_structural_check_runs_before_buffering(self) -> None:
        ledger = ReorgLedger()
        bad = blk(
            "X", "unknown", 1,
            (spend("t", ("a", 0), 10), coinbase("cbx", 50)),  # coinbase 不在首位
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(bad)
        self.assertEqual(ctx.exception.reason, "coinbase-position")
        self.assertFalse(ledger.is_buffered_orphan("X"))


class AncestorInvalidTests(unittest.TestCase):
    def test_invalid_parent_marks_buffered_descendants(self) -> None:
        ledger = ReorgLedger()
        # 孤块链 P -> C -> GC，先全部缓冲。
        child = blk("C", "P", 5, (coinbase("cbc", 50),))
        grand = blk("GC", "C", 5,
                    (coinbase("cbgc", 50),
                     spend("tgc", ("gcb", 0), 50)))  # 单看交易“像”合法
        ledger.submit(child)
        ledger.submit(grand)
        ledger.submit(GENESIS)

        # P double spend：P 自己花 gcb，而 gcb 只有一枚；先放一个在 G 高度
        # 已花掉它的兄弟状态并非必须——直接让 P 构造非法：coinbase 超额。
        invalid_parent = blk("P", "G", 5, (coinbase("cbp", 999),))
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(invalid_parent)
        self.assertEqual(ctx.exception.block_id, "P")
        for descendant in ("C", "GC"):
            self.assertTrue(ledger.is_ancestor_invalid(descendant))
            self.assertEqual(ledger.invalid_ancestor_of(descendant), "P")
            self.assertFalse(ledger.is_buffered_orphan(descendant))
            self.assertFalse(ledger.is_verified(descendant))

        # 即使把 GC 的交易单独重放看似合法，也不能接入。
        with self.assertRaises(AncestorInvalidError) as actx:
            ledger.submit(grand)
        self.assertEqual(actx.exception.ancestor_id, "P")
        # 已验证主链不受影响。
        self.assertEqual(ledger.active_path, ("G",))

    def test_parent_double_spend_flavor_invalidates_chain(self) -> None:
        ledger = ReorgLedger()
        # 真实 double spend：A 已花掉 gcb；孤块链的父块 P 试图再花同一枚。
        ledger.submit(GENESIS)
        a = blk("A", "G", 1,
                (coinbase("cba", 50), spend("ta", ("gcb", 0), 50)))
        ledger.submit(a)

        orphan_child = blk("C", "P", 50, (coinbase("cbc", 50),))
        ledger.submit(orphan_child)
        p = blk("P", "A", 1,
                (coinbase("cbp", 50), spend("tp", ("gcb", 0), 50)))
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(p)
        self.assertEqual(ctx.exception.reason, "missing-input")
        self.assertTrue(ledger.is_ancestor_invalid("C"))
        self.assertEqual(ledger.invalid_ancestor_of("C"), "P")


class CompetingOrphanChainsTests(unittest.TestCase):
    def test_two_orphan_chains_compete_by_cumulative_work(self) -> None:
        ledger = ReorgLedger()
        # 两条都挂在未知父 G 下的孤块链：低 work 链 L1->L2，高 work 链 H1->H2。
        ledger.submit(blk("L2", "L1", 1, (coinbase("cbl2", 50),)))
        ledger.submit(blk("H2", "H1", 100, (coinbase("cbh2", 50),)))
        ledger.submit(blk("L1", "G", 1, (coinbase("cbl1", 50),)))
        ledger.submit(blk("H1", "G", 1, (coinbase("cbh1", 50),)))
        self.assertEqual(ledger.orphan_count(), 4)
        ledger.submit(GENESIS)  # 父到达：按 ID 顺序递归接入两条链
        for bid in ("L1", "L2", "H1", "H2"):
            self.assertTrue(ledger.is_verified(bid), bid)
        # 高 work 链胜出成为 tip，且 UTXO 等于独立参考的重放结果。
        self.assertEqual(ledger.tip_id, "H2")
        expected = replay([GENESIS, blk("H1", "G", 1, (coinbase("cbh1", 50),)),
                           blk("H2", "H1", 100, (coinbase("cbh2", 50),))])
        self.assertEqual(utxo_view(ledger), dict(expected))
        # 低 work 链仍保留为已验证侧枝。
        self.assertIn("L2", ledger)


class EvictionTests(unittest.TestCase):
    def test_buffer_eviction_drops_earliest_whole_subtree(self) -> None:
        ledger = ReorgLedger(max_orphans=3)
        x1 = blk("X1", "RX", 1, (coinbase("x1", 50),))
        x2 = blk("X2", "X1", 1, (coinbase("x2", 50),))  # 与 X1 同子树
        y1 = blk("Y1", "RY", 1, (coinbase("y1", 50),))
        z1 = blk("Z1", "RZ", 1, (coinbase("z1", 50),))

        r = ledger.submit(x1)
        self.assertEqual(r.evicted_orphan_ids, ())
        ledger.submit(x2)
        ledger.submit(y1)
        # 缓冲 [X1,X2,Y1] 已满；Z1 到达时淘汰接收序最早的整棵子树 X1+X2。
        r = ledger.submit(z1)
        self.assertEqual(r.evicted_orphan_ids, ("X1", "X2"))
        self.assertEqual(ledger.buffered_orphan_ids(), ("Y1", "Z1"))
        for gone in ("X1", "X2"):
            self.assertFalse(ledger.is_buffered_orphan(gone))

        # 被淘汰子树的根父块 RX 后来到达并验证，也不会把它们接回来。
        ledger.submit(GENESIS)
        ledger.submit(blk("RX", "G", 1, (coinbase("rx", 50),)))
        self.assertNotIn("X1", ledger)
        # Y1 仍可正常接入；已验证主链/侧链始终未受淘汰影响。
        ledger.submit(blk("RY", "G", 1, (coinbase("ry", 50),)))
        self.assertTrue(ledger.is_verified("Y1"))
        # Y1 与 RY 都在高度 3、累计 work 同分；tip 取 ID 字典序较小者 Y1。
        self.assertEqual(ledger.tip_id, "Y1")

    def test_eviction_does_not_touch_verified_chains(self) -> None:
        ledger = ReorgLedger(max_orphans=1)
        ledger.submit(GENESIS)
        verified = blk("A", "G", 1, (coinbase("ca", 50),))
        ledger.submit(verified)
        ledger.submit(blk("O1", "U1", 1, (coinbase("o1", 50),)))
        r = ledger.submit(blk("O2", "U2", 1, (coinbase("o2", 50),)))
        self.assertEqual(r.evicted_orphan_ids, ("O1",))
        self.assertEqual(ledger.active_path, ("G", "A"))
        self.assertTrue(ledger.is_verified("A"))


class PermutationEquivalenceTests(unittest.TestCase):
    def _scenario_blocks(self) -> dict[str, Block]:
        g = GENESIS
        a = blk("A", "G", 2,
                (coinbase("ca", 60), spend("ta", ("gcb", 0), 40)))
        b = blk("B", "G", 3, (coinbase("cb", 50),))
        a1 = blk("A1", "A", 2, (coinbase("ca1", 50),))
        b1 = blk("B1", "B", 2, (coinbase("cb1", 50),))
        return {x.block_id: x for x in (g, a, b, a1, b1)}

    def test_all_small_permutations_match_parent_first_outcome(self) -> None:
        blocks = self._scenario_blocks()
        baseline = ReorgLedger()
        parent_first = ["G", "A", "A1", "B", "B1"]
        for bid in parent_first:
            baseline.submit(blocks[bid])
        expected_tip = baseline.tip_id
        expected_utxos = utxo_view(baseline)

        # 独立参考独立选出的 tip 交叉核对。
        work_of = {bid: blocks[bid].work for bid in blocks}
        ref_chain = best_chain(blocks, work_of)
        self.assertEqual(ref_chain[-1], expected_tip)

        for perm in itertools.permutations(blocks):
            ledger = ReorgLedger()
            for bid in perm:
                ledger.submit(blocks[bid])
            self.assertEqual(ledger.tip_id, expected_tip, perm)
            self.assertEqual(utxo_view(ledger), expected_utxos, perm)
            ref_utxos = dict(replay([blocks[i] for i in ledger.active_path]))
            self.assertEqual(utxo_view(ledger), ref_utxos, perm)
            self.assertEqual(ledger.orphan_count(), 0, perm)

    def test_orphan_chain_winning_reorg_is_atomic(self) -> None:
        # 高 work 侧枝 B->B2 全部以后代先到方式缓冲；G 到达时按 ID 顺序
        # 递归级联验证并选出 tip，之后低 work 主链块到达不再改变 tip；
        # tip 处 UTXO 与独立参考从创世重放完全一致（重组原子性）。
        g = GENESIS
        a = blk("A", "G", 2,
                (coinbase("ca", 60), spend("ta", ("gcb", 0), 40)))
        b = blk("B", "G", 50,
                (coinbase("cb", 50), spend("tb", ("gcb", 0), 45)))
        b2 = blk("B2", "B", 50,
                 (coinbase("cb2", 50), spend("tb2", ("tb", 0), 45)))

        ledger = ReorgLedger()
        ledger.submit(b2)   # 孙块先到
        ledger.submit(b)    # 父也未知，整条侧枝缓冲
        self.assertEqual(ledger.orphan_count(), 2)
        ledger.submit(g)    # G 到达：按 ID 顺序递归接入 B、B2
        self.assertEqual(ledger.tip_id, "B2")
        self.assertEqual(ledger.orphan_count(), 0)
        ledger.submit(a)    # 低 work 主链块成为侧枝，不改变 tip
        self.assertEqual(ledger.tip_id, "B2")
        self.assertTrue(ledger.is_verified("A"))
        self.assertEqual(utxo_view(ledger), dict(replay([g, b, b2])))
        self.assertNotIn(OutPoint("ta", 0), ledger.snapshot_utxos())
        self.assertIn(OutPoint("tb2", 0), ledger.snapshot_utxos())


if __name__ == "__main__":
    unittest.main()
