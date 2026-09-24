"""最终确认检查点 / 撤销日志裁剪 / requires_history 的测试。

期望值来源（不调用被测引擎自证）：

* 裁剪边界、锚点高度、裁剪脊链元数据：按 ``finalize_depth=K`` 的定义**手算**
  （“埋于 tip 之下恰好 K 个确认的块成为锚点”）；
* 恢复/重放后的 UTXO 集合：调用独立参考 :func:`tests.reference.replay`
  从创世完整重放（测试自己保留全部块，不依赖被裁剪的引擎）；
* 损坏样本：手工翻转/截断字节，或解码载荷后改写语义字段再重新编码。
"""

import json
import random
import unittest

from reorg_ledger import (
    Block,
    ChainLimitError,
    CheckpointCorruptError,
    DuplicateBlockError,
    HistoryRequired,
    InputContractError,
    LedgerStateError,
    OrphanBlockError,
    OutPoint,
    PrunedBlock,
    ReorgLedger,
    SubmitResult,
    Transaction,
    TxInput,
    TxOutput,
)
from reorg_ledger import checkpoint as ckpt
from tests.reference import best_chain, replay
from tests.test_differential import _Builder


def coinbase(tx_id: str, amount: int = 50, lock: int = 0) -> Transaction:
    return Transaction(tx_id, (), (TxOutput(amount, lock),))


def spend(tx_id: str, prev: str, amount_out: int, lock: int = 0) -> Transaction:
    return Transaction(
        tx_id, (TxInput(prev, 0),), (TxOutput(amount_out, lock),)
    )


def blk(block_id: str, parent: str | None, work: int,
        txs: tuple[Transaction, ...]) -> Block:
    return Block(block_id, parent, work, txs)


def genesis(amount: int = 50, coin_lock: int = 1, block_id: str = "G",
            work: int = 10) -> Block:
    return blk(block_id, None, work, (coinbase("gcb", amount, coin_lock),))


def linear_chain(n: int, *, work: int = 1, start: str = "G") -> list[Block]:
    """生成 G, a1..a(n-1) 的线性合法链（每块花上一块转账输出，coinbase 铸 50）。

    创世输出 50 锁定到高度 1；a1 花掉它输出 45（费 5），coinbase 铸 50；
    之后每块花上一块转账输出 45→40→…（费 5），coinbase 始终铸 50。
    """
    blocks = [genesis(block_id=start, work=work)]
    prev_tx = "gcb"
    amount = 50
    for i in range(1, n):
        bid = f"{start}-a{i}"
        out_amount = amount - 5
        txs = (
            coinbase(f"cb-{bid}", 50),
            spend(f"tx-{bid}", prev_tx, out_amount),
        )
        parent = start if i == 1 else f"{start}-a{i-1}"
        blocks.append(blk(bid, parent, work, txs))
        prev_tx = f"tx-{bid}"
        amount = out_amount
    return blocks


def submit_all(ledger: ReorgLedger, blocks: list[Block]):
    return [ledger.submit(b) for b in blocks]


def utxo_pairs(ledger: ReorgLedger) -> dict:
    return {op: (u.amount, u.lock_height)
            for op, u in ledger.snapshot_utxos().items()}


