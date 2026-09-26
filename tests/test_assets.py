"""多资产与发行授权的场景测试 + 独立审计对照。

覆盖：创设授权、同块顺序发行再转账、同块二次发行、两分支竞争同一授权、
显式销毁、额度耗尽、跨资产补短被拒、未声明销毁被拒、无授权铸造被拒、
重组后数量与授权恢复、独立重算（audit_assets）核对守恒。
"""

import unittest

from reorg_ledger import (
    AssetTotals,
    Block,
    BlockValidationError,
    OutPoint,
    ReorgLedger,
    Transaction,
    TxInput,
    TxOutput,
    audit_assets,
)


def coinbase(tx_id, amount, lock=0):
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def block(block_id, parent, work, txs):
    return Block(block_id, parent, work, txs)


def make_chain(ledger, prefix="g"):
    """创世 + 一个普通块，返回可花费的基础币 outpoint（50 枚，lock=1）。"""
    ledger.submit(block("G", None, 10, (coinbase("gcb", 50, lock=1),)))
    return ("gcb", 0)


def create_asset_tx(tx_id, prev, allowance, base_out=30):
    """花一笔普通基础币输入，创设新授权；资产 ID = f"{tx_id}:1"。"""
    return Transaction(
        tx_id,
        (TxInput(*prev),),
        (
            TxOutput(base_out, 0),
            TxOutput(20, 0, issue_asset=f"{tx_id}:1", issue_allowance=allowance),
        ),
    )


def issue_tx(tx_id, auth_point, asset_id, minted, successor=None, extra_outs=()):
    """消费授权铸出 minted 个资产；successor 给定时携带剩余额度。"""
    outs = [TxOutput(5, 0, assets={asset_id: minted})]
    if successor is not None:
        outs.append(
            TxOutput(5, 0, issue_asset=asset_id, issue_allowance=successor)
        )
    outs.extend(extra_outs)
    return Transaction(tx_id, (TxInput(*auth_point),), tuple(outs))


class AssetBasicTests(unittest.TestCase):
    def test_create_issue_transfer_burn_and_audit(self):
        ledger = ReorgLedger()
        make_chain(ledger)
        asset = "m1:1"

        # 块 M：创设授权（额度 100），同块顺序发行 30 并转账 12 给另一输出。
        create = create_asset_tx("m1", ("gcb", 0), allowance=100)
        issue = issue_tx("m2", ("m1", 1), asset, minted=30, successor=70)
        transfer = Transaction(
            "m3",
            (TxInput("m2", 0),),
            (
                TxOutput(1, 0, assets={asset: 12}),
                TxOutput(1, 0, assets={asset: 18}),
            ),
        )
        ledger.submit(block("M", "G", 5, (coinbase("mcb", 50), create, issue, transfer)))
        utxos = ledger.snapshot_utxos()
        self.assertEqual(utxos[OutPoint("m3", 0)].asset_map, {asset: 12})
        self.assertEqual(utxos[OutPoint("m3", 1)].asset_map, {asset: 18})
        # 后继授权剩余 70。
        self.assertEqual(utxos[OutPoint("m2", 1)].issue_allowance, 70)
        self.assertEqual(utxos[OutPoint("m2", 1)].issue_asset, asset)

        # 块 N：显式销毁 8，余 4 转走；同时再发行 70（额度耗尽，无后继）。
        burn = Transaction(
            "n1",
            (TxInput("m3", 0),),
            (TxOutput(1, 0, assets={asset: 4}),),
            burns={asset: 8},
        )
        issue_rest = issue_tx("n2", ("m2", 1), asset, minted=70, successor=None)
        ledger.submit(block("N", "M", 5, (coinbase("ncb", 50), burn, issue_rest)))
        utxos = ledger.snapshot_utxos()
        self.assertNotIn(OutPoint("m2", 1), utxos)  # 授权已耗尽消失
        self.assertEqual(utxos[OutPoint("n2", 0)].asset_map, {asset: 70})

        # 独立审计：已发行 100、已销毁 8、在外 92，守恒。
        chain = [ledger.get_block(bid) for bid in ledger.active_path]
        totals = audit_assets(chain, ledger.snapshot_utxos())
        self.assertEqual(
            totals[asset],
            AssetTotals(asset_id=asset, issued=100, burned=8, outstanding=92),
        )

    def test_second_issuance_same_block(self):
        """同块二次发行：tx2 消费 tx1 的后继授权继续铸，额度逐次结算。"""
        ledger = ReorgLedger()
        make_chain(ledger)
        asset = "c1:1"
        create = create_asset_tx("c1", ("gcb", 0), allowance=10)
        first = issue_tx("c2", ("c1", 1), asset, minted=4, successor=6)
        second = issue_tx("c3", ("c2", 1), asset, minted=6, successor=None)
        ledger.submit(
            block("C", "G", 5, (coinbase("ccb", 50), create, first, second))
        )
        utxos = ledger.snapshot_utxos()
        self.assertEqual(utxos[OutPoint("c2", 0)].asset_map, {asset: 4})
        self.assertEqual(utxos[OutPoint("c3", 0)].asset_map, {asset: 6})
        chain = [ledger.get_block(bid) for bid in ledger.active_path]
        totals = audit_assets(chain, utxos)
        self.assertEqual(totals[asset].issued, 10)
        self.assertEqual(totals[asset].outstanding, 10)

    def test_double_spend_of_authority_same_block_rejected(self):
        """同一授权在同块被两笔交易消费：第二笔 missing-input。"""
        ledger = ReorgLedger()
        make_chain(ledger)
        asset = "d1:1"
        create = create_asset_tx("d1", ("gcb", 0), allowance=10)
        first = issue_tx("d2", ("d1", 1), asset, minted=10)
        second = issue_tx("d3", ("d1", 1), asset, minted=10)
        with self.assertRaises(BlockValidationError) as ctx:
            ledger.submit(
                block("D", "G", 5, (coinbase("dcb", 50), create, first, second))
            )
        self.assertEqual(ctx.exception.reason, "missing-input")
        self.assertNotIn("D", ledger)

    def test_old_style_transactions_unchanged(self):
        """无资产字段的旧交易保持原行为（含手续费与 coinbase 结算）。"""
        ledger = ReorgLedger()
        make_chain(ledger)
        plain = Transaction(
            "p1", (TxInput("gcb", 0),), (TxOutput(45, 0),)
        )
        ledger.submit(block("P", "G", 5, (coinbase("pcb", 55), plain)))
        utxos = ledger.snapshot_utxos()
        self.assertEqual(utxos[OutPoint("p1", 0)].amount, 45)
        self.assertEqual(utxos[OutPoint("p1", 0)].asset_map, {})
        self.assertFalse(utxos[OutPoint("p1", 0)].is_issuance_authority)


