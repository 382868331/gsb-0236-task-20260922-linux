"""确定性差分测试。

* 场景全部由固定种子的伪随机生成器在仓库内生成（无外部数据）。
* 期望值由 :mod:`tests.reference` 独立重放 / 独立选链给出，**不调用被测引擎**
  计算任何期望值；引擎只作为被比对对象。
* 覆盖：随机线性链、分叉、work 驱动的真实重组、同分 ID 字典序决胜，以及
  双花 / coinbase 超额 / 锁定高度 / 输出超额 / 重复输入 / 悬空输入的拒绝短码
  与参考实现逐条对齐。
"""

import random
import unittest
from collections import Counter

from reorg_ledger import (
    Block,
    BlockValidationError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
)
from tests.reference import ReferenceReject, best_chain, replay

SUBSIDY = 50


class _Coin:
    __slots__ = ("tx_id", "index", "amount", "lock_height")

    def __init__(self, tx_id: str, index: int, amount: int,
                 lock_height: int) -> None:
        self.tx_id = tx_id
        self.index = index
        self.amount = amount
        self.lock_height = lock_height

    def as_input(self) -> TxInput:
        return TxInput(self.tx_id, self.index)


class _Builder:
    """独立于引擎的小钱包式块生成器（测试夹具，自己记账）。"""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.counter = 0
        self.blocks: dict[str, Block] = {}
        self.work_of: dict[str, int] = {}
        self.heights: dict[str, int] = {}
        self.wallets: dict[str, list[_Coin]] = {}
        self._genesis()

    def _uid(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter:04d}"

    def _genesis(self) -> None:
        gcb = Transaction("genesis-cb", (), (TxOutput(SUBSIDY, 1),))
        g = Block("b0000-genesis", None, 1, (gcb,))
        self.blocks[g.block_id] = g
        self.work_of[g.block_id] = 1
        self.heights[g.block_id] = 1
        self.wallets[g.block_id] = [_Coin("genesis-cb", 0, SUBSIDY, 1)]

    @property
    def genesis_id(self) -> str:
        return "b0000-genesis"

    def chain_ids(self, tip: str) -> list[str]:
        ids: list[str] = []
        cur: str | None = tip
        while cur is not None:
            ids.append(cur)
            cur = self.blocks[cur].parent_id
        ids.reverse()
        return ids

    def extend(self, tip: str, work: int | None = None) -> str:
        """在 ``tip`` 上生成一个保证合法的块，返回新块 ID。"""
        h = self.heights[tip] + 1
        available = list(self.wallets[tip])
        self.rng.shuffle(available)
        txs: list[Transaction] = []
        fees_total = 0
        n_spends = self.rng.randint(0, min(3, len(available)))
        for _ in range(n_spends):
            k = self.rng.randint(1, min(2, len(available)))
            chosen = available[:k]
            del available[:k]
            in_sum = sum(c.amount for c in chosen)
            fee = self.rng.randint(0, min(2, in_sum - 1))
            fees_total += fee
            amount = in_sum - fee
            tx_id = self._uid("tx")
            lock = self.rng.choice([0, 0, 0, h])
            outs = (TxOutput(amount, lock),)
            available.append(_Coin(tx_id, 0, amount, lock))
            txs.append(Transaction(
                tx_id, tuple(c.as_input() for c in chosen), outs
            ))

        cb_id = self._uid("cb")
        cb_amount = SUBSIDY + fees_total
        coinbase = Transaction(cb_id, (), (TxOutput(cb_amount, 0),))
        available.append(_Coin(cb_id, 0, cb_amount, 0))

        block_id = self._uid("b")
        blk = Block(block_id, tip, work or self.rng.randint(1, 9),
                    (coinbase, *txs))
        self.blocks[block_id] = blk
        self.work_of[block_id] = blk.work
        self.heights[block_id] = h
        self.wallets[block_id] = available
        return block_id

    def make_invalid(self, tip: str, kind: str) -> Block:
        """在 ``tip`` 上构造一个确定性非法块（用于拒绝短码交叉比对）。"""
        h = self.heights[tip] + 1
        coins = self.wallets[tip]
        bid = self._uid("bad")
        cb = lambda amt: Transaction(self._uid("cbx"), (), (TxOutput(amt, 0),))

        if kind == "coinbase-overflow":
            coin = coins[0]
            spend = Transaction(
                self._uid("tx"), (coin.as_input(),), (TxOutput(coin.amount, 0),)
            )
            return Block(bid, tip, 1, (cb(SUBSIDY + 1), spend))

        if kind == "missing-input":
            spend = Transaction(
                self._uid("tx"),
                (TxInput("never-existed-tx", 7),),
                (TxOutput(1, 0),),
            )
            return Block(bid, tip, 1, (cb(SUBSIDY), spend))

        if kind == "output-exceeds-input":
            coin = coins[0]
            spend = Transaction(
                self._uid("tx"), (coin.as_input(),),
                (TxOutput(coin.amount + 1, 0),),
            )
            return Block(bid, tip, 1, (cb(SUBSIDY), spend))

        if kind == "duplicate-input":
            coin = coins[0]
            spend = Transaction(
                self._uid("tx"),
                (coin.as_input(), coin.as_input()),
                (TxOutput(1, 0),),
            )
            return Block(bid, tip, 1, (cb(SUBSIDY), spend))

        if kind == "output-locked":
            # 先在父链状态已有硬币上构造“锁定到 h+5”的新硬币不行——它需要先
            # 存在；直接花一枚已存在但把场景设为：tip 高度处新建块里铸一个
            # lock=h+5 的 coinbase，再让子块立刻花费它。
            lock_cb = Transaction(
                self._uid("cbl"), (), (TxOutput(SUBSIDY, h + 5),)
            )
            carrier_id = self._uid("bcr")
            carrier = Block(carrier_id, tip, 1, (lock_cb,))
            # carrier 本身合法（只是把币锁远）；登记生成器簿记。
            self.blocks[carrier_id] = carrier
            self.work_of[carrier_id] = 1
            self.heights[carrier_id] = h
            self.wallets[carrier_id] = [
                c for c in self.wallets[tip]
            ] + [_Coin(lock_cb.tx_id, 0, SUBSIDY, h + 5)]
            spend = Transaction(
                self._uid("tx"),
                (TxInput(lock_cb.tx_id, 0),),
                (TxOutput(SUBSIDY, 0),),
            )
            return Block(bid, carrier_id, 1, (cb(SUBSIDY), spend))

        if kind == "double-spend":
            # 父块花掉 coins[0]，本块再花一次（在自己祖先状态下已不存在）。
            coin = coins[0]
            parent_spend = Transaction(
                self._uid("txp"), (coin.as_input(),),
                (TxOutput(coin.amount, 0),),
            )
            carrier_id = self._uid("bds")
            carrier = Block(carrier_id, tip, 1, (cb(SUBSIDY), parent_spend))
            self.blocks[carrier_id] = carrier
            self.work_of[carrier_id] = 1
            self.heights[carrier_id] = h
            remaining = [c for c in coins if c is not coin]
            self.wallets[carrier_id] = remaining + [
                _Coin(parent_spend.tx_id, 0, coin.amount, 0),
                _Coin(carrier.transactions[0].tx_id, 0, SUBSIDY, 0),
            ]
            again = Transaction(
                self._uid("txd"), (coin.as_input(),),
                (TxOutput(coin.amount, 0),),
            )
            return Block(bid, carrier_id, 1, (cb(SUBSIDY), again))

        raise AssertionError(f"unknown kind {kind!r}")