class FinalityBoundaryTests(unittest.TestCase):
    def test_no_finality_by_default_keeps_everything(self) -> None:
        ledger = ReorgLedger()  # 默认不启用
        blocks = linear_chain(6)
        submit_all(ledger, blocks)
        self.assertIsNone(ledger.finalize_depth)
        self.assertIsNone(ledger.anchor_id)
        self.assertIsNone(ledger.finalized_height)
        self.assertEqual(ledger.pruned_spine, ())
        for b in blocks:  # 旧行为：全部块仍可取回
            self.assertIn(b.block_id, ledger)
            self.assertEqual(ledger.get_block(b.block_id), b)

    def test_finalize_depth_contract(self) -> None:
        for bad in (0, -1, True, 1.5):
            with self.assertRaises(InputContractError):
                ReorgLedger(finalize_depth=bad)  # type: ignore[arg-type]
        # None 与正整数合法。
        self.assertIsNone(ReorgLedger(finalize_depth=None).finalize_depth)
        self.assertEqual(ReorgLedger(finalize_depth=3).finalize_depth, 3)

    def test_anchor_appears_at_exact_confirmation_boundary(self) -> None:
        # K=2：块 b 在 tip 之下恰好 2 个确认时（path 索引 len-1-2）成为锚点。
        ledger = ReorgLedger(finalize_depth=2)
        blocks = linear_chain(5)
        expected_anchor = [None, None, "G", "G-a1", "G-a2"]
        for b, want in zip(blocks, expected_anchor):
            ledger.submit(b)
            self.assertEqual(
                ledger.anchor_id, want,
                f"after tip {b.block_id!r}: anchor must be {want!r}",
            )
        # 手算边界：tip=G-a3(高度5)，锚点=高度3 的 G-a2；窗口只含 a2,a3,a4。
        self.assertEqual(ledger.finalized_height, 3)
        self.assertEqual(ledger.active_path, ("G-a2", "G-a3", "G-a4"))
        self.assertNotIn("G", ledger)
        self.assertNotIn("G-a1", ledger)
        self.assertIn("G-a2", ledger)
        with self.assertRaises(KeyError):
            ledger.get_block("G")

    def test_k1_anchor_after_one_block_on_top(self) -> None:
        ledger = ReorgLedger(finalize_depth=1)
        blocks = linear_chain(3)
        ledger.submit(blocks[0])
        self.assertIsNone(ledger.anchor_id)  # tip 自身上方还没有块
        ledger.submit(blocks[1])
        self.assertEqual(ledger.anchor_id, "G")  # 上方埋 1 块 → 创世成锚
        ledger.submit(blocks[2])
        self.assertEqual(ledger.anchor_id, "G-a1")
        self.assertNotIn("G", ledger)

    def test_pruned_spine_metadata_is_hand_checkable(self) -> None:
        ledger = ReorgLedger(finalize_depth=2)
        submit_all(ledger, linear_chain(5))  # 每块 work=1
        spine = ledger.pruned_spine
        # 手算：锚点 G-a2（高3）；脊链恰为创世 G（高1）、G-a1（高2）。
        self.assertEqual(
            spine,
            (
                PrunedBlock("G", None, 1, 1),
                PrunedBlock("G-a1", "G", 2, 2),
            ),
        )

    def test_tip_utxo_always_matches_full_genesis_replay(self) -> None:
        ledger = ReorgLedger(finalize_depth=2)
        blocks = linear_chain(6)
        for b in blocks:
            ledger.submit(b)
            self.assertEqual(
                utxo_pairs(ledger), dict(replay(blocks[: blocks.index(b) + 1]))
            )


