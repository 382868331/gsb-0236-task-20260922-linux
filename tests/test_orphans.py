"""孤块缓冲（orphan buffer）测试。

覆盖题面要求的四类场景 + 排列差分：

* 孙块先到：父块未知时仅结构校验后暂存，父块到达并验证成功后按块 ID
  字典序递归接入后代；
* 父块 double spend 被证伪：其缓冲后代标 ``ancestor_invalid``，即使后代
  自身交易单独看起来合法也绝不接入；
* 两个孤块链竞争：暂存期间不影响 tip / 累计 work，接入后高 work 链胜出；
* 缓冲淘汰：缓冲满时按显式接收序淘汰最早整棵待验证子树并返回淘汰 ID，
  已验证的主链与侧链不受影响。

另有幂等/冲突语义与“同一有效区块集全排列 vs 父先到”的最终 tip/UTXO 对照，
并在每次接收后用独立参考重放核对活跃链状态（重组原子性）。
"""

from __future__ import annotations

import itertools
import unittest

from reorg_ledger import (
    Block,
    BlockValidationError,
    ConflictingBlockError,
    DuplicateBlockError,
    OrphanBlockError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
)
from tests.reference import replay

SUBSIDY = 50


def coinbase(tx_id: str, amount: int = SUBSIDY, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def transfer(tx_id: str, prev: str, amount: int, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (TxInput(prev, 0),), (TxOutput(amount, lock),))


def block(bid: str, parent: str | None, work: int,
          txs: tuple[Transaction, ...]) -> Block:
    return Block(bid, parent, work, txs)


def genesis(work: int = 10, lock: int = 0) -> Block:
    return block("G", None, work, (coinbase("g-cb", SUBSIDY, lock),))


def utxo_plain(ledger: ReorgLedger) -> dict[OutPoint, tuple[int, int]]:
    return {op: (u.amount, u.lock_height)
            for op, u in ledger.snapshot_utxos().items()}


class OrphanBufferTests(unittest.TestCase):
    def test_grandchild_first_then_recursive_connect_in_id_order(self) -> None:
        ledger = ReorgLedger()
        ledger.receive(genesis())
        a = block("A", "G", 1, (coinbase("cb-a"),))
        b = block("B", "A", 1, (coinbase("cb-b"),))
        c = block("C", "B", 1, (coinbase("cb-c"),))

        r_c = ledger.receive(c)  # 孙块先到
        self.assertEqual(r_c.status, "buffered")
        self.assertEqual(ledger.orphan_status("C"), "pending")
        r_b = ledger.receive(b)
        self.assertEqual(r_b.status, "buffered")

        # 暂存期间：不计入累计 work 候选，不应用交易。
        self.assertEqual(ledger.tip_id, "G")
        self.assertEqual(ledger.cumulative_work, 10)
        self.assertNotIn(OutPoint("cb-b", 0), ledger.snapshot_utxos())
        self.assertNotIn("B", ledger)
        self.assertNotIn("C", ledger)

        r_a = ledger.receive(a)  # 父块到达并验证成功 → 递归接入
        self.assertEqual(r_a.status, "connected")
        self.assertEqual(r_a.connected, ("A", "B", "C"))  # 按块 ID 字典序
        self.assertEqual(ledger.active_path, ("G", "A", "B", "C"))
        self.assertEqual(ledger.tip_id, "C")
        self.assertEqual(ledger.pending_count, 0)
        self.assertIn(OutPoint("cb-c", 0), ledger.snapshot_utxos())

    def test_utxo_dependent_invalid_orphan_is_buffered_first(self) -> None:
        """依赖 UTXO 的非法（双花父块输出）在暂存时不判定，接入时才证伪。"""
        ledger = ReorgLedger()
        ledger.receive(genesis())
        bad = block(
            "BAD", "G", 1,
            (coinbase("cb-bad"),
             transfer("t1", "g-cb", 50),
             transfer("t2", "g-cb", 50)),  # 双花：依赖祖先状态才能判定
        )
        # 父在树中 → 直接接入验证，双花被证伪。
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.receive(bad)
        self.assertEqual(ctx.exception.reason, "missing-input")

        # 父未知时：同样的双花块只做结构校验，先暂存。
        ledger2 = ReorgLedger()
        orphan = block(
            "O", "unknown-parent", 1,
            (coinbase("cb-o"),
             transfer("o-t1", "someone", 10),
             transfer("o-t2", "someone", 10)),
        )
        r = ledger2.receive(orphan)
        self.assertEqual(r.status, "buffered")
        self.assertEqual(ledger2.orphan_status("O"), "pending")

    def test_structural_error_rejected_even_when_parent_unknown(self) -> None:
        ledger = ReorgLedger()
        bad_structure = block(
            "S", "unknown-parent", 1,
            (transfer("t", "whatever", 1), coinbase("cb-late")),  # coinbase 位置错
        )
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.receive(bad_structure)
        self.assertEqual(ctx.exception.reason, "coinbase-position")
        self.assertEqual(ledger.pending_count, 0)

    def test_parent_double_spend_marks_descendants_ancestor_invalid(self) -> None:
        ledger = ReorgLedger()
        ledger.receive(genesis())
        parent = block(
            "P", "G", 1,
            (coinbase("cb-p"),
             transfer("p-t1", "g-cb", 50),
             transfer("p-t2", "g-cb", 50)),  # 父块双花 → 证伪
        )
        child = block("PC", "P", 1, (coinbase("cb-pc"),))  # 自身完全合法
        grandchild = block("PGC", "PC", 1, (coinbase("cb-pgc"),))

        ledger.receive(grandchild)
        ledger.receive(child)
        self.assertEqual(ledger.pending_count, 2)

        with self.assertRaises(BlockValidationError):
            ledger.receive(parent)  # 父块被证伪
        self.assertEqual(ledger.orphan_status("PC"), "ancestor_invalid")
        self.assertEqual(ledger.orphan_status("PGC"), "ancestor_invalid")
        self.assertEqual(ledger.pending_count, 0)
        self.assertNotIn("PC", ledger)
        self.assertNotIn("PGC", ledger)

        # 后代即使单独合法也不能接入：重复接收幂等返回 ancestor_invalid。
        r = ledger.receive(child)
        self.assertEqual(r.status, "ancestor_invalid")
        self.assertNotIn("PC", ledger)
        self.assertEqual(ledger.tip_id, "G")

    def test_two_orphan_chains_compete_higher_work_wins(self) -> None:
        ledger = ReorgLedger()
        ledger.receive(genesis())  # work 10
        # 链 X：G→X1→X2，累计 10+1+1=12；链 Y：G→Y1→Y2，累计 10+5+5=20。
        x1 = block("X1", "G", 1, (coinbase("cb-x1"),))
        x2 = block("X2", "X1", 1, (coinbase("cb-x2"),))
        y1 = block("Y1", "G", 5, (coinbase("cb-y1"),))
        y2 = block("Y2", "Y1", 5, (coinbase("cb-y2"),))

        # 子块先到（暂存不影响选链），父块随后到达触发递归接入。
        for orphan in (x2, y2):
            r = ledger.receive(orphan)
            self.assertEqual(r.status, "buffered")
            self.assertEqual(ledger.tip_id, "G")
            self.assertEqual(ledger.cumulative_work, 10)
        ledger.receive(x1)
        ledger.receive(y1)

        self.assertEqual(ledger.tip_id, "Y2")
        self.assertEqual(ledger.cumulative_work, 20)
        self.assertEqual(ledger.active_path, ("G", "Y1", "Y2"))
        self.assertIn("X2", ledger)  # 侧链保留在树中
        self.assertFalse(ledger.is_active("X2"))

    def test_buffer_eviction_oldest_subtree_first(self) -> None:
        ledger = ReorgLedger(orphan_capacity=3)
        ledger.receive(genesis())
        side = block("SIDE", "G", 1, (coinbase("cb-side"),))
        ledger.receive(side)  # 已验证侧链：淘汰不得影响它

        r1 = block("R1", "unknown-1", 1, (coinbase("cb-r1"),))
        r1c = block("R1C", "R1", 1, (coinbase("cb-r1c"),))
        r2 = block("R2", "unknown-2", 1, (coinbase("cb-r2"),))
        ledger.receive(r1)   # seq 1（最早子树的根）
        ledger.receive(r1c)  # seq 2
        ledger.receive(r2)   # seq 3 → 缓冲满
        self.assertEqual(ledger.pending_count, 3)

        r_x = ledger.receive(block("X", "unknown-3", 1, (coinbase("cb-x"),)))
        self.assertEqual(r_x.status, "buffered")
        self.assertEqual(r_x.evicted, ("R1", "R1C"))  # 整棵最早子树
        self.assertEqual(ledger.pending_orphans(), ("R2", "X"))
        self.assertIsNone(ledger.orphan_status("R1"))

        # 已验证的主链与侧链不受影响（SIDE 累计 work 11 > 10，为当前 tip）。
        self.assertEqual(ledger.tip_id, "SIDE")
        self.assertIn("SIDE", ledger)
        self.assertEqual(ledger.active_path, ("G", "SIDE"))

        # 被淘汰的块可重新接收（视为全新孤块）。
        r_again = ledger.receive(r1c)
        self.assertEqual(r_again.status, "buffered")

    def test_idempotent_duplicate_and_conflicting_resubmit(self) -> None:
        ledger = ReorgLedger()
        g = genesis()
        ledger.receive(g)
        r = ledger.receive(g)  # 同 ID 同内容：幂等
        self.assertEqual(r.status, "duplicate")
        self.assertEqual(ledger.tip_id, "G")

        orphan = block("O", "missing", 1, (coinbase("cb-o"),))
        ledger.receive(orphan)
        r2 = ledger.receive(orphan)  # 缓冲中同内容：幂等
        self.assertEqual(r2.status, "duplicate")
        self.assertEqual(ledger.pending_count, 1)

        # 同 ID 异内容：拒绝（且兼容旧的 DuplicateBlockError 捕获）。
        other_g = block("G", None, 99, (coinbase("g-cb2"),))
        with self.assertRaises(ConflictingBlockError):
            ledger.receive(other_g)
        with self.assertRaises(DuplicateBlockError):
            ledger.receive(other_g)
        other_o = block("O", "missing", 2, (coinbase("cb-o2"),))
        with self.assertRaises(ConflictingBlockError):
            ledger.receive(other_o)

    def test_submit_keeps_legacy_orphan_and_duplicate_semantics(self) -> None:
        """原 submit 接口行为不变：父缺失抛 OrphanBlockError，重复抛错。"""
        ledger = ReorgLedger()
        ledger.receive(genesis())
        child = block("C", "missing-parent", 1, (coinbase("cb-c"),))
        with self.assertRaises(OrphanBlockError):
            ledger.submit(child)
        g2 = genesis()
        with self.assertRaises(DuplicateBlockError):
            ledger.submit(g2)


class OrphanPermutationTests(unittest.TestCase):
    """同一有效区块集按全部小规模排列输入，最终 tip/UTXO 与父先到一致。"""

    def _blocks(self) -> dict[str, Block]:
        # 主链 G→A→B（累计 10+5+5=20），侧枝 G→C→D（累计 10+6+6=22 胜出）。
        return {
            "G": genesis(work=10),
            "A": block("A", "G", 5, (coinbase("cb-a"),)),
            "B": block("B", "A", 5,
                       (coinbase("cb-b", 60), transfer("b-tx", "cb-a", 40))),
            "C": block("C", "G", 6, (coinbase("cb-c"),)),
            "D": block("D", "C", 6, (coinbase("cb-d"),)),
        }

    def test_all_permutations_converge_to_parent_first_result(self) -> None:
        blocks = self._blocks()
        parent_first = ["G", "A", "B", "C", "D"]

        expected_ledger = ReorgLedger()
        for bid in parent_first:
            expected_ledger.receive(blocks[bid])
        expected_tip = expected_ledger.tip_id
        expected_utxos = utxo_plain(expected_ledger)
        self.assertEqual(expected_tip, "D")  # 高 work 侧枝经重组胜出

        # 独立参考：父先到顺序下活跃链重放，交叉核对期望值本身。
        self.assertEqual(
            replay([blocks[bid] for bid in expected_ledger.active_path]),
            expected_utxos,
        )

        for perm in itertools.permutations(blocks):
            ledger = ReorgLedger()
            for bid in perm:
                ledger.receive(blocks[bid])
                # 重组原子性：每一步后活跃链状态都与独立参考重放一致。
                expected_now = (
                    replay([blocks[b] for b in ledger.active_path])
                    if ledger.active_path else {}
                )
                self.assertEqual(utxo_plain(ledger), expected_now,
                                 msg=f"permutation {perm} diverged after {bid}")
            self.assertEqual(ledger.tip_id, expected_tip,
                             msg=f"permutation {perm}")
            self.assertEqual(utxo_plain(ledger), expected_utxos,
                             msg=f"permutation {perm}")
            self.assertEqual(ledger.pending_count, 0)


if __name__ == "__main__":
    unittest.main()