class AssetRejectionTests(unittest.TestCase):
    def setUp(self):
        self.ledger = ReorgLedger()
        make_chain(self.ledger)

    def _reject(self, txs, reason):
        with self.assertRaises(BlockValidationError) as ctx:
            self.ledger.submit(
                block("X", "G", 5, (coinbase("xcb", 50), *txs))
            )
        self.assertEqual(ctx.exception.reason, reason)
        self.assertNotIn("X", self.ledger)
        self.assertEqual(self.ledger.tip_id, "G")

    def test_cross_asset_coverup_rejected(self):
        """资产 A 的短缺不能用资产 B 的盈余补。"""
        a1 = create_asset_tx("a1", ("gcb", 0), allowance=10)   # 资产 a1:1
        mint_a = issue_tx("a2", ("a1", 1), "a1:1", minted=10)
        # 输出 12 个 a1:1（只有 10 个）——即使同 tx 也没别的资产可补。
        bad = Transaction(
            "a3",
            (TxInput("a2", 0),),
            (TxOutput(1, 0, assets={"a1:1": 12}),),
        )
        self._reject((a1, mint_a, bad), "mint-without-authority")

    def test_undeclared_burn_rejected(self):
        a1 = create_asset_tx("b1", ("gcb", 0), allowance=10)
        mint = issue_tx("b2", ("b1", 1), "b1:1", minted=10)
        bad = Transaction(
            "b3",
            (TxInput("b2", 0),),
            (TxOutput(1, 0, assets={"b1:1": 7}),),  # 少 3 个未声明
        )
        self._reject((a1, mint, bad), "asset-burn-undeclared")

    def test_mint_without_authority_rejected(self):
        bad = Transaction(
            "c1",
            (TxInput("gcb", 0),),
            (TxOutput(40, 0), TxOutput(1, 0, assets={"fake:0": 5})),
        )
        self._reject((bad,), "mint-without-authority")

    def test_foreign_asset_id_rejected(self):
        """创设交易不得指定别人的 asset_id。"""
        bad = Transaction(
            "e1",
            (TxInput("gcb", 0),),
            (
                TxOutput(40, 0),
                TxOutput(10, 0, issue_asset="someone-else:0", issue_allowance=5),
            ),
        )
        self._reject((bad,), "issue-asset-mismatch")

    def test_allowance_mismatch_rejected(self):
        create = create_asset_tx("f1", ("gcb", 0), allowance=10)
        bad = issue_tx("f2", ("f1", 1), "f1:1", minted=6, successor=3)  # 6+3≠10
        self._reject((create, bad), "allowance-mismatch")

    def test_multiple_successors_rejected(self):
        create = create_asset_tx("h1", ("gcb", 0), allowance=10)
        bad = Transaction(
            "h2",
            (TxInput("h1", 1),),
            (
                TxOutput(1, 0, issue_asset="h1:1", issue_allowance=6),
                TxOutput(1, 0, issue_asset="h1:1", issue_allowance=4),
            ),
        )
        self._reject((create, bad), "multiple-successors")

    def test_issue_in_coinbase_rejected(self):
        cb = Transaction(
            "icb",
            (),
            (
                TxOutput(50, 0),
                TxOutput(10, 0, issue_asset="icb:1", issue_allowance=5),
            ),
        )
        with self.assertRaises(BlockValidationError) as ctx:
            self.ledger.submit(block("I", "G", 5, (cb,)))
        self.assertEqual(ctx.exception.reason, "issue-in-coinbase")

    def test_allowance_exhaustion(self):
        """额度耗尽后授权消失，再发行即 missing-input。"""
        create = create_asset_tx("j1", ("gcb", 0), allowance=10)
        drain = issue_tx("j2", ("j1", 1), "j1:1", minted=10, successor=None)
        self.ledger.submit(
            block("J", "G", 5, (coinbase("jcb", 50), create, drain))
        )
        self.assertNotIn(OutPoint("j1", 1), self.ledger.snapshot_utxos())
        again = issue_tx("j3", ("j1", 1), "j1:1", minted=1)
        with self.assertRaises(BlockValidationError) as ctx:
            self.ledger.submit(block("K", "J", 5, (coinbase("kcb", 50), again)))
        self.assertEqual(ctx.exception.reason, "missing-input")
        self.assertNotIn("K", self.ledger)