class ShallowReorgAfterFinalityTests(unittest.TestCase):
    def test_reorg_within_window_works_and_old_branch_gets_pruned(self) -> None:
        ledger = ReorgLedger(finalize_depth=2)
        g, a1, a2, a3 = linear_chain(4)
        for b in (g, a1, a2, a3):
            ledger.submit(b)
        # 锚点 G-a1（高2）。在锚点之后的 G-a2 上分叉：窗口内浅层重组必须成功。
        fork = blk(
            "F1", "G-a2", 100,
            (coinbase("cb-F1", 50), spend("tx-F1", "tx-G-a2", 30)),
        )
        outcome = ledger.submit(fork)
        self.assertEqual(outcome.status, "accepted")
        self.assertTrue(outcome.became_tip)
        self.assertTrue(outcome.reorganized)
        self.assertIsNotNone(outcome.result)
        self.assertEqual(outcome.reorg.fork_id, "G-a2")
        self.assertEqual(outcome.reorg.deactivated, ("G-a3",))
        self.assertEqual(outcome.reorg.activated, ("F1",))
        # active_path 自当前锚点 G-a1 起。
        self.assertEqual(ledger.active_path, ("G-a1", "G-a2", "F1"))
        self.assertEqual(
            utxo_pairs(ledger),
            dict(replay([g, a1, a2, fork])),
        )
        # 再沿 F1 推进两格，使锚点前移到 G-a2→F1；旧枝 G-a3 不可达新锚点，
        # 被裁剪，其 ID 留作判重。
        f2 = blk("F2", "F1", 1, (coinbase("cb-F2", 50),
                                  spend("tx-F2", "tx-F1", 25)))
        ledger.submit(f2)
        self.assertEqual(ledger.anchor_id, "G-a2")
        f3 = blk("F3", "F2", 1, (coinbase("cb-F3", 50),
                                  spend("tx-F3", "tx-F2", 20)))
        ledger.submit(f3)
        self.assertEqual(ledger.anchor_id, "F1")
        self.assertNotIn("G-a3", ledger)
        with self.assertRaises(DuplicateBlockError):
            ledger.submit(a3)  # 重交已裁剪的旧枝块 → 判重，不是孤儿/缺历史

    def test_finalized_chain_cannot_be_rolled_back(self) -> None:
        ledger = ReorgLedger(finalize_depth=2)
        blocks = linear_chain(5)
        submit_all(ledger, blocks)
        # 锚点 G-a2（高3）。从已裁剪的 G 直接分叉，work 再大也不能回退最终确认。
        deep = blk(
            "X", "G", 10_000,
            (coinbase("cb-X", 50), spend("tx-X", "gcb", 40)),
        )
        tip_before = ledger.tip_id
        path_before = ledger.active_path
        utxos_before = dict(ledger.snapshot_utxos())
        anchor_before = ledger.anchor_id
        outcome = ledger.submit(deep)
        self.assertEqual(outcome.status, "requires_history")
        self.assertFalse(outcome.accepted)
        self.assertTrue(outcome.requires_history)
        self.assertIsNone(outcome.result)
        history = outcome.history
        self.assertIsInstance(history, HistoryRequired)
        self.assertEqual(history.block_id, "X")
        self.assertEqual(history.missing_parent_id, "G")
        self.assertEqual(history.anchor_id, "G-a2")
        self.assertEqual(history.anchor_height, 3)
        self.assertEqual(history.finalize_depth, 2)
        # 零变更：tip/路径/UTXO/锚点全部保持，块不入树。
        self.assertEqual(ledger.tip_id, tip_before)
        self.assertEqual(ledger.active_path, path_before)
        self.assertEqual(dict(ledger.snapshot_utxos()), utxos_before)
        self.assertEqual(ledger.anchor_id, anchor_before)
        self.assertNotIn("X", ledger)

    def test_unknown_parent_is_still_orphan_not_history(self) -> None:
        ledger = ReorgLedger(finalize_depth=2)
        submit_all(ledger, linear_chain(5))
        with self.assertRaises(OrphanBlockError) as ctx:
            ledger.submit(blk("Y", "never-existed", 1, (coinbase("cb-Y"),)))
        self.assertEqual(ctx.exception.parent_id, "never-existed")
        self.assertNotIn("Y", ledger)
        # requires_history 之后账本仍可正常接块（无半更新）。
        cont = blk(
            "N", "G-a4", 1,
            (coinbase("cb-N", 50), spend("tx-N", "tx-G-a4", 10)),
        )
        out = ledger.submit(cont)
        self.assertEqual(out.status, "accepted")
        self.assertEqual(ledger.tip_id, "N")

    def test_parentless_block_after_pruned_genesis_is_history(self) -> None:
        ledger = ReorgLedger(finalize_depth=1)
        submit_all(ledger, linear_chain(3))  # 创世 G 已被裁剪
        self.assertNotIn("G", ledger)
        out = ledger.submit(blk("G2", None, 1, (coinbase("cb-G2"),)))
        self.assertEqual(out.status, "requires_history")
        self.assertIsNone(out.history.missing_parent_id)


