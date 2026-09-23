"""重组路径、原子性与“逐链从创世重放”独立参考对照测试。"""

from __future__ import annotations

import random
import unittest

from utxoledger import (
    BudgetExhaustedError,
    CoinbaseExcessError,
    LockHeightError,
    MissingOutPointError,
    NoSolutionError,
)
from utxoledger.chain import BlockTree
from factories import block, coinbase, genesis, tx
import reference as ref


def ref_utxo_view(tree: BlockTree):
    """用独立参考实现重放当前活动链，返回 UTXO 字典与有序链 id。"""

    chain_ids = tree.active_chain_ids()
    block_map = {nid: tree.get_block(nid) for nid in chain_ids}
    if not chain_ids:
        return {}
    utxos, _ = ref.replay(block_map, tree.tip, tree.subsidy)
    return utxos


def core_utxo_map(tree: BlockTree):
    return {(u.outpoint.txid, u.outpoint.output_index):
            ref.RefOut(u.value, u.lock_height) for u in tree.utxos()}


class ReorgPathTests(unittest.TestCase):
    def _base_tree(self):
        tree = BlockTree(subsidy=50)
        tree.add_block(genesis("G", "cbG", 50, 0))
        return tree

    def test_activation_and_tie_break(self):
        tree = self._base_tree()
        r1 = tree.add_block(block("A1", "G", 10, []))
        self.assertFalse(r1.is_reorg)
        self.assertEqual(r1.activated, ("A1",))
        # 同 work 同分：字典序 A1 < B1，A1 保持。
        tree.add_block(block("B1", "G", 10, []))
        self.assertEqual(tree.tip, "A1")
        # B2 累计 work 更大，触发真实重组。
        r3 = tree.add_block(block("B2", "B1", 1, []))
        self.assertTrue(r3.is_reorg)
        self.assertEqual(r3.deactivated, ("A1",))
        self.assertEqual(r3.activated, ("B1", "B2"))
        self.assertEqual(tree.tip, "B2")
        self.assertEqual(tree.active_chain_ids(), ("G", "B1", "B2"))

    def test_reorg_utxo_rollover(self):
        tree = self._base_tree()
        # A 链花费创世币给 a1。
        tree.add_block(block("A1", "G", 10, [
            tx("a1", [("cbG", 0)], [(50, 0)]),
        ]))
        self.assertIsNone(tree.utxo("cbG", 0))
        self.assertIsNotNone(tree.utxo("a1", 0))
        # B 侧枝同样合法地花费创世币给 b1，累计 work 更高。
        r = tree.add_block(block("B1", "G", 11, [
            tx("b1", [("cbG", 0)], [(50, 0)]),
        ]))
        self.assertTrue(r.is_reorg)
        self.assertEqual(r.deactivated, ("A1",))
        # 撤销 A1 后 cbG 恢复，再被 B1 花费。
        self.assertIsNone(tree.utxo("a1", 0))
        self.assertIsNone(tree.utxo("cbG", 0))
        self.assertEqual(tree.utxo("b1", 0).value, 50)

    def test_invalid_side_branch_rejected_atomic(self):
        tree = self._base_tree()
        tree.add_block(block("A1", "G", 10, [
            tx("a1", [("cbG", 0)], [(50, 0)]),
        ]))
        snapshot_nodes = len(tree)
        snapshot_tip = tree.tip
        # 侧枝 B1 想双花一个在祖先状态里存在的输出，且 work 更高；
        # 构造两个输入里第二个引用已花费的输出（跨链祖先状态验证）：
        bad = block("B1", "G", 100, [
            tx("x1", [("cbG", 0)], [(10, 0)]),
            tx("x2", [("cbG", 0)], [(10, 0)]),  # 同块二次花费
        ])
        with self.assertRaises(MissingOutPointError):
            tree.add_block(bad)
        # 原子性：块未入树，活动链不变，UTXO 不变。
        self.assertEqual(len(tree), snapshot_nodes)
        self.assertEqual(tree.tip, snapshot_tip)
        self.assertIsNotNone(tree.utxo("a1", 0))

    def test_invalid_chain_cannot_become_tip_even_with_more_work(self):
        tree = self._base_tree()
        tree.add_block(block("A1", "G", 1, []))
        # 高 work 侧枝：coinbase 超额，必须在祖先状态验证时被拒。
        bad = block("B1", "G", 1000, [coinbase("cbB", 51)])
        with self.assertRaises(CoinbaseExcessError):
            tree.add_block(bad)
        self.assertEqual(tree.tip, "A1")
        self.assertNotIn("B1", tree)

    def test_lock_height_respected_on_reorg(self):
        # 输出锁定高度跨重组仍然生效：
        # G 给的币锁到高度 2；高 work 侧枝在高度 1 就花 -> 拒绝。
        tree = BlockTree(subsidy=50)
        tree.add_block(genesis("G", "cbG", 50, lock_height=2))
        tree.add_block(block("A1", "G", 1, []))  # 不花钱
        bad = block("B1", "G", 100, [
            tx("b1", [("cbG", 0)], [(50, 0)]),
        ])
        with self.assertRaises(LockHeightError):
            tree.add_block(bad)
        self.assertEqual(tree.tip, "A1")

    def test_failed_reorg_leaves_no_partial_change(self):
        # 旧链 A1/A2 活动；新链 B1 合法，但 B2 非法（双花）。
        # 提交阶段必须整体失败，活动状态停留在 A2。
        tree = self._base_tree()
        tree.add_block(block("A1", "G", 5, [
            tx("a1", [("cbG", 0)], [(25, 0), (25, 0)]),
        ]))
        tree.add_block(block("A2", "A1", 5, [
            tx("a2", [("a1", 0)], [(25, 0)]),
        ]))
        self.assertEqual(tree.tip, "A2")
        tree.add_block(block("B1", "G", 6, [
            tx("b1", [("cbG", 0)], [(40, 0)]),  # fee 10
        ]))
        # B2 引用一个 B1 上不存在的输出（b1:1 只有一个输出）。
        bad = block("B2", "B1", 6, [
            tx("b2", [("b1", 0)], [(40, 0)]),
            tx("b3", [("b1", 0)], [(40, 0)]),  # 二次花费
        ])
        with self.assertRaises(MissingOutPointError):
            tree.add_block(bad)
        self.assertEqual(tree.tip, "A2")
        self.assertEqual(tree.active_chain_ids(), ("G", "A1", "A2"))
        self.assertIsNotNone(tree.utxo("a2", 0))
        # B1 已合法入树（作为非活动侧枝），但其 UTXO 不在活动视图。
        self.assertIn("B1", tree)
        self.assertIsNone(tree.utxo("b1", 0))