class DifferentialTests(unittest.TestCase):
    def _reference_utxos(self, builder: _Builder, tip: str):
        chain = [builder.blocks[i] for i in builder.chain_ids(tip)]
        got = replay(chain, subsidy=SUBSIDY)
        return {op: pair for op, pair in got.items()}

    def _assert_matches_reference(
        self, ledger: ReorgLedger, builder: _Builder, accepted: set[str]
    ) -> None:
        expected_tip_chain = best_chain(
            {i: builder.blocks[i] for i in accepted}, builder.work_of
        )
        self.assertEqual(
            list(ledger.active_path), expected_tip_chain,
            "engine active chain disagrees with independent selection",
        )
        tip = expected_tip_chain[-1]
        expected_utxos = self._reference_utxos(builder, tip)
        actual = {
            op: (u.amount, u.lock_height)
            for op, u in ledger.snapshot_utxos().items()
        }
        self.assertEqual(actual, expected_utxos)
        self.assertEqual(ledger.cumulative_work,
                         sum(builder.work_of[i] for i in expected_tip_chain))

    def test_random_trees_against_reference(self) -> None:
        for seed in range(7):
            with self.subTest(seed=seed):
                builder = _Builder(seed)
                # 先沿主链长几节，再随机从任意历史块分叉。
                main_tip = builder.genesis_id
                for _ in range(12):
                    main_tip = builder.extend(main_tip)
                for _ in range(26):
                    base = self.rng_pick_tip(builder, seed)
                    builder.extend(base)

                ledger = ReorgLedger(subsidy=SUBSIDY, max_blocks=100)
                accepted: set[str] = set()
                # 按深度分组、组内洗牌：保证父先于子，又制造乱序接收侧枝。
                order_rng = random.Random(1000 + seed)
                by_depth: dict[int, list[str]] = {}
                for bid, blk in builder.blocks.items():
                    by_depth.setdefault(builder.heights[bid], []).append(bid)
                for depth in sorted(by_depth):
                    group = by_depth[depth]
                    order_rng.shuffle(group)
                    for bid in group:
                        blk = builder.blocks[bid]
                        # 独立预言：对该块整条链重放，应合法。
                        chain = [
                            builder.blocks[i] for i in builder.chain_ids(bid)
                        ]
                        expected = replay(chain, subsidy=SUBSIDY)
                        self.assertIsInstance(expected, dict)
                        ledger.submit(blk)
                        accepted.add(bid)
                        self._assert_matches_reference(
                            ledger, builder, accepted
                        )

    @staticmethod
    def rng_pick_tip(builder: "_Builder", seed: int) -> str:
        picker = random.Random(700 + seed + len(builder.blocks))
        return picker.choice(list(builder.blocks))

    def test_invalid_block_reason_codes_match_reference(self) -> None:
        kinds = (
            "coinbase-overflow",
            "missing-input",
            "output-exceeds-input",
            "duplicate-input",
            "output-locked",
            "double-spend",
        )
        for seed in range(4):
            for kind in kinds:
                with self.subTest(seed=seed, kind=kind):
                    builder = _Builder(200 + seed)
                    tip = builder.genesis_id
                    for _ in range(4):
                        tip = builder.extend(tip)
                    bad = builder.make_invalid(tip, kind)

                    # 先把坏块缺失的载体父块补提交（output-locked / double-spend）。
                    ledger = ReorgLedger(subsidy=SUBSIDY)
                    chain_to_submit = [
                        builder.blocks[i]
                        for i in builder.chain_ids(bad.parent_id)
                    ]
                    for blk in chain_to_submit:
                        ledger.submit(blk)
                    path_before = ledger.active_path
                    utxos_before = dict(ledger.snapshot_utxos())

                    # 引擎：必须给出与参考一致的 reason 短码，且零部分变更。
                    with self.assertRaises(BlockValidationError) as ctx:
                        ledger.submit(bad)
                    engine_reason = ctx.exception.reason
                    self.assertEqual(ledger.active_path, path_before)
                    self.assertEqual(dict(ledger.snapshot_utxos()),
                                     utxos_before)
                    self.assertNotIn(bad.block_id, ledger)

                    # 独立参考：同一条链、同一个拒绝短码。
                    chain = chain_to_submit + [bad]
                    with self.assertRaises(ReferenceReject) as rctx:
                        replay(chain, subsidy=SUBSIDY)
                    self.assertEqual(rctx.exception.reason, engine_reason)

    def test_reorg_paths_and_activation_counts_against_reference(self) -> None:
        builder = _Builder(42)
        # 主链低 work 推进到 m4；侧枝从 m1 分叉并以高 work 反超。
        main = [builder.genesis_id]
        for _ in range(4):
            main.append(builder.extend(main[-1], work=1))
        s1 = builder.extend(main[1], work=50)
        s2 = builder.extend(s1, work=50)

        def fork_between(old_tip: str, new_tip: str):
            old_path = builder.chain_ids(old_tip)
            new_path = builder.chain_ids(new_tip)
            new_set = set(new_path)
            fork = max(i for i, bid in enumerate(new_path) if bid in set(old_path))
            deactivated = tuple(
                bid for bid in reversed(old_path) if bid not in new_set
            )
            activated = tuple(new_path[fork + 1:])
            return new_path[fork], deactivated, activated

        ledger = ReorgLedger(subsidy=SUBSIDY)
        events = Counter()
        # 拓扑序提交：创世 + 线性推进主链，再让侧枝反超（真实重组）。
        ledger.submit(builder.blocks[builder.genesis_id])
        for bid in main[1:]:
            ledger.submit(builder.blocks[bid])
        self.assertEqual(ledger.tip_id, main[4])

        result = ledger.submit(builder.blocks[s1])
        self.assertTrue(result.reorganized)
        assert result.reorg is not None
        fork, deact, act = fork_between(main[4], s1)
        self.assertEqual(result.reorg.fork_id, fork)
        self.assertEqual(result.reorg.deactivated, deact)
        self.assertEqual(result.reorg.activated, act)
        self.assertEqual(deact, (main[4], main[3], main[2]))
        self.assertEqual(act, (s1,))
        events["reorg"] += 1

        # 侧枝继续延伸：无撤销块，属于同链向前延伸而非重组。
        result2 = ledger.submit(builder.blocks[s2])
        self.assertFalse(result2.reorganized)

        self.assertGreaterEqual(events["reorg"], 1)
        self.assertEqual(list(ledger.active_path), builder.chain_ids(s2))
        actual = {
            op: (u.amount, u.lock_height)
            for op, u in ledger.snapshot_utxos().items()
        }
        self.assertEqual(actual, dict(self._reference_utxos(builder, s2)))

    def test_roundtrip_only_as_supplement(self) -> None:
        """往返一致（UTXO 快照不可变/可重建）仅作补充断言。"""
        builder = _Builder(9)
        tip = builder.genesis_id
        for _ in range(5):
            tip = builder.extend(tip)
        ledger = ReorgLedger(subsidy=SUBSIDY)
        for bid in builder.chain_ids(tip):
            ledger.submit(builder.blocks[bid])
        snap1 = ledger.snapshot_utxos()
        snap2 = ledger.snapshot_utxos()
        self.assertEqual(snap1, snap2)
        self.assertIsNot(snap1, snap2)
        for op, utxo in snap1.items():
            self.assertIsInstance(op, OutPoint)
            self.assertEqual((utxo.amount, utxo.lock_height),
                             (snap2[op].amount, snap2[op].lock_height))


if __name__ == "__main__":
    unittest.main()