class CheckpointTests(unittest.TestCase):
    def _grown_ledger(self, k: int = 2, n: int = 6) -> ReorgLedger:
        ledger = ReorgLedger(subsidy=50, max_blocks=100, finalize_depth=k)
        submit_all(ledger, linear_chain(n))
        return ledger

    def test_save_before_anchor_is_a_state_error(self) -> None:
        ledger = ReorgLedger(finalize_depth=2)
        with self.assertRaises(LedgerStateError):
            ledger.checkpoint_bytes()  # 尚无任何块
        ledger.submit(genesis())
        with self.assertRaises(LedgerStateError):
            ledger.checkpoint_bytes()  # 只有创世，无块取得 2 个确认
        no_finality = ReorgLedger()
        no_finality.submit(genesis())
        with self.assertRaises(LedgerStateError):
            no_finality.checkpoint_bytes()  # 未启用 finalize_depth

    def test_restore_replays_to_identical_tip_state(self) -> None:
        blocks = linear_chain(7)
        ledger = self._grown_ledger(n=7)
        data = ledger.checkpoint_bytes()
        restored = ReorgLedger.from_checkpoint(data)

        self.assertEqual(restored.subsidy, 50)
        self.assertEqual(restored.max_blocks, 100)
        self.assertEqual(restored.finalize_depth, 2)
        self.assertEqual(restored.tip_id, ledger.tip_id)
        self.assertEqual(restored.height, ledger.height)
        self.assertEqual(restored.cumulative_work, ledger.cumulative_work)
        self.assertEqual(restored.anchor_id, ledger.anchor_id)
        self.assertEqual(restored.active_path, ledger.active_path)
        self.assertEqual(
            [p.block_id for p in restored.pruned_spine],
            [p.block_id for p in ledger.pruned_spine],
        )
        # 关键：tip UTXO 与从创世**完整重放**的独立参考一致。
        self.assertEqual(
            utxo_pairs(restored), dict(replay(blocks))
        )
        self.assertEqual(restored.snapshot_utxos(), ledger.snapshot_utxos())

    def test_restore_then_continue_equals_uninterrupted_chain(self) -> None:
        blocks = linear_chain(6)
        ledger = self._grown_ledger(n=6)
        data = ledger.checkpoint_bytes()
        restored = ReorgLedger.from_checkpoint(data)

        more = [
            blk("G-a6", "G-a5", 1,
                (coinbase("cb-G-a6", 50), spend("tx-G-a6", "tx-G-a5", 15))),
            blk("G-a7", "G-a6", 1,
                (coinbase("cb-G-a7", 50), spend("tx-G-a7", "tx-G-a6", 10))),
        ]
        full = blocks + more
        outcomes = [restored.submit(b) for b in more]
        self.assertTrue(all(o.status == "accepted" for o in outcomes))
        # 加两块后锚点前移到 G-a5，活跃路径自锚点起。
        self.assertEqual(restored.active_path, ("G-a5", "G-a6", "G-a7"))
        self.assertEqual(restored.anchor_id, "G-a5")
        self.assertEqual(utxo_pairs(restored), dict(replay(full)))

        # 与“从未中断、从头提交”的账本逐字段等价。
        uninterrupted = ReorgLedger(finalize_depth=2)
        submit_all(uninterrupted, full)
        self.assertEqual(restored.tip_id, uninterrupted.tip_id)
        self.assertEqual(restored.anchor_id, uninterrupted.anchor_id)
        self.assertEqual(restored.active_path, uninterrupted.active_path)
        self.assertEqual(
            restored.snapshot_utxos(), uninterrupted.snapshot_utxos()
        )
        # 第二次保存/恢复可串联。
        again = ReorgLedger.from_checkpoint(restored.checkpoint_bytes())
        self.assertEqual(again.snapshot_utxos(), uninterrupted.snapshot_utxos())

    def test_restored_ledger_rejects_deep_fork(self) -> None:
        ledger = self._grown_ledger()
        restored = ReorgLedger.from_checkpoint(ledger.checkpoint_bytes())
        deep = blk("X", "G", 999, (coinbase("cb-X", 50),))
        out = restored.submit(deep)
        self.assertEqual(out.status, "requires_history")
        self.assertEqual(out.history.missing_parent_id, "G")

    def test_explicit_restore_params_must_match(self) -> None:
        data = self._grown_ledger().checkpoint_bytes()
        with self.assertRaises(CheckpointCorruptError) as ctx:
            ReorgLedger.from_checkpoint(data, subsidy=49)
        self.assertEqual(ctx.exception.kind, "param-mismatch")
        with self.assertRaises(CheckpointCorruptError) as ctx:
            ReorgLedger.from_checkpoint(data, max_blocks=50)
        self.assertEqual(ctx.exception.kind, "param-mismatch")
        with self.assertRaises(CheckpointCorruptError) as ctx:
            ReorgLedger.from_checkpoint(data, finalize_depth=3)
        self.assertEqual(ctx.exception.kind, "param-mismatch")
        # 显式给出一致参数则正常。
        ok = ReorgLedger.from_checkpoint(
            data, subsidy=50, max_blocks=100, finalize_depth=2
        )
        self.assertEqual(ok.tip_id, "G-a5")

    # ------------------------------------------------------- 损坏样本族

    def test_corrupt_byte_flip_is_rejected(self) -> None:
        data = bytearray(self._grown_ledger().checkpoint_bytes())
        data[len(data) // 2] ^= 0x01
        with self.assertRaises(CheckpointCorruptError) as ctx:
            ReorgLedger.from_checkpoint(bytes(data))
        self.assertEqual(ctx.exception.kind, "bad-checksum")

    def test_truncated_and_garbage_is_rejected(self) -> None:
        raw = self._grown_ledger().checkpoint_bytes()
        for garbage in (b"", b"x", b"RLCK1\n", raw[:20], b"not a checkpoint at all",
                        b"\x00\x01\x02\x03"):
            with self.subTest(garbage=garbage):
                with self.assertRaises(CheckpointCorruptError):
                    ReorgLedger.from_checkpoint(garbage)

    def test_non_bytes_is_rejected(self) -> None:
        with self.assertRaises(CheckpointCorruptError) as ctx:
            ReorgLedger.from_checkpoint("strings are not accepted")  # type: ignore
        self.assertEqual(ctx.exception.kind, "bad-encoding")

    def test_semantic_tamper_anchor_amount_is_rejected(self) -> None:
        raw = self._grown_ledger().checkpoint_bytes()
        payload = ckpt.decode_payload(raw)
        # 改写锚点状态中一枚 UTXO 的金额（字节流仍合法、校验和重算通过），
        # 语义指纹必须失配——恢复不得接受被篡改的锚点余额。
        entry = payload["anchor_state"]["utxos"][0]
        entry[2] = entry[2] + 1
        tampered = ckpt.encode_payload(payload)
        with self.assertRaises(CheckpointCorruptError) as ctx:
            ReorgLedger.from_checkpoint(tampered)
        self.assertEqual(ctx.exception.kind, "bad-fingerprint")

    def test_semantic_tamper_tip_id_is_rejected(self) -> None:
        raw = self._grown_ledger().checkpoint_bytes()
        payload = ckpt.decode_payload(raw)
        payload["tip_id"] = payload["anchor"]["block_id"]  # 谎称 tip=锚点
        with self.assertRaises(CheckpointCorruptError):
            ReorgLedger.from_checkpoint(ckpt.encode_payload(payload))

    def test_semantic_tamper_window_block_is_rejected(self) -> None:
        raw = self._grown_ledger().checkpoint_bytes()
        payload = ckpt.decode_payload(raw)
        # 把窗口内一块的 work 改大：累计 work 元数据与声明不一致 / 指纹失配。
        payload["blocks"][0]["work"] += 1
        with self.assertRaises(CheckpointCorruptError):
            ReorgLedger.from_checkpoint(ckpt.encode_payload(payload))

    def test_tampered_params_rejected(self) -> None:
        raw = self._grown_ledger().checkpoint_bytes()
        payload = ckpt.decode_payload(raw)
        payload["params"]["subsidy"] = 999
        with self.assertRaises(CheckpointCorruptError) as ctx:
            ReorgLedger.from_checkpoint(ckpt.encode_payload(payload))
        self.assertEqual(ctx.exception.kind, "bad-fingerprint")

    def test_bad_magic_and_version_rejected(self) -> None:
        raw = self._grown_ledger().checkpoint_bytes()
        payload = ckpt.decode_payload(raw)
        payload["magic"] = "something-else"
        with self.assertRaises(CheckpointCorruptError) as ctx:
            ReorgLedger.from_checkpoint(ckpt.encode_payload(payload))
        self.assertEqual(ctx.exception.kind, "bad-magic")
        payload2 = ckpt.decode_payload(raw)
        payload2["version"] = 99
        with self.assertRaises(CheckpointCorruptError) as ctx:
            ReorgLedger.from_checkpoint(ckpt.encode_payload(payload2))
        self.assertEqual(ctx.exception.kind, "bad-version")

    def test_broken_window_chain_is_rejected(self) -> None:
        raw = self._grown_ledger().checkpoint_bytes()
        payload = ckpt.decode_payload(raw)
        # 让一个窗口块指向不存在的父：窗口无法从锚点连通重建。
        payload["blocks"][-1]["parent_id"] = "ghost-block"
        with self.assertRaises(CheckpointCorruptError):
            ReorgLedger.from_checkpoint(ckpt.encode_payload(payload))

    def test_invalid_block_content_rejected(self) -> None:
        raw = self._grown_ledger().checkpoint_bytes()
        payload = ckpt.decode_payload(raw)
        # 删掉某笔交易的全部输出：违反“至少一个输出”的结构契约。
        del payload["blocks"][-1]["transactions"][-1]["outputs"][:]
        with self.assertRaises(CheckpointCorruptError):
            ReorgLedger.from_checkpoint(ckpt.encode_payload(payload))

    def test_json_body_with_valid_envelope_but_bad_structure(self) -> None:
        # 信封与校验和都正确，但 JSON 顶层是数组而非对象。
        body = json.dumps([1, 2, 3], separators=(",", ":")).encode()
        import hashlib
        raw = b"RLCK1\n" + hashlib.sha256(body).hexdigest().encode() + b"\n" + body
        with self.assertRaises(CheckpointCorruptError) as ctx:
            ReorgLedger.from_checkpoint(raw)
        self.assertEqual(ctx.exception.kind, "bad-structure")

    def test_restored_state_is_independent_copy(self) -> None:
        ledger = self._grown_ledger()
        restored = ReorgLedger.from_checkpoint(ledger.checkpoint_bytes())
        snap = restored.snapshot_utxos()
        snap.clear()
        self.assertNotEqual(len(snap), len(restored.snapshot_utxos()))


class FinalityWithLimitTests(unittest.TestCase):
    def test_block_limit_is_absolute_height_after_pruning(self) -> None:
        # max_blocks=4, K=1：锚点前移、早期块被裁剪后，第 5 块仍按绝对高度触界。
        ledger = ReorgLedger(max_blocks=4, finalize_depth=1)
        blocks = linear_chain(4)  # 末块 G-a3，高度 4
        submit_all(ledger, blocks)
        self.assertEqual(ledger.anchor_id, "G-a2")  # 高度4 的 tip 之下1确认
        with self.assertRaises(ChainLimitError) as ctx:
            ledger.submit(
                blk("G-a4", "G-a3", 1,
                    (coinbase("cb-over", 50), spend("tx-over", "tx-G-a3", 5)))
            )
        self.assertEqual(ctx.exception.depth, 5)
        self.assertEqual(ctx.exception.limit, 4)
        self.assertEqual(ledger.tip_id, "G-a3")

    def test_checkpoint_respects_small_block_limit_on_restore(self) -> None:
        ledger = ReorgLedger(max_blocks=10, finalize_depth=2)
        submit_all(ledger, linear_chain(6))
        restored = ReorgLedger.from_checkpoint(ledger.checkpoint_bytes())
        self.assertEqual(restored.max_blocks, 10)
        with self.assertRaises(ChainLimitError):
            # 已到高度 6；再构造高度 11 的延伸（经若干块）会在第 11 块触界。
            cur = "G-a5"
            for i in range(6, 11):
                nxt = f"G-a{i}"
                restored.submit(
                    blk(nxt, cur, 1,
                        (coinbase(f"cb-{nxt}", 50),))
                )
                cur = nxt


class OutcomeCompatibilityTests(unittest.TestCase):
    def test_accepted_outcome_carries_submit_result_fields(self) -> None:
        ledger = ReorgLedger(finalize_depth=2)
        out = ledger.submit(genesis())
        self.assertIsInstance(out.result, SubmitResult)
        # 委托字段：旧调用方式 result.became_tip / result.reorg 继续可用。
        self.assertTrue(out.became_tip)
        self.assertFalse(out.reorganized)
        self.assertIsNone(out.reorg)
        self.assertEqual(out.block_id, "G")
        self.assertEqual(out.tip_height, 1)

    def test_history_outcome_has_no_submit_result_fields(self) -> None:
        ledger = ReorgLedger(finalize_depth=1)
        submit_all(ledger, linear_chain(3))
        out = ledger.submit(blk("G2", None, 1, (coinbase("x"),)))
        self.assertEqual(out.status, "requires_history")
        with self.assertRaises(LedgerStateError):
            out.became_tip


class FinalityDifferentialTests(unittest.TestCase):
    """启用 finalize_depth 的随机树差分：期望值仍由独立 reference 全链给出。

    复用 test_differential 中独立于引擎、自记账的确定性块生成器；引擎只作为
    被比对对象，绝不用它生成期望值。每个被接受块后，tip UTXO 必须与
    reference 从创世完整重放该 tip 链一致；tip 必须与独立选链一致。结束后
    对每个已裁剪脊块发高 work 深分叉，必须全部 requires_history；检查点往返
    后状态逐字段一致。
    """

    def test_random_trees_with_finality_match_reference(self) -> None:
        for seed in range(6):
            with self.subTest(seed=seed):
                builder = _Builder(500 + seed)
                main_tip = builder.genesis_id
                for _ in range(9):
                    main_tip = builder.extend(main_tip)
                for _ in range(24):
                    builder.extend(
                        self._pick(builder, seed, len(builder.blocks))
                    )

                ledger = ReorgLedger(subsidy=50, max_blocks=100,
                                     finalize_depth=3)
                ledger.submit(builder.blocks[builder.genesis_id])
                submitted = {builder.genesis_id}
                unavailable: set[str] = set()
                by_depth: dict[int, list[str]] = {}
                for bid in builder.blocks:
                    by_depth.setdefault(builder.heights[bid], []).append(bid)
                order = random.Random(9000 + seed)
                for depth in sorted(by_depth):
                    group = [b for b in by_depth[depth]
                             if b != builder.genesis_id]
                    order.shuffle(group)
                    for bid in group:
                        parent = builder.blocks[bid].parent_id
                        if parent in unavailable or parent not in submitted:
                            unavailable.add(bid)  # 引擎侧必为孤儿
                            continue
                        outcome = ledger.submit(builder.blocks[bid])
                        if outcome.status == "requires_history":
                            unavailable.add(bid)
                            continue
                        self.assertEqual(outcome.status, "accepted")
                        submitted.add(bid)

                        tip = ledger.tip_id
                        chain_ids = builder.chain_ids(tip)
                        chain = [builder.blocks[i] for i in chain_ids]
                        expected = dict(replay(chain, subsidy=50))
                        actual = {
                            op: (u.amount, u.lock_height)
                            for op, u in ledger.snapshot_utxos().items()
                        }
                        self.assertEqual(actual, expected)
                        expect_tip = best_chain(
                            {i: builder.blocks[i] for i in submitted},
                            builder.work_of,
                        )[-1]
                        self.assertEqual(tip, expect_tip)

                # 已裁剪脊块上的任意高 work 深分叉都不能回退最终确认。
                self.assertGreaterEqual(len(ledger.pruned_spine), 1)
                for p in ledger.pruned_spine:
                    deep = blk(
                        f"deep-{seed}-{p.block_id}", p.block_id, 10 ** 6,
                        (coinbase(f"deep-cb-{seed}-{p.block_id}", 50),),
                    )
                    out = ledger.submit(deep)
                    self.assertEqual(out.status, "requires_history")
                    self.assertEqual(
                        out.history.missing_parent_id, p.block_id
                    )
                    self.assertNotIn(deep.block_id, ledger)

                # 检查点往返：tip/锚点/UTXO 全部一致，且可串联二次恢复。
                raw = ledger.checkpoint_bytes()
                restored = ReorgLedger.from_checkpoint(raw)
                self.assertEqual(restored.tip_id, ledger.tip_id)
                self.assertEqual(restored.anchor_id, ledger.anchor_id)
                self.assertEqual(restored.height, ledger.height)
                self.assertEqual(
                    restored.snapshot_utxos(), ledger.snapshot_utxos()
                )
                again = ReorgLedger.from_checkpoint(restored.checkpoint_bytes())
                self.assertEqual(
                    again.snapshot_utxos(), restored.snapshot_utxos()
                )

    @staticmethod
    def _pick(builder: "_Builder", seed: int, n: int) -> str:
        picker = random.Random(800 + seed + n)
        return picker.choice(list(builder.blocks))


if __name__ == "__main__":
    unittest.main()