class AssetReorgTests(unittest.TestCase):
    def test_competing_branches_restore_amounts_and_authority(self):
        """两分支竞争同一授权：重组后数量与授权随撤销机制恢复。"""
        ledger = ReorgLedger()
        make_chain(ledger)
        asset = "r1:1"
        create = create_asset_tx("r1", ("gcb", 0), allowance=10)
        ledger.submit(block("R", "G", 5, (coinbase("rcb", 50), create)))

        # 分支 A（弱）：发行 4，留后继 6。
        issue_a = issue_tx("ra2", ("r1", 1), asset, minted=4, successor=6)
        ledger.submit(block("RA", "R", 5, (coinbase("racb", 50), issue_a)))
        self.assertEqual(ledger.tip_id, "RA")
        utxos_a = ledger.snapshot_utxos()
        self.assertEqual(utxos_a[OutPoint("ra2", 0)].asset_map, {asset: 4})
        self.assertEqual(utxos_a[OutPoint("ra2", 1)].issue_allowance, 6)

        # 分支 B（强）：从同一授权发行 10（全额，无后继）。
        issue_b = issue_tx("rb2", ("r1", 1), asset, minted=10, successor=None)
        result = ledger.submit(
            block("RB", "R", 9, (coinbase("rbcb", 50), issue_b))
        )
        self.assertTrue(result.reorganized)
        self.assertEqual(result.reorg.deactivated, ("RA",))
        self.assertEqual(result.reorg.activated, ("RB",))
        utxos_b = ledger.snapshot_utxos()
        # 旧分支的铸币与后继授权被撤销，新分支状态生效。
        self.assertNotIn(OutPoint("ra2", 0), utxos_b)
        self.assertNotIn(OutPoint("ra2", 1), utxos_b)
        self.assertEqual(utxos_b[OutPoint("rb2", 0)].asset_map, {asset: 10})
        self.assertNotIn(OutPoint("r1", 1), utxos_b)  # 授权已耗尽

        # 独立审计新链：发行 10、销毁 0、在外 10。
        chain = [ledger.get_block(bid) for bid in ledger.active_path]
        totals = audit_assets(chain, utxos_b)
        self.assertEqual(totals[asset].issued, 10)
        self.assertEqual(totals[asset].outstanding, 10)

        # 再重组回分支 A 的延伸（AA 累计 work 反超）：授权与数量恢复为 A 线。
        extend_a = Transaction(
            "ra3", (TxInput("ra2", 0),), (TxOutput(1, 0, assets={asset: 4}),)
        )
        result2 = ledger.submit(
            block("RAA", "RA", 20, (coinbase("raacb", 50), extend_a))
        )
        self.assertTrue(result2.reorganized)
        utxos_a2 = ledger.snapshot_utxos()
        self.assertEqual(utxos_a2[OutPoint("ra3", 0)].asset_map, {asset: 4})
        self.assertEqual(utxos_a2[OutPoint("ra2", 1)].issue_allowance, 6)
        self.assertNotIn(OutPoint("rb2", 0), utxos_a2)
        chain = [ledger.get_block(bid) for bid in ledger.active_path]
        totals = audit_assets(chain, utxos_a2)
        self.assertEqual(totals[asset].issued, 4)
        self.assertEqual(totals[asset].outstanding, 4)


if __name__ == "__main__":
    unittest.main()