class NoSolutionVsBudgetTests(unittest.TestCase):
    def test_empty_tree_no_solution(self):
        tree = BlockTree()
        with self.assertRaises(NoSolutionError):
            tree.choose_tip()
        self.assertIsNone(tree.tip)

    def test_budget_exhausted_is_distinct_from_no_solution(self):
        tree = BlockTree(subsidy=50)
        tree.add_block(genesis())
        # 预算 1 而树里有 1 个节点时刚好够；再加一个块，预算 1 必耗尽。
        tree.add_block(block("A1", "G", 1, []))
        with self.assertRaises(BudgetExhaustedError) as cm:
            tree.choose_tip(budget=1)
        self.assertGreaterEqual(cm.exception.visited, 2)

    def test_budget_failure_is_atomic(self):
        tree = BlockTree(subsidy=50, search_budget=1)
        tree.add_block(genesis())
        before = tree.tip
        with self.assertRaises(BudgetExhaustedError):
            tree.add_block(block("A1", "G", 1, []))
        self.assertEqual(tree.tip, before)
        self.assertNotIn("A1", tree)


class IndependentReferenceTests(unittest.TestCase):
    """对照 tests/reference.py —— 期望值由独立实现给出。"""

    def test_agreement_on_fixed_fork_scenario(self):
        tree = BlockTree(subsidy=50)
        tree.add_block(genesis("G", "cbG", 50, 0))
        blocks = [
            block("A1", "G", 8, [tx("a1", [("cbG", 0)], [(30, 0), (20, 0)])]),
            block("A2", "A1", 8, [
                tx("a2", [("a1", 0)], [(30, 0)]),
            ]),
            block("B1", "G", 9, [
                coinbase("cbB", 60),
                tx("b1", [("cbG", 0)], [(40, 0)]),  # fee 10
            ]),
            block("B2", "B1", 9, [
                tx("b2", [("b1", 0)], [(15, 0), (15, 0)]),
            ]),
        ]
        for b in blocks:
            tree.add_block(b)
        self.assertEqual(tree.tip, "B2")  # 期望来自独立参考
        block_map = {b.block_id: b for b in blocks}
        block_map["G"] = tree.get_block("G")
        ref_tip, valid = ref.best_tip(block_map, 50)
        self.assertEqual(ref_tip, "B2")
        # 核心活动视图必须等于参考实现对 B2 的重放结果。
        self.assertEqual(core_utxo_map(tree), valid["B2"][1])
        # 参考实现自洽性补充断言：replay() 与 best_tip() 内部结果一致。
        for cand in ("A2", "A1", "B1", "B2"):
            self.assertEqual(
                ref.replay(block_map, cand, 50)[0], valid[cand][1])

    def test_reference_flags_each_invalid_family(self):
        # 四类非法情形必须被独立参考拒绝（核心侧在其他用例中逐一断言）。
        g = genesis("G", "cbG", 50, 0)
        cases = {
            "double_spend": block("B", "G", 1, [
                tx("t", [("cbG", 0)], [(10, 0)]),
                tx("u", [("cbG", 0)], [(10, 0)]),
            ]),
            "coinbase_excess": block("B", "G", 1, [coinbase("cb", 51)]),
        }
        for name, b in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ref.RefReject):
                    ref.replay({"G": g, "B": b}, "B", 50)
        # 锁定高度：创世币锁到高度 5，在高度 1 的子块花费 -> 拒绝。
        g2 = genesis("G2", "cbG2", 50, 5)
        child = block("C", "G2", 1, [tx("t", [("cbG2", 0)], [(50, 0)])])
        with self.assertRaises(ref.RefReject):
            ref.replay({"G2": g2, "C": child}, "C", 50)

    def test_randomized_agreement(self):
        rng = random.Random(20260923)
        for trial in range(120):
            tree, blocks, subsidy = self._build_random(rng, trial)
            block_map = {b.block_id: b for b in blocks}
            # 独立参考选 tip 与核心 tip 必须一致。
            ref_tip, valid = ref.best_tip(block_map, subsidy)
            if tree.tip is not None:
                self.assertEqual(
                    tree.tip, ref_tip,
                    msg=f"trial {trial} tip mismatch")
                # UTXO 集合必须逐条一致。
                self.assertEqual(
                    core_utxo_map(tree), valid[ref_tip][1],
                    msg=f"trial {trial} utxo mismatch")
                # 活动链顺序也与参考的 root->tip 一致。
                ids = [n.block_id for n in ref._as_chain(block_map, ref_tip)]
                self.assertEqual(tree.active_chain_ids(), tuple(ids))

    # ------------------------------------------------------------------

    def _build_random(self, rng, trial):
        subsidy = 50
        tree = BlockTree(subsidy=subsidy, search_budget=1_000_000)
        g = genesis(f"g{trial}", f"cbg{trial}", subsidy, 0)
        tree.add_block(g)
        blocks = [g]
        # 每个 UTXO 记为 (txid, idx, value)。
        def funds_for(chain_blocks):
            # 用独立参考给某 tip 重放，拿可用资金池。
            m = {b.block_id: b for b in chain_blocks}
            tip = chain_blocks[-1].block_id
            try:
                utxos, _ = ref.replay(m, tip, subsidy)
            except ref.RefReject:
                return None
            return [(k[0], k[1], v.value) for k, v in utxos.items()]

        frontier = [g.block_id]
        chain_of = {g.block_id: [g]}
        for k in range(1, 7):
            parent = rng.choice(frontier + [g.block_id])
            parent_chain = chain_of[parent]
            height = len(parent_chain)
            if height >= 100:
                break
            pool = funds_for(parent_chain)
            if pool is None:
                continue
            txs = []
            used_inputs = set()
            fees = 0
            rng.shuffle(pool)
            for (sp_tx, sp_idx, val) in pool[:rng.randint(0, min(2, len(pool)))]:
                if (sp_tx, sp_idx) in used_inputs or val <= 0:
                    continue
                # 10% 概率故意双花（同块内二次引用），制造无效块。
                dup = rng.random() < 0.1
                outs_n = rng.randint(1, 2)
                # 大多数时候输出和 <= 输入和；偶尔超额制造非法块。
                overspend = rng.random() < 0.1
                if overspend:
                    pieces = [val + 1]
                else:
                        # 留 0~2 的手续费
                    fee = rng.randint(0, min(2, val))
                    remain = val - fee
                    fees += fee
                    if outs_n == 1 or remain == 0:
                        pieces = [remain]
                    else:
                        cut = rng.randint(0, remain)
                        pieces = [cut, remain - cut]
                inputs = [(sp_tx, sp_idx)]
                if dup:
                    inputs.append((sp_tx, sp_idx))
                # 10% 概率制造锁定高度，使在下一块不可花。
                lh = height + 1 + rng.randint(0, 1) if rng.random() < 0.2 else 0
                txs.append(tx(f"t{trial}_{k}_{sp_tx}_{sp_idx}", inputs,
                              [(p, lh) for p in pieces]))
                used_inputs.add((sp_tx, sp_idx))
            # coinbase：大多数时候合法（补贴+手续费），偶尔多拿 1。
            cb_value = subsidy + fees + (1 if rng.random() < 0.1 else 0)
            txs = [coinbase(f"cb{trial}_{k}", cb_value, 0)] + txs
            b = block(f"blk{trial}_{k}", parent,
                      rng.randint(1, 12), txs)
            try:
                tree.add_block(b)
                accepted = True
            except Exception:
                accepted = False
            if accepted:
                blocks.append(b)
                chain_of[b.block_id] = parent_chain + [b]
                frontier.append(b.block_id)
        return tree, blocks, subsidy


if __name__ == "__main__":
    unittest.main()
