"""核心引擎：有界 UTXO 区块树 + 撤销账本式重组 + 最终确认检查点。

设计要点
--------
1. **区块树**：所有通过祖先状态验证的区块都保留在内存树中（含非活跃侧枝）。
2. **祖先状态验证**：接收区块时，从最旧的可用状态（创世，或最终确认**锚点**）
   沿其父链重放到父块，再按序应用本块交易。验证在临时状态上进行，失败时引擎
   状态零变更。
3. **选链**：累计 ``work`` 最大；累计 work 相同取 *tip 区块 ID 字典序较小*。
4. **原子重组**：tip 改变时，先在临时状态上重放整条新链，成功后一次性切换
   tip / UTXO / 活跃路径——即“撤销旧链再应用新链”的原子提交；中途任何异常
   都保留旧状态。
5. **有界**：链长（含创世）超过 ``max_blocks``（默认 100）抛
   :class:`ChainLimitError`（预算耗尽），区别于 :class:`BlockValidationError`
   （被证明非法）。
6. **最终确认深度与检查点**（``finalize_depth=K``）：活跃链上埋于 tip 之下恰好
   K 个确认的块成为不可回退的**锚点**。引擎在锚点处保存 UTXO/已用交易集合的
   检查点，并裁剪锚点之前的撤销日志（块与重放数据）；此后所有验证/重组只需从
   锚点重放窗口内的块。锚点只沿活跃链单调前移，绝不后退。
7. **缺历史（requires_history）**：锚点只允许在其之上接块。提交块的父块（或
   更早祖先）落在已裁剪脊链上，意味着接受它需要深度回退最终确认历史，
   ``submit`` 返回状态 ``"requires_history"``（该块不入树、状态零变更）；
   父块完全未知仍是 :class:`OrphanBlockError`，二者严格区分。

引擎内全部金额运算均为任意精度整数，不使用浮点。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import checkpoint as _ckpt
from .errors import (
    BlockValidationError,
    ChainLimitError,
    CheckpointCorruptError,
    DuplicateBlockError,
    InputContractError,
    LedgerStateError,
    OrphanBlockError,
)
from .models import Block, OutPoint, Transaction, TxInput, TxOutput, UTXO

__all__ = [
    "DEFAULT_SUBSIDY",
    "MAX_CHAIN_BLOCKS",
    "ReorgPath",
    "SubmitResult",
    "HistoryRequired",
    "SubmitOutcome",
    "PrunedBlock",
    "ReorgLedger",
]

DEFAULT_SUBSIDY = 50
MAX_CHAIN_BLOCKS = 100


@dataclass(frozen=True, slots=True)
class ReorgPath:
    """一次重组的路径描述。

    * ``fork_id``       —— 新旧链的最后一个共同块。
    * ``deactivated``   —— 被撤销的旧链区块，顺序为 **旧 tip 向下** 至分叉点之后
      第一个块（即撤销应用顺序）。
    * ``activated``     —— 被应用的新链区块，顺序为分叉点之后第一个块 **向上到
      新 tip**（即应用顺序）。
    """

    fork_id: str
    old_tip: str
    new_tip: str
    deactivated: tuple[str, ...]
    activated: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """``submit`` 的结果。区块被拒绝时不返回结果而是抛异常。

    高度与累计工作量字段描述**提交后活跃链 tip**（本块未必成为 tip）。
    """

    block_id: str
    block_height: int
    tip_id: str
    tip_height: int
    cumulative_work: int
    became_tip: bool
    reorganized: bool
    reorg: ReorgPath | None


@dataclass(frozen=True, slots=True)
class HistoryRequired:
    """``submit`` 的“缺历史”状态载荷：接块需要已被裁剪的最终确认之前的历史。

    这不是非法判定，也不是孤儿块：被引用的祖先确实存在过，但已随检查点裁剪。
    """

    block_id: str
    missing_parent_id: str | None
    anchor_id: str
    anchor_height: int
    finalize_depth: int | None

    @property
    def status(self) -> str:
        return "requires_history"


@dataclass(frozen=True, slots=True)
class SubmitOutcome:
    """``submit`` 的联合返回。

    * ``status == "accepted"``       ：``result`` 为 :class:`SubmitResult`；
    * ``status == "requires_history"``：``history`` 为 :class:`HistoryRequired`，
      提交块未入树，账本状态零变更。
    """

    status: str
    result: SubmitResult | None = None
    history: HistoryRequired | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    @property
    def requires_history(self) -> bool:
        return self.status == "requires_history"

    # 向后兼容：accepted 时委托传统 SubmitResult 的字段，旧调用方可以继续
    # 直接写 ``result.became_tip`` / ``result.reorg`` 而无需感知联合状态。
    def _r(self) -> SubmitResult:
        if self.result is None:
            raise LedgerStateError(
                "submit outcome is 'requires_history'; there is no accepted "
                "SubmitResult to read"
            )
        return self.result

    @property
    def block_id(self) -> str:
        return self._r().block_id

    @property
    def block_height(self) -> int:
        return self._r().block_height

    @property
    def tip_id(self) -> str:
        return self._r().tip_id

    @property
    def tip_height(self) -> int:
        return self._r().tip_height

    @property
    def cumulative_work(self) -> int:
        return self._r().cumulative_work

    @property
    def became_tip(self) -> bool:
        return self._r().became_tip

    @property
    def reorganized(self) -> bool:
        return self._r().reorganized

    @property
    def reorg(self) -> ReorgPath | None:
        return self._r().reorg


@dataclass(frozen=True, slots=True)
class PrunedBlock:
    """已裁剪脊链上的一个块（仅保留定位元数据，撤销日志已删）。"""

    block_id: str
    parent_id: str | None
    height: int
    cumulative_work: int


@dataclass(slots=True)
class _Node:
    block: Block
    parent_id: str | None
    height: int
    cumulative_work: int

    @property
    def block_id(self) -> str:
        return self.block.block_id


class _UtxoState:
    """重放过程中的可变 UTXO 状态（内部类型，非公开契约）。"""

    __slots__ = ("utxos", "applied_tx_ids")

    def __init__(self) -> None:
        self.utxos: dict[OutPoint, UTXO] = {}
        self.applied_tx_ids: set[str] = set()

    def copy(self) -> "_UtxoState":
        clone = _UtxoState()
        clone.utxos = self.utxos.copy()
        clone.applied_tx_ids = self.applied_tx_ids.copy()
        return clone

    def commit(self, other: "_UtxoState") -> None:
        self.utxos = other.utxos
        self.applied_tx_ids = other.applied_tx_ids


def _ve(
    reason: str,
    message: str,
    *,
    block: Block | None = None,
    height: int | None = None,
    tx: Transaction | None = None,
    outpoint: OutPoint | None = None,
) -> BlockValidationError:
    return BlockValidationError(
        reason,
        message,
        block_id=block.block_id if block is not None else None,
        height=height,
        tx_id=tx.tx_id if tx is not None else None,
        outpoint=outpoint,
    )


def _apply_block_to_state(
    state: _UtxoState,
    block: Block,
    height: int,
    subsidy: int,
) -> None:
    """在 ``state`` 上**严格按序**应用一个区块的全部交易。

    语义：

    * coinbase（零输入交易）必须位于索引 0 且至多一笔；其输出在索引 0 处
      即刻入账，因此同块后续交易可按序花费（仍受锁定高度约束）。
    * coinbase 的铸币上限 ``subsidy + 手续费`` 在块末结算；超额则整块回滚。
    * 失败时 ``state`` 不被部分修改（所有写入先发生在副本上）。
    """

    work_state = state.copy()
    total_fees = 0
    coinbase: Transaction | None = None

    for index, tx in enumerate(block.transactions):
        if tx.tx_id in work_state.applied_tx_ids:
            raise _ve(
                "duplicate-txid",
                "transaction id already used earlier on this chain",
                block=block, height=height, tx=tx,
            )

        if tx.is_coinbase:
            if coinbase is not None:
                raise _ve(
                    "multiple-coinbase",
                    "a block contains more than one coinbase transaction",
                    block=block, height=height, tx=tx,
                )
            if index != 0:
                raise _ve(
                    "coinbase-position",
                    "a zero-input (coinbase) transaction is only allowed at "
                    "transaction index 0",
                    block=block, height=height, tx=tx,
                )
            coinbase = tx
            for out_index, out in enumerate(tx.outputs):
                op = OutPoint(tx.tx_id, out_index)
                if op in work_state.utxos:
                    raise _ve(
                        "duplicate-txid",
                        "coinbase output point collides with an existing UTXO",
                        block=block, height=height, tx=tx, outpoint=op,
                    )
                work_state.utxos[op] = UTXO(out.amount, out.lock_height)
            work_state.applied_tx_ids.add(tx.tx_id)
            continue

        # --- 普通交易：输入唯一性 / 存在性 / 锁定高度 / 金额守恒 ---
        seen: set[OutPoint] = set()
        input_sum = 0
        resolved: list[OutPoint] = []
        for txin in tx.inputs:
            op = txin.outpoint
            if op in seen:
                raise _ve(
                    "duplicate-input",
                    "input is spent more than once within the same transaction",
                    block=block, height=height, tx=tx, outpoint=op,
                )
            seen.add(op)
            utxo = work_state.utxos.get(op)
            if utxo is None:
                raise _ve(
                    "missing-input",
                    "input references an output that does not exist or is "
                    "already spent",
                    block=block, height=height, tx=tx, outpoint=op,
                )
            if utxo.lock_height > height:
                raise _ve(
                    "output-locked",
                    f"output locked until height {utxo.lock_height} cannot be "
                    f"spent at height {height}",
                    block=block, height=height, tx=tx, outpoint=op,
                )
            input_sum += utxo.amount
            resolved.append(op)

        output_sum = sum(o.amount for o in tx.outputs)
        if output_sum > input_sum:
            raise _ve(
                "output-exceeds-input",
                f"output sum {output_sum} exceeds input sum {input_sum}",
                block=block, height=height, tx=tx,
            )
        total_fees += input_sum - output_sum

        # --- 提交该交易：移除输入、新增输出（严格按序，后者可花前者输出） ---
        for op in resolved:
            del work_state.utxos[op]
        for out_index, out in enumerate(tx.outputs):
            op = OutPoint(tx.tx_id, out_index)
            if op in work_state.utxos:
                raise _ve(
                    "duplicate-txid",
                    "transaction output point collides with an existing UTXO "
                    "(transaction id reused on this chain)",
                    block=block, height=height, tx=tx, outpoint=op,
                )
            work_state.utxos[op] = UTXO(out.amount, out.lock_height)
        work_state.applied_tx_ids.add(tx.tx_id)

    if coinbase is not None:
        minted = sum(o.amount for o in coinbase.outputs)
        allowance = subsidy + total_fees
        if minted > allowance:
            raise _ve(
                "coinbase-overflow",
                f"coinbase mints {minted} but subsidy+fees allowance is "
                f"{allowance} (subsidy={subsidy}, fees={total_fees})",
                block=block, height=height, tx=coinbase,
            )

    state.commit(work_state)


def _state_parts(state: _UtxoState) -> dict:
    """状态的确定性纯数据表示（指纹/序列化用）。"""
    return {
        "utxos": sorted(
            (op.tx_id, op.index, utxo.amount, utxo.lock_height)
            for op, utxo in state.utxos.items()
        ),
        "tx_ids": sorted(state.applied_tx_ids),
    }


def _state_from_parts(parts: dict, where: str) -> _UtxoState:
    state = _UtxoState()
    if set(parts.keys()) != {"utxos", "tx_ids"}:
        raise CheckpointCorruptError(
            "bad-structure", f"{where}: state object must have keys utxos/tx_ids"
        )
    raw_utxos, raw_txids = parts["utxos"], parts["tx_ids"]
    if not isinstance(raw_utxos, list) or not isinstance(raw_txids, list):
        raise CheckpointCorruptError(
            "bad-structure", f"{where}: utxos/tx_ids must be arrays"
        )
    for item in raw_utxos:
        if (
            not isinstance(item, list)
            or len(item) != 4
            or not all(isinstance(v, int) and not isinstance(v, bool)
                       for v in item[1:])
            or not isinstance(item[0], str)
        ):
            raise CheckpointCorruptError(
                "bad-structure", f"{where}: malformed utxo entry {item!r}"
            )
        tx_id, index, amount, lock = item
        if index < 0 or amount < 1 or lock < 0:
            raise CheckpointCorruptError(
                "bad-value", f"{where}: utxo entry out of range: {item!r}"
            )
        op = OutPoint(tx_id, index)
        if op in state.utxos:
            raise CheckpointCorruptError(
                "bad-structure", f"{where}: duplicate utxo entry {item!r}"
            )
        state.utxos[op] = UTXO(amount, lock)
    for tx_id in raw_txids:
        if not isinstance(tx_id, str) or not tx_id:
            raise CheckpointCorruptError(
                "bad-structure", f"{where}: malformed tx id {tx_id!r}"
            )
        state.applied_tx_ids.add(tx_id)
    return state


class ReorgLedger:
    """有界 UTXO 重组账本。

    Parameters
    ----------
    subsidy:
        每个区块 coinbase 的固定补贴（非负整数）。coinbase 总产出不得超过
        ``subsidy + 区块内普通交易手续费之和``。
    max_blocks:
        任意一条链允许的最大块数（含创世），默认 100。超出抛
        :class:`~reorg_ledger.errors.ChainLimitError`。
    finalize_depth:
        最终确认深度 ``K``（正整数），默认 ``None`` 表示**不启用检查点裁剪**
        （此时行为与无界保留撤销日志的旧版本完全一致）。启用后，活跃链上埋于
        tip 之下恰好 K 个确认的块成为锚点：锚点处保存 UTXO 检查点，更早的撤销
        日志被裁剪；需要回退到锚点之前的提交返回 ``"requires_history"``。
    """

    def __init__(
        self,
        subsidy: int = DEFAULT_SUBSIDY,
        max_blocks: int = MAX_CHAIN_BLOCKS,
        *,
        finalize_depth: int | None = None,
    ) -> None:
        self._subsidy = self._check_param(subsidy, "subsidy", min_value=0)
        self._max_blocks = self._check_param(max_blocks, "max_blocks", min_value=1)
        self._finalize_depth = self._check_finalize_depth(finalize_depth)
        self._nodes: dict[str, _Node] = {}
        self._tip_id: str | None = None
        self._state = _UtxoState()
        # 最终确认检查点状态。
        self._anchor_id: str | None = None
        self._anchor_state: _UtxoState | None = None
        self._pruned_spine: list[PrunedBlock] = []
        # 所有已被裁剪掉的块 ID（最终确认脊链 + 锚点之前的非脊链侧枝）。
        self._pruned_ids: set[str] = set()

    @staticmethod
    def _check_param(value: object, name: str, *, min_value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise InputContractError(
                f"{name}: expected int, got {type(value).__name__}"
            )
        if value < min_value:
            raise InputContractError(
                f"{name}: expected value >= {min_value}, got {value}"
            )
        return value

    @staticmethod
    def _check_finalize_depth(value: object) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise InputContractError(
                f"finalize_depth: expected int or None, got {type(value).__name__}"
            )
        if value < 1:
            raise InputContractError(
                f"finalize_depth: expected value >= 1, got {value}"
            )
        return value

    # ------------------------------------------------------------------ 查询

    @property
    def subsidy(self) -> int:
        return self._subsidy

    @property
    def max_blocks(self) -> int:
        return self._max_blocks

    @property
    def finalize_depth(self) -> int | None:
        return self._finalize_depth

    @property
    def is_empty(self) -> bool:
        return self._tip_id is None

    @property
    def tip_id(self) -> str | None:
        return self._tip_id

    @property
    def tip(self) -> Block | None:
        return None if self._tip_id is None else self._nodes[self._tip_id].block

    @property
    def height(self) -> int:
        """活跃链高度（块数；空树为 0；含已裁剪脊链）。"""
        if self._tip_id is None:
            return 0
        return self._nodes[self._tip_id].height

    @property
    def cumulative_work(self) -> int:
        return 0 if self._tip_id is None else self._nodes[self._tip_id].cumulative_work

    @property
    def active_path(self) -> tuple[str, ...]:
        """活跃链区块 ID，自创世（或锚点——启用裁剪时更早块已被裁剪）至 tip。"""
        if self._tip_id is None:
            return ()
        return self._path_to(self._tip_id)

    @property
    def anchor_id(self) -> str | None:
        """当前最终确认锚点块 ID（尚未产生锚点时为 ``None``）。"""
        return self._anchor_id

    @property
    def anchor_height(self) -> int | None:
        return None if self._anchor_id is None else self._nodes[self._anchor_id].height

    @property
    def finalized_height(self) -> int | None:
        """已最终确认（不可回退）的最高高度，即锚点高度；无锚点为 ``None``。"""
        return self.anchor_height

    @property
    def pruned_spine(self) -> tuple[PrunedBlock, ...]:
        """已裁剪脊链块的定位元数据（高度升序；撤销日志已删）。"""
        return tuple(self._pruned_spine)

    @property
    def window_block_ids(self) -> tuple[str, ...]:
        """当前仍保留完整数据（可重放/可取回）的窗口块 ID（含锚点与侧枝）。"""
        return tuple(sorted(self._nodes))

    def __contains__(self, block_id: object) -> bool:
        return isinstance(block_id, str) and block_id in self._nodes

    def contains(self, block_id: str) -> bool:
        return block_id in self._nodes

    def get_block(self, block_id: str) -> Block:
        if not isinstance(block_id, str):
            raise InputContractError(
                f"get_block: block_id must be str, got {type(block_id).__name__}"
            )
        try:
            return self._nodes[block_id].block
        except KeyError:
            raise KeyError(block_id) from None

    def is_active(self, block_id: str) -> bool:
        if block_id not in self._nodes:
            return False
        return block_id in set(self.active_path)

    def snapshot_utxos(self) -> dict[OutPoint, UTXO]:
        """活跃链 tip 处的 UTXO 集合副本（修改返回值不影响账本）。"""
        return self._state.utxos.copy()

    # ------------------------------------------------------------------ 提交

    def submit(self, block: Block) -> SubmitOutcome:
        """提交一个区块，返回 :class:`SubmitOutcome`（联合状态）。

        返回：
          * ``status="accepted"``：区块入树（无论是否成为 tip），
            ``outcome.result`` 为传统 :class:`SubmitResult`；
          * ``status="requires_history"``：父块/祖先在已裁剪的最终确认历史中，
            深度重组缺历史，区块未入树、状态零变更。

        异常：
          * 父块完全缺失 → :class:`OrphanBlockError`；
          * 超出块界 → :class:`ChainLimitError`（预算耗尽）；
          * 祖先状态下证伪 → :class:`BlockValidationError`（证明无解/非法）；
          * ID 重复 → :class:`DuplicateBlockError`。

        任何失败/缺历史都不会造成部分变更。成功且新链 work 胜出时原子完成重组。
        """
        if not isinstance(block, Block):
            raise InputContractError(
                f"submit: expected Block, got {type(block).__name__}"
            )

        if block.block_id in self._nodes or self._is_pruned_id(block.block_id):
            raise DuplicateBlockError(block.block_id)

        if block.parent_id is None:
            if self._nodes:
                # 创世仍在窗口内：第二个无父块可确定性证伪。
                genesis_in_window = any(
                    n.parent_id is None for n in self._nodes.values()
                )
                if not genesis_in_window:
                    # 创世已随检查点裁剪：无法判定新无父块与旧历史的关系。
                    return self._requires_history(block.block_id, None)
                raise _ve(
                    "multiple-genesis",
                    "a parent-less block can only be the first (genesis) block; "
                    "genesis already exists",
                    block=block,
                )
            parent_node = None
        else:
            parent_node = self._nodes.get(block.parent_id)
            if parent_node is None:
                if self._anchor_id is not None and self._is_pruned_id(
                    block.parent_id
                ):
                    return self._requires_history(block.block_id, block.parent_id)
                raise OrphanBlockError(block.block_id, block.parent_id)
            # 父块在窗口内：其祖先链必须能连通锚点，否则缺的是更深的历史。
            missing = self._missing_below_window(parent_node)
            if missing is not None:
                anchor_id = self._anchor_id
                assert anchor_id is not None
                if self._is_pruned_id(missing):
                    return self._requires_history(block.block_id, missing)
                raise OrphanBlockError(parent_node.block_id, missing)

        height = 1 if parent_node is None else parent_node.height + 1
        if height > self._max_blocks:
            # 预算/有界性拒绝：不做“合法与否”的判定，与证伪严格分开。
            raise ChainLimitError(block.block_id, height, self._max_blocks)

        candidate = _Node(
            block=block,
            parent_id=block.parent_id,
            height=height,
            cumulative_work=(
                block.work if parent_node is None
                else parent_node.cumulative_work + block.work
            ),
        )

        # 1) 以祖先状态验证：从锚点（或创世）重放父链，再应用本块（临时状态）。
        ancestor_path = (
            () if parent_node is None
            else self._path_to(parent_node.block_id)
        )
        ancestor_state = self._replay_window(ancestor_path)
        _apply_block_to_state(ancestor_state, block, height, self._subsidy)

        # 2) 选链：全部已验证节点 + 候选节点，累计 work 降序、ID 字典序升序。
        new_tip_id = self._select_tip(candidate)

        # 3) 需要切换时，在临时状态重放整条新链（撤销旧链 + 应用新链）。
        new_state: _UtxoState | None = None
        reorg: ReorgPath | None = None
        old_tip_id = self._tip_id
        if new_tip_id != old_tip_id:
            new_state = self._replay_window(
                self._path_to(new_tip_id, candidate), candidate
            )
            if old_tip_id is not None:
                fork_id, deactivated, activated = self._reorg_paths(
                    old_tip_id, new_tip_id, candidate
                )
                if deactivated:
                    # 同链向前延伸（无撤销块）不是重组。
                    reorg = ReorgPath(
                        fork_id=fork_id,
                        old_tip=old_tip_id,
                        new_tip=new_tip_id,
                        deactivated=deactivated,
                        activated=activated,
                    )

        # 4) 原子提交：以上全部成功后才修改自身状态。
        self._nodes[block.block_id] = candidate
        if new_tip_id != old_tip_id:
            assert new_state is not None
            self._state.commit(new_state)
            self._tip_id = new_tip_id

        result = SubmitResult(
            block_id=block.block_id,
            block_height=self._nodes[block.block_id].height,
            tip_id=self._tip_id,  # type: ignore[arg-type]
            tip_height=self._nodes[self._tip_id].height if self._tip_id else 0,
            cumulative_work=(
                self._nodes[self._tip_id].cumulative_work if self._tip_id else 0
            ),
            became_tip=new_tip_id == block.block_id,
            reorganized=reorg is not None,
            reorg=reorg,
        )

        # 5) 最终确认：沿活跃链单调前移锚点、保存检查点状态并裁剪撤销日志。
        self._advance_finality()

        return SubmitOutcome(status="accepted", result=result)

    # -------------------------------------------------------- 最终确认/裁剪

    def _is_pruned_id(self, block_id: str) -> bool:
        return block_id in self._pruned_ids

    def _requires_history(
        self, block_id: str, missing_parent_id: str | None
    ) -> SubmitOutcome:
        anchor_id = self._anchor_id
        assert anchor_id is not None
        return SubmitOutcome(
            status="requires_history",
            history=HistoryRequired(
                block_id=block_id,
                missing_parent_id=missing_parent_id,
                anchor_id=anchor_id,
                anchor_height=self._nodes[anchor_id].height,
                finalize_depth=self._finalize_depth,
            ),
        )

    def _missing_below_window(self, node: _Node) -> str | None:
        """若 ``node`` 的祖先链不能连通当前锚点，返回断链处缺失的父 ID。"""
        if self._anchor_id is None:
            return None
        current = node
        seen: set[str] = set()
        while current.block_id != self._anchor_id:
            if current.block_id in seen:
                return current.parent_id  # 理论上不可达：无环由提交顺序保证
            seen.add(current.block_id)
            pid = current.parent_id
            if pid is None:
                return pid
            parent = self._nodes.get(pid)
            if parent is None:
                return pid
            current = parent
        return None

    def _advance_finality(self) -> None:
        """按最终确认深度前移锚点，并裁剪新锚点之前/之外的撤销日志。"""
        k = self._finalize_depth
        if k is None or self._tip_id is None:
            return
        path = self._path_to(self._tip_id)
        target_index = len(path) - 1 - k
        if target_index < 0:
            return  # 还没有任何块取得 K 个确认。
        new_anchor_id = path[target_index]
        if self._anchor_id is not None:
            if new_anchor_id == self._anchor_id:
                return
            if self._nodes[new_anchor_id].height <= self._nodes[
                self._anchor_id
            ].height:
                return  # 锚点单调不前移（例如重组到更短链）。

        # 从当前锚点（或创世）重放到新锚点，得到其检查点状态（全新副本）。
        new_anchor_state = self._replay_window(path[: target_index + 1])

        # 记录新被裁剪的活跃脊块元数据：首次锚定时为创世（含）到新锚点
        # （不含）；之后为旧锚点（含）到新锚点（不含）。
        if self._anchor_id is None:
            pruned_path = path[:target_index]
        else:
            old_index = path.index(self._anchor_id)
            pruned_path = path[old_index:target_index]
        for pid in pruned_path:
            node = self._nodes[pid]
            self._pruned_spine.append(
                PrunedBlock(
                    block_id=node.block_id,
                    parent_id=node.parent_id,
                    height=node.height,
                    cumulative_work=node.cumulative_work,
                )
            )

        # 只保留自新锚点可达的块（锚点子树）；其余（更早的失败侧枝、锚点之前
        # 与锚点不同枝的块）永久无关，一并裁剪，ID 留作重复提交检测。
        children: dict[str, list[str]] = {}
        for bid, node in self._nodes.items():
            if node.parent_id is not None:
                children.setdefault(node.parent_id, []).append(bid)
        reachable = {new_anchor_id}
        stack = [new_anchor_id]
        while stack:
            cur = stack.pop()
            for child_id in children.get(cur, ()):  # type: ignore[arg-type]
                if child_id not in reachable:
                    reachable.add(child_id)
                    stack.append(child_id)
        dropped = [bid for bid in self._nodes if bid not in reachable]
        self._pruned_ids.update(dropped)
        for dropped_id in dropped:
            del self._nodes[dropped_id]

        self._anchor_id = new_anchor_id
        self._anchor_state = new_anchor_state

    # -------------------------------------------------------------- 检查点

    def checkpoint_bytes(self) -> bytes:
        """导出当前最终确认检查点为自描述字节流。

        要求已启用 ``finalize_depth`` 且活跃链已经产生至少一个锚点
        （链上有块取得 K 个确认）；否则抛 :class:`LedgerStateError`。
        """
        if self._finalize_depth is None:
            raise LedgerStateError(
                "finalize_depth is not enabled; no finality checkpoint is kept"
            )
        if self._anchor_id is None or self._anchor_state is None:
            raise LedgerStateError(
                "no finalized anchor yet: submit more blocks beyond "
                "finalize_depth before saving a checkpoint"
            )
        anchor_node = self._nodes[self._anchor_id]
        blocks = sorted(
            (self._block_to_dict(node.block) for node in self._nodes.values()),
            key=lambda d: d["block_id"],
        )
        payload = {
            "magic": _ckpt.MAGIC,
            "version": _ckpt.VERSION,
            "params": {
                "subsidy": self._subsidy,
                "max_blocks": self._max_blocks,
                "finalize_depth": self._finalize_depth,
            },
            "anchor": {
                "block_id": anchor_node.block_id,
                "parent_id": anchor_node.parent_id,
                "height": anchor_node.height,
                "cumulative_work": anchor_node.cumulative_work,
            },
            "anchor_state": _state_parts(self._anchor_state),
            "spine": [
                {
                    "block_id": p.block_id,
                    "parent_id": p.parent_id,
                    "height": p.height,
                    "cumulative_work": p.cumulative_work,
                }
                for p in self._pruned_spine
            ],
            "pruned_ids": sorted(self._pruned_ids),
            "tip_id": self._tip_id,
            "blocks": blocks,
        }
        payload["fingerprint"] = _ckpt.fingerprint(self._fingerprint_parts(payload))
        return _ckpt.encode_payload(payload)

    @classmethod
    def from_checkpoint(
        cls,
        data: bytes | bytearray,
        *,
        subsidy: int | None = None,
        max_blocks: int | None = None,
        finalize_depth: int | None = None,
    ) -> "ReorgLedger":
        """从 :meth:`checkpoint_bytes` 的字节流恢复账本。

        可选参数给定时必须与检查点记录一致（否则 ``CheckpointCorruptError``）；
        不给定则采用检查点内参数。任何损坏/不一致都抛
        :class:`CheckpointCorruptError`，不会返回半成品对象。恢复后可直接继续
        :meth:`submit` 接块。
        """
        payload = _ckpt.decode_payload(data)
        try:
            ledger = cls._restore(payload, subsidy, max_blocks, finalize_depth)
        except CheckpointCorruptError:
            raise
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise CheckpointCorruptError(
                "bad-structure", f"malformed checkpoint payload: {exc}"
            ) from None
        return ledger

    @classmethod
    def _restore(
        cls,
        payload: dict,
        subsidy: int | None,
        max_blocks: int | None,
        finalize_depth: int | None,
    ) -> "ReorgLedger":
        required = {
            "magic", "version", "params", "anchor", "anchor_state",
            "spine", "pruned_ids", "tip_id", "blocks", "fingerprint",
        }
        if not required.issubset(payload.keys()):
            raise CheckpointCorruptError(
                "bad-structure",
                f"payload missing keys: {sorted(required - set(payload.keys()))}",
            )
        if payload["magic"] != _ckpt.MAGIC:
            raise CheckpointCorruptError(
                "bad-magic", f"unexpected magic {payload['magic']!r}"
            )
        if payload["version"] != _ckpt.VERSION:
            raise CheckpointCorruptError(
                "bad-version", f"unsupported checkpoint version {payload['version']!r}"
            )

        params = payload["params"]
        p_subsidy = cls._restore_int(params, "subsidy", min_value=0)
        p_max_blocks = cls._restore_int(params, "max_blocks", min_value=1)
        p_depth = cls._restore_int(params, "finalize_depth", min_value=1)
        if subsidy is not None and subsidy != p_subsidy:
            raise CheckpointCorruptError(
                "param-mismatch",
                f"subsidy argument {subsidy} != checkpoint subsidy {p_subsidy}",
            )
        if max_blocks is not None and max_blocks != p_max_blocks:
            raise CheckpointCorruptError(
                "param-mismatch",
                f"max_blocks argument {max_blocks} != checkpoint {p_max_blocks}",
            )
        if finalize_depth is not None and finalize_depth != p_depth:
            raise CheckpointCorruptError(
                "param-mismatch",
                f"finalize_depth argument {finalize_depth} != checkpoint {p_depth}",
            )

        ledger = cls(
            p_subsidy, p_max_blocks, finalize_depth=p_depth
        )

        # --- 脊链元数据：连续性/高度自洽 ---
        spine_raw = payload["spine"]
        if not isinstance(spine_raw, list):
            raise CheckpointCorruptError("bad-structure", "spine must be an array")
        expect_parent: str | None = None
        prev_height = 0
        prev_cwork = 0
        for i, item in enumerate(spine_raw):
            bid, pid, h, cw = cls._spine_entry(item, f"spine[{i}]")
            if i == 0:
                if pid is not None or h != 1:
                    raise CheckpointCorruptError(
                        "bad-spine",
                        "first pruned block must be genesis (height 1, "
                        "parent_id=null)",
                    )
                if cw < 1:
                    raise CheckpointCorruptError(
                        "bad-spine", "genesis cumulative work must be >= 1"
                    )
            elif pid != expect_parent or h != prev_height + 1:
                raise CheckpointCorruptError(
                    "bad-spine",
                    f"spine not contiguous at height {h}: parent={pid!r}, "
                    f"expected {expect_parent!r}",
                )
            elif cw < prev_cwork + 1:  # 每块 work>=1，累计 work 必须严格递增
                raise CheckpointCorruptError(
                    "bad-spine",
                    f"spine cumulative work not strictly increasing at {bid!r}",
                )
            ledger._pruned_spine.append(PrunedBlock(bid, pid, h, cw))
            expect_parent, prev_height, prev_cwork = bid, h, cw
        pruned_ids_raw = payload["pruned_ids"]
        if not isinstance(pruned_ids_raw, list) or not all(
            isinstance(x, str) and x for x in pruned_ids_raw
        ):
            raise CheckpointCorruptError(
                "bad-structure", "pruned_ids must be an array of non-empty strings"
            )
        ledger._pruned_ids = set(pruned_ids_raw)
        spine_ids = {p.block_id for p in ledger._pruned_spine}
        if not spine_ids.issubset(ledger._pruned_ids):
            raise CheckpointCorruptError(
                "bad-spine", "every spine block must also appear in pruned_ids"
            )

        # --- 锚点 ---
        anchor_raw = payload["anchor"]
        a_id, a_parent, a_height, a_cw = cls._spine_entry(anchor_raw, "anchor")
        if ledger._pruned_spine:
            tail = ledger._pruned_spine[-1]
            if a_parent != tail.block_id or a_height != tail.height + 1:
                raise CheckpointCorruptError(
                    "bad-anchor",
                    f"anchor does not continue pruned spine: parent={a_parent!r}, "
                    f"expected {tail.block_id!r}",
                )
        elif a_parent is not None or a_height != 1:
            raise CheckpointCorruptError(
                "bad-anchor",
                "anchor without pruned spine must be genesis at height 1",
            )
        anchor_block = cls._block_from_dict(
            cls._find_block_dict(payload["blocks"], a_id), "anchor block"
        )
        if (
            anchor_block.parent_id != a_parent
            or anchor_block.block_id != a_id
        ):
            raise CheckpointCorruptError(
                "bad-anchor", "anchor metadata does not match anchor block data"
            )
        ledger._anchor_id = a_id
        ledger._anchor_state = _state_from_parts(
            payload["anchor_state"], "anchor_state"
        )

        # --- 窗口块：重建节点，并从锚点逐块独立重放验证 ---
        blocks_raw = payload["blocks"]
        if not isinstance(blocks_raw, list):
            raise CheckpointCorruptError("bad-structure", "blocks must be an array")
        restored: dict[str, Block] = {}
        for i, item in enumerate(blocks_raw):
            blk = cls._block_from_dict(item, f"blocks[{i}]")
            if blk.block_id in restored:
                raise CheckpointCorruptError(
                    "bad-blocks", f"duplicate block id {blk.block_id!r} in window"
                )
            restored[blk.block_id] = blk
        if a_id not in restored:
            raise CheckpointCorruptError(
                "bad-anchor", "anchor block is missing from window blocks"
            )

        # 自洽元数据 + 逐块验证（按高度，父块须在锚点或已恢复窗口内）。
        pending = dict(restored)
        nodes: dict[str, _Node] = {}
        anchor_node = _Node(
            anchor_block, a_parent, a_height,
            anchor_block.work if not spine_raw else
            ledger._pruned_spine[-1].cumulative_work + anchor_block.work,
        )
        if anchor_node.cumulative_work != a_cw:
            raise CheckpointCorruptError(
                "bad-anchor", "anchor cumulative work is inconsistent"
            )
        nodes[a_id] = anchor_node
        del pending[a_id]

        progress = True
        while progress and pending:
            progress = False
            for bid in list(pending):
                blk = pending[bid]
                parent = nodes.get(blk.parent_id) if blk.parent_id is not None else None
                if blk.parent_id is None or parent is None:
                    continue
                node = _Node(
                    block=blk,
                    parent_id=blk.parent_id,
                    height=parent.height + 1,
                    cumulative_work=parent.cumulative_work + blk.work,
                )
                if node.height > p_max_blocks:
                    raise CheckpointCorruptError(
                        "bad-blocks",
                        f"restored block {bid!r} height {node.height} exceeds "
                        f"max_blocks {p_max_blocks}",
                    )
                # 从锚点沿父链重放到本块（含本块）；任何证伪都意味着
                # 检查点内容与锚点状态不一致。
                try:
                    ledger._replay_window_from(nodes, node, candidate=node)
                except BlockValidationError as exc:
                    raise CheckpointCorruptError(
                        "bad-replay",
                        f"restored block {bid!r} fails replay from anchor: "
                        f"{exc.reason}",
                    ) from None
                nodes[bid] = node
                del pending[bid]
                progress = True
        if pending:
            raise CheckpointCorruptError(
                "bad-blocks",
                "window blocks do not form a connected tree from the anchor: "
                f"{sorted(pending)[:5]}{' ...' if len(pending) > 5 else ''}",
            )
        ledger._nodes = nodes

        # --- tip：必须在窗口内，重放状态与锚点状态指纹一致 ---
        tip_id = payload["tip_id"]
        if not isinstance(tip_id, str) or tip_id not in nodes:
            raise CheckpointCorruptError(
                "bad-tip", f"tip_id {tip_id!r} is not a restored window block"
            )
        tip_state = ledger._replay_window_from(nodes, nodes[tip_id])
        ledger._state = tip_state
        ledger._tip_id = tip_id

        stored_fp = payload["fingerprint"]
        if not isinstance(stored_fp, str):
            raise CheckpointCorruptError("bad-fingerprint", "fingerprint must be str")
        expected_fp = _ckpt.fingerprint(ledger._fingerprint_parts(payload))
        if stored_fp != expected_fp:
            raise CheckpointCorruptError(
                "bad-fingerprint",
                "checkpoint fingerprint mismatch (payload is internally "
                "inconsistent or tampered)",
            )
        return ledger

    # --------------------------------------------------------- 序列化辅助

    @staticmethod
    def _restore_int(params: object, key: str, *, min_value: int) -> int:
        if not isinstance(params, dict):
            raise CheckpointCorruptError("bad-structure", "params must be an object")
        value = params.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise CheckpointCorruptError(
                "bad-structure", f"params.{key} must be an int"
            )
        if value < min_value:
            raise CheckpointCorruptError(
                "bad-value", f"params.{key} out of range: {value}"
            )
        return value

    @staticmethod
    def _spine_entry(item: object, where: str) -> tuple[str, str | None, int, int]:
        if not isinstance(item, dict):
            raise CheckpointCorruptError("bad-structure", f"{where} must be an object")
        bid = item.get("block_id")
        pid = item.get("parent_id")
        height = item.get("height")
        cwork = item.get("cumulative_work")
        if not isinstance(bid, str) or not bid:
            raise CheckpointCorruptError("bad-structure", f"{where}.block_id invalid")
        if pid is not None and not isinstance(pid, str):
            raise CheckpointCorruptError(
                "bad-structure", f"{where}.parent_id invalid"
            )
        for name, val in (("height", height), ("cumulative_work", cwork)):
            if isinstance(val, bool) or not isinstance(val, int):
                raise CheckpointCorruptError(
                    "bad-structure", f"{where}.{name} must be int"
                )
        if height < 1:
            raise CheckpointCorruptError(
                "bad-value", f"{where}.height must be >= 1, got {height}"
            )
        return bid, pid, height, cwork

    @staticmethod
    def _find_block_dict(blocks: object, block_id: str) -> dict:
        if not isinstance(blocks, list):
            raise CheckpointCorruptError("bad-structure", "blocks must be an array")
        for item in blocks:
            if isinstance(item, dict) and item.get("block_id") == block_id:
                return item
        raise CheckpointCorruptError(
            "bad-blocks", f"block data for {block_id!r} not found"
        )

    @staticmethod
    def _block_to_dict(block: Block) -> dict:
        return {
            "block_id": block.block_id,
            "parent_id": block.parent_id,
            "work": block.work,
            "transactions": [
                {
                    "tx_id": tx.tx_id,
                    "inputs": [[tin.prev_tx, tin.prev_index] for tin in tx.inputs],
                    "outputs": [[out.amount, out.lock_height]
                                for out in tx.outputs],
                }
                for tx in block.transactions
            ],
        }

    @staticmethod
    def _block_from_dict(item: object, where: str) -> Block:
        if not isinstance(item, dict):
            raise CheckpointCorruptError("bad-structure", f"{where} must be an object")
        bid = item.get("block_id")
        pid = item.get("parent_id")
        work = item.get("work")
        txs_raw = item.get("transactions")
        if not isinstance(bid, str) or not bid:
            raise CheckpointCorruptError("bad-structure", f"{where}.block_id invalid")
        if pid is not None and not isinstance(pid, str):
            raise CheckpointCorruptError(
                "bad-structure", f"{where}.parent_id invalid"
            )
        if pid == bid:
            raise CheckpointCorruptError("bad-blocks", f"{bid!r} is its own parent")
        if isinstance(work, bool) or not isinstance(work, int) or work < 1:
            raise CheckpointCorruptError(
                "bad-value", f"{where}.work must be a positive int"
            )
        if not isinstance(txs_raw, list) or not txs_raw:
            raise CheckpointCorruptError(
                "bad-structure", f"{where}.transactions must be a non-empty array"
            )
        txs: list[Transaction] = []
        for i, tx_item in enumerate(txs_raw):
            if not isinstance(tx_item, dict):
                raise CheckpointCorruptError(
                    "bad-structure", f"{where}.transactions[{i}] must be an object"
                )
            tx_id = tx_item.get("tx_id")
            inputs_raw = tx_item.get("inputs")
            outputs_raw = tx_item.get("outputs")
            if not isinstance(tx_id, str) or not tx_id:
                raise CheckpointCorruptError(
                    "bad-structure", f"{where}.transactions[{i}].tx_id invalid"
                )
            if not isinstance(inputs_raw, list) or not isinstance(outputs_raw, list):
                raise CheckpointCorruptError(
                    "bad-structure",
                    f"{where}.transactions[{i}] inputs/outputs must be arrays",
                )
            inputs: list[TxInput] = []
            for j, inp in enumerate(inputs_raw):
                if (
                    not isinstance(inp, list) or len(inp) != 2
                    or not isinstance(inp[0], str)
                    or isinstance(inp[1], bool)
                    or not isinstance(inp[1], int)
                    or inp[1] < 0
                ):
                    raise CheckpointCorruptError(
                        "bad-structure",
                        f"{where}.transactions[{i}].inputs[{j}] malformed",
                    )
                inputs.append(TxInput(inp[0], inp[1]))
            outputs: list[TxOutput] = []
            for j, out in enumerate(outputs_raw):
                if (
                    not isinstance(out, list) or len(out) != 2
                    or isinstance(out[0], bool) or not isinstance(out[0], int)
                    or isinstance(out[1], bool) or not isinstance(out[1], int)
                    or out[0] < 1 or out[1] < 0
                ):
                    raise CheckpointCorruptError(
                        "bad-structure",
                        f"{where}.transactions[{i}].outputs[{j}] malformed",
                    )
                outputs.append(TxOutput(out[0], out[1]))
            if not outputs:
                raise CheckpointCorruptError(
                    "bad-blocks",
                    f"{where}.transactions[{i}] must have at least one output",
                )
            txs.append(Transaction(tx_id, tuple(inputs), tuple(outputs)))
        return Block(bid, pid, work, tuple(txs))

    def _fingerprint_parts(self, payload: dict) -> dict:
        """指纹覆盖参数、锚点、锚点状态、脊链、tip 与窗口块集合。

        tip 状态不直接取自载荷（载荷未存），而由当前账本重放结果给出；
        恢复时用重算出的同一结构比对，因此任何语义篡改都会失配。
        """
        return {
            "params": payload["params"],
            "anchor": payload["anchor"],
            "anchor_state": payload["anchor_state"],
            "spine": payload["spine"],
            "pruned_ids": payload["pruned_ids"],
            "tip_id": payload["tip_id"],
            "tip_state": _state_parts(self._state),
            # 覆盖窗口块的**完整内容**（按 ID 规范化排序）：任何块内交易/金额
            # 篡改都会使指纹失配，即使该块不在 tip 路径上。
            "blocks": sorted(
                payload["blocks"], key=lambda d: d["block_id"]
            ),
        }

    # -------------------------------------------------------------- 内部工具

    def _path_to(
        self,
        tip_id: str,
        candidate: _Node | None = None,
        nodes: dict[str, _Node] | None = None,
        anchor_id: str | None = "default",
    ) -> tuple[str, ...]:
        """自最旧可用块（创世或锚点）至 ``tip_id`` 的 ID 元组。"""
        node_map = self._nodes if nodes is None else nodes
        anchor = self._anchor_id if anchor_id == "default" else anchor_id
        ids: list[str] = []
        current_id: str | None = tip_id
        while current_id is not None:
            node = self._lookup_in(node_map, current_id, candidate)
            ids.append(current_id)
            current_id = None if current_id == anchor else node.parent_id
        ids.reverse()
        return tuple(ids)

    @staticmethod
    def _lookup_in(
        node_map: dict[str, _Node],
        node_id: str,
        candidate: _Node | None,
    ) -> _Node:
        if candidate is not None and node_id == candidate.block.block_id:
            return candidate
        return node_map[node_id]

    def _replay_window(
        self,
        path_ids: tuple[str, ...],
        candidate: _Node | None = None,
    ) -> _UtxoState:
        """从锚点状态（无锚点则空状态）重放给定路径，返回全新状态。

        有锚点时 ``path_ids`` 必须以锚点开头（锚点本身不重复应用）；无锚点时
        路径自创世开始、逐块应用。
        """
        return self._replay_window_from(self._nodes, None, path_ids, candidate)

    def _replay_window_from(
        self,
        node_map: dict[str, _Node],
        target: _Node | None,
        path_ids: tuple[str, ...] | None = None,
        candidate: _Node | None = None,
    ) -> _UtxoState:
        """``_replay_window`` 的可注入节点表版本（恢复期间节点表尚未提交）。"""
        if path_ids is None:
            assert target is not None
            path_ids = self._path_to(
                target.block_id, candidate, nodes=node_map,
                anchor_id=self._anchor_id,
            )
        state = (
            self._anchor_state.copy() if self._anchor_state is not None
            else _UtxoState()
        )
        start_apply = 1 if self._anchor_state is not None else 0
        expected_height = (
            node_map[self._anchor_id].height
            if self._anchor_state is not None else 0
        )
        for index, node_id in enumerate(path_ids):
            node = self._lookup_in(node_map, node_id, candidate)
            if index < start_apply:
                if node_id != self._anchor_id:
                    raise LedgerStateError(
                        f"internal error: replay path must start at anchor, "
                        f"got {node_id!r}"
                    )
                continue
            expected_height += 1
            if node.height != expected_height:
                raise LedgerStateError(
                    f"internal error: height mismatch replaying {node_id!r}"
                )
            _apply_block_to_state(
                state, node.block, node.height, self._subsidy
            )
        return state

    def _select_tip(self, candidate: _Node) -> str:
        """累计 work 最大；同分取区块 ID 字典序较小者。

        候选区块加入前，旧树的活跃 tip 已是旧树最优；候选只能让其自身成为
        新 tip，因此只需与旧 tip 用 ``(累计work 降序, ID 升序)`` 键比较。
        """
        if self._tip_id is None:
            return candidate.block.block_id
        best_id = self._tip_id
        best_node = self._nodes[best_id]
        candidate_key = (-candidate.cumulative_work, candidate.block.block_id)
        best_key = (-best_node.cumulative_work, best_id)
        if candidate_key < best_key:
            return candidate.block.block_id
        return best_id

    def _reorg_paths(
        self,
        old_tip_id: str,
        new_tip_id: str,
        candidate: _Node,
    ) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
        old_path = self._path_to(old_tip_id)
        new_path = self._path_to(new_tip_id, candidate)
        old_set = set(old_path)
        fork_index = max(i for i, bid in enumerate(new_path) if bid in old_set)
        fork_id = new_path[fork_index]
        deactivated = tuple(
            bid for bid in reversed(old_path) if bid not in set(new_path)
        )
        activated = tuple(bid for bid in new_path[fork_index + 1:])
        return fork_id, deactivated, activated
