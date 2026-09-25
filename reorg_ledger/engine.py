"""核心引擎：有界 UTXO 区块树 + 撤销账本式重组。

设计要点
--------
1. **区块树**：所有通过祖先状态验证的区块都保留在内存树中（含非活跃侧枝）。
2. **祖先状态验证**：接收区块时，从创世沿其父链重放到父块，再按序应用本块
   交易。验证在临时状态上进行，失败时引擎状态零变更。
3. **选链**：累计 ``work`` 最大；累计 work 相同取 *tip 区块 ID 字典序较小*。
4. **原子重组**：tip 改变时，先在临时状态上从创世重放整条新链，成功后一次性
   切换 tip / UTXO / 活跃路径——即“撤销旧链再应用新链”的原子提交；中途任何
   异常都保留旧状态。
5. **有界**：链长（含创世）超过 ``max_blocks``（默认 100）抛
   :class:`ChainLimitError`（预算耗尽），区别于 :class:`BlockValidationError`
   （被证明非法）。

引擎内全部金额运算均为任意精度整数，不使用浮点。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    AncestorInvalidError,
    BlockValidationError,
    BlockConflictError,
    ChainLimitError,
    InputContractError,
    LedgerStateError,
)
from .models import Block, OutPoint, Transaction, UTXO

__all__ = [
    "DEFAULT_SUBSIDY",
    "MAX_CHAIN_BLOCKS",
    "ReorgPath",
    "SubmitResult",
    "ReorgLedger",
    "DEFAULT_MAX_ORPHANS",
]

DEFAULT_SUBSIDY = 50
MAX_CHAIN_BLOCKS = 100
DEFAULT_MAX_ORPHANS = 32


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
    buffered: bool = False
    evicted_orphan_ids: tuple[str, ...] = ()


def _check_block_structure(block: Block) -> None:
    """不依赖 UTXO / 祖先状态的纯块内结构校验。

    孤块缓冲前只运行这里的检查：coinbase 位置与唯一性、普通交易内输入是否
    重复、块内交易 ID / OutPoint 是否自撞。涉及链历史的判定（输入存在性、
    锁定高度、金额守恒、跨块 txid 复用、coinbase 手续费额度）一律推迟到
    父块可达后做完整验证。
    """

    seen_tx_ids: set[str] = set()
    seen_outpoints: set[OutPoint] = set()
    coinbase: Transaction | None = None
    for index, tx in enumerate(block.transactions):
        if tx.tx_id in seen_tx_ids:
            raise _ve(
                "duplicate-txid",
                "transaction id appears more than once within the same block",
                block=block, tx=tx,
            )
        seen_tx_ids.add(tx.tx_id)

        if tx.is_coinbase:
            if coinbase is not None:
                raise _ve(
                    "multiple-coinbase",
                    "a block contains more than one coinbase transaction",
                    block=block, tx=tx,
                )
            if index != 0:
                raise _ve(
                    "coinbase-position",
                    "a zero-input (coinbase) transaction is only allowed at "
                    "transaction index 0",
                    block=block, tx=tx,
                )
            coinbase = tx

        local_inputs: set[OutPoint] = set()
        for txin in tx.inputs:
            op = txin.outpoint
            if op in local_inputs:
                raise _ve(
                    "duplicate-input",
                    "input is spent more than once within the same transaction",
                    block=block, tx=tx, outpoint=op,
                )
            local_inputs.add(op)

        for out_index in range(len(tx.outputs)):
            op = OutPoint(tx.tx_id, out_index)
            if op in seen_outpoints:
                raise _ve(
                    "duplicate-txid",
                    "output point collides within the same block "
                    "(transaction id reused)",
                    block=block, tx=tx, outpoint=op,
                )
            seen_outpoints.add(op)


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
    """从创世重放过程中的可变 UTXO 状态（内部类型，非公开契约）。"""

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
    """

    def __init__(
        self,
        subsidy: int = DEFAULT_SUBSIDY,
        max_blocks: int = MAX_CHAIN_BLOCKS,
        max_orphans: int = DEFAULT_MAX_ORPHANS,
    ) -> None:
        if isinstance(subsidy, bool):
            raise InputContractError("subsidy: bool is not accepted as an integer")
        if not isinstance(subsidy, int):
            raise InputContractError(
                f"subsidy: expected int, got {type(subsidy).__name__}"
            )
        if subsidy < 0:
            raise InputContractError(f"subsidy: expected value >= 0, got {subsidy}")
        if isinstance(max_blocks, bool):
            raise InputContractError("max_blocks: bool is not accepted as an integer")
        if not isinstance(max_blocks, int):
            raise InputContractError(
                f"max_blocks: expected int, got {type(max_blocks).__name__}"
            )
        if max_blocks < 1:
            raise InputContractError(
                f"max_blocks: expected value >= 1, got {max_blocks}"
            )
        if isinstance(max_orphans, bool):
            raise InputContractError(
                "max_orphans: bool is not accepted as an integer"
            )
        if not isinstance(max_orphans, int):
            raise InputContractError(
                f"max_orphans: expected int, got {type(max_orphans).__name__}"
            )
        if max_orphans < 0:
            raise InputContractError(
                f"max_orphans: expected value >= 0, got {max_orphans}"
            )
        self._subsidy = subsidy
        self._max_blocks = max_blocks
        self._max_orphans = max_orphans
        self._nodes: dict[str, _Node] = {}
        self._tip_id: str | None = None
        self._state = _UtxoState()
        # --- 孤块缓冲（父块未知的区块，只做过不依赖 UTXO 的结构校验）---
        # _orphans[id]   = Block
        # _orphan_children[parent_id] = 该父块的直接孤儿子块 ID 集合
        # _orphan_order  = 显式接收序（最早在前）
        # _last_results[id] = 已验证块 / 缓冲块上次 submit 的 SubmitResult（幂等返回）
        # _invalid_ancestor[id] = 导致该 ID 被否决的最近祖先 ID
        self._orphans: dict[str, Block] = {}
        self._orphan_children: dict[str, set[str]] = {}
        self._orphan_order: list[str] = []
        self._last_results: dict[str, SubmitResult] = {}
        self._invalid_ancestor: dict[str, str] = {}

    # ------------------------------------------------------------------ 查询

    @property
    def subsidy(self) -> int:
        return self._subsidy

    @property
    def max_blocks(self) -> int:
        return self._max_blocks

    @property
    def max_orphans(self) -> int:
        return self._max_orphans

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
        """活跃链高度（块数；空树为 0）。"""
        return 0 if self._tip_id is None else self._nodes[self._tip_id].height

    @property
    def cumulative_work(self) -> int:
        return 0 if self._tip_id is None else self._nodes[self._tip_id].cumulative_work

    @property
    def active_path(self) -> tuple[str, ...]:
        """活跃链区块 ID，自创世至 tip。"""
        if self._tip_id is None:
            return ()
        return self._path_to(self._tip_id)

    def __contains__(self, block_id: object) -> bool:
        return isinstance(block_id, str) and (
            block_id in self._nodes
            or block_id in self._orphans
            or block_id in self._invalid_ancestor
        )

    def contains(self, block_id: str) -> bool:
        return (
            block_id in self._nodes
            or block_id in self._orphans
            or block_id in self._invalid_ancestor
        )

    def get_block(self, block_id: str) -> Block:
        if not isinstance(block_id, str):
            raise InputContractError(
                f"get_block: block_id must be str, got {type(block_id).__name__}"
            )
        try:
            return self._nodes[block_id].block
        except KeyError:
            if block_id in self._orphans:
                return self._orphans[block_id]
            raise KeyError(block_id) from None

    def is_verified(self, block_id: str) -> bool:
        """块是否已通过祖先状态验证并入树（含非活跃侧枝）。"""
        return block_id in self._nodes

    def is_buffered_orphan(self, block_id: str) -> bool:
        """块是否在孤块缓冲区内等待缺失祖先。"""
        return block_id in self._orphans

    def is_ancestor_invalid(self, block_id: str) -> bool:
        """块是否因其祖先（或自身）被证明非法而被否决。"""
        return block_id in self._invalid_ancestor

    def invalid_ancestor_of(self, block_id: str) -> str | None:
        """否决该块的最近无效祖先 ID；未被否决时返回 None。"""
        return self._invalid_ancestor.get(block_id)

    def buffered_orphan_ids(self) -> tuple[str, ...]:
        """缓冲中的孤块 ID，按显式接收序。"""
        return tuple(self._orphan_order)

    def orphan_count(self) -> int:
        return len(self._orphans)

    def orphan_buffer_available(self) -> int:
        return self._max_orphans - len(self._orphans)

    def is_active(self, block_id: str) -> bool:
        if block_id not in self._nodes:
            return False
        return block_id in set(self.active_path)

    def snapshot_utxos(self) -> dict[OutPoint, UTXO]:
        """活跃链 tip 处的 UTXO 集合副本（修改返回值不影响账本）。"""
        return self._state.utxos.copy()

    # ------------------------------------------------------------------ 提交

    def submit(self, block: Block) -> SubmitResult:
        """提交一个区块。

        * 父块未知（孤块）：只做不依赖 UTXO 的块内结构校验，通过后放入有上限
          的待验证区（SubmitResult.buffered=True）。缓冲块不计入累计 work
          候选、不提前应用交易。父块到达并验证成功后，按块 ID 字典序递归
          接入可连通后代。
        * 缓冲满：按显式接收序淘汰最早的整棵待验证子树，被淘汰 ID 见
          SubmitResult.evicted_orphan_ids（接收序）；已验证主链/侧链不受影响。
        * 祖先最终非法：缓冲后代整棵标 ancestor_invalid，再次提交抛
          AncestorInvalidError，即使后代交易单独看似合法也不能接入。
        * 同内容区块重复提交幂等返回原结果；同 ID 异内容抛 BlockConflictError
          （DuplicateBlockError 子类）。
        * 超出块数界抛 ChainLimitError（预算耗尽）；祖先状态下证伪抛
          BlockValidationError（证明非法）。

        失败不产生未说明的部分变更；成功且新链 work 胜出时原子完成重组。
        """
        if not isinstance(block, Block):
            raise InputContractError(
                f"submit: expected Block, got {type(block).__name__}"
            )

        existing = self._nodes.get(block.block_id)
        if existing is not None:
            if existing.block != block:
                raise BlockConflictError(block.block_id)
            return self._last_results[block.block_id]
        if block.block_id in self._orphans:
            if self._orphans[block.block_id] != block:
                raise BlockConflictError(block.block_id)
            return self._last_results[block.block_id]
        if block.block_id in self._invalid_ancestor:
            raise AncestorInvalidError(
                block.block_id, self._invalid_ancestor[block.block_id]
            )

        _check_block_structure(block)

        if block.parent_id is None:
            if self._nodes:
                raise _ve(
                    "multiple-genesis",
                    "a parent-less block can only be the first (genesis) block; "
                    "genesis already exists",
                    block=block,
                )
            return self._connect(block)

        if block.parent_id in self._invalid_ancestor:
            ancestor_id = self._invalid_ancestor[block.parent_id]
            self._mark_invalid_subtree(block.block_id, ancestor_id)
            raise AncestorInvalidError(block.block_id, ancestor_id)

        if block.parent_id not in self._nodes:
            evicted = self._evict_for(1)
            self._orphans[block.block_id] = block
            self._orphan_order.append(block.block_id)
            self._orphan_children.setdefault(block.parent_id, set()).add(
                block.block_id
            )
            result = SubmitResult(
                block_id=block.block_id,
                block_height=0,
                tip_id=self._tip_id,  # type: ignore[arg-type]
                tip_height=0 if self._tip_id is None
                else self._nodes[self._tip_id].height,
                cumulative_work=self.cumulative_work,
                became_tip=False,
                reorganized=False,
                reorg=None,
                buffered=True,
                evicted_orphan_ids=tuple(evicted),
            )
            self._last_results[block.block_id] = result
            return result

        return self._connect(block)

    # -------------------------------------------------------------- 内部工具

    def _with_eviction(
        self, result: SubmitResult, evicted: list[str]
    ) -> SubmitResult:
        if not evicted:
            return result
        return SubmitResult(
            block_id=result.block_id,
            block_height=result.block_height,
            tip_id=result.tip_id,
            tip_height=result.tip_height,
            cumulative_work=result.cumulative_work,
            became_tip=result.became_tip,
            reorganized=result.reorganized,
            reorg=result.reorg,
            buffered=result.buffered,
            evicted_orphan_ids=tuple(evicted),
        )

    def _evict_for(self, needed_slots: int) -> list[str]:
        """按显式接收序淘汰最早的整棵待验证子树，直到腾出足够槽位。

        根 = 其父块不在缓冲区中的孤块（父块可能已验证或完全未知）。淘汰根
        即移除其整棵缓冲后代。返回被淘汰 ID（显式接收序）。已验证的主链与
        侧链不在缓冲中，不受影响。
        """
        evicted: list[str] = []
        limit = self._max_orphans
        while len(self._orphans) + needed_slots > limit:
            root_id = next(
                candidate for candidate in self._orphan_order
                if candidate in self._orphans
                and self._orphans[candidate].parent_id not in self._orphans
            )
            evicted.extend(self._remove_orphan_subtree(root_id))
        return evicted

    def _remove_orphan_subtree(self, root_id: str) -> list[str]:
        """从缓冲区移除以 root_id 为根的整棵子树，返回按接收序排列的 ID。"""
        removed: list[str] = []
        stack = [root_id]
        while stack:
            current = stack.pop()
            block = self._orphans.pop(current, None)
            if block is None:
                continue
            removed.append(current)
            stack.extend(
                cid for cid in sorted(self._orphan_children.get(current, ()))
                if cid in self._orphans
            )
        order_index = {
            bid: index for index, bid in enumerate(self._orphan_order)
        }
        removed.sort(key=lambda bid: order_index[bid])
        removed_set = set(removed)
        for current in removed:
            self._orphan_order.remove(current)
            self._orphan_children.pop(current, None)
            self._last_results.pop(current, None)
        for children in self._orphan_children.values():
            children -= removed_set
        return removed

    def _detach_orphan(self, block_id: str) -> None:
        """单个孤块验证通过后离开缓冲区（不触碰其缓冲后代）。"""
        if block_id not in self._orphans:
            return
        block = self._orphans.pop(block_id)
        self._orphan_order.remove(block_id)
        siblings = self._orphan_children.get(block.parent_id)
        if siblings is not None:
            siblings.discard(block_id)
        self._orphan_children.pop(block_id, None)

    def _mark_invalid_subtree(self, root_id: str, ancestor_id: str) -> None:
        """把以 root_id 为根（含根本身，若在缓冲中）的整棵后代标为无效。"""
        removed: list[str] = []
        stack = [root_id] if root_id in self._orphans else sorted(
            self._orphan_children.get(root_id, set())
        )
        while stack:
            current = stack.pop()
            if current not in self._orphans:
                continue
            self._orphans.pop(current)
            removed.append(current)
            stack.extend(sorted(self._orphan_children.get(current, set())))
        order_index = {
            bid: index for index, bid in enumerate(self._orphan_order)
        }
        removed.sort(key=lambda bid: order_index[bid])
        removed_set = set(removed)
        for current in removed:
            self._orphan_order.remove(current)
            self._orphan_children.pop(current, None)
            self._last_results.pop(current, None)
            self._invalid_ancestor[current] = ancestor_id
        for children in self._orphan_children.values():
            children -= removed_set

    def _connect(self, block: Block) -> SubmitResult:
        """验证并接入一个父块已就绪的区块（临时状态验证 + 选链 + 原子提交）。"""
        parent_id = block.parent_id
        parent_node = None if parent_id is None else self._nodes[parent_id]

        height = 1 if parent_node is None else parent_node.height + 1
        if height > self._max_blocks:
            raise ChainLimitError(block.block_id, height, self._max_blocks)

        candidate = _Node(
            block=block,
            parent_id=parent_id,
            height=height,
            cumulative_work=(
                block.work if parent_node is None
                else parent_node.cumulative_work + block.work
            ),
        )

        ancestor_state = self._replay_chain(
            () if parent_node is None else self._path_to(parent_node.block_id)
        )
        try:
            _apply_block_to_state(ancestor_state, block, height, self._subsidy)
        except BlockValidationError:
            self._mark_invalid_subtree(block.block_id, block.block_id)
            raise

        new_tip_id = self._select_tip(candidate)
        new_state: _UtxoState | None = None
        reorg: ReorgPath | None = None
        old_tip_id = self._tip_id
        if new_tip_id != old_tip_id:
            new_state = self._replay_chain(
                self._path_to(new_tip_id, candidate), candidate
            )
            if old_tip_id is not None:
                fork_id, deactivated, activated = self._reorg_paths(
                    old_tip_id, new_tip_id, candidate
                )
                if deactivated:
                    reorg = ReorgPath(
                        fork_id=fork_id,
                        old_tip=old_tip_id,
                        new_tip=new_tip_id,
                        deactivated=deactivated,
                        activated=activated,
                    )

        self._nodes[block.block_id] = candidate
        if new_tip_id != old_tip_id:
            assert new_state is not None
            self._state.commit(new_state)
            self._tip_id = new_tip_id

        if block.block_id in self._orphans:
            self._detach_orphan(block.block_id)

        result = SubmitResult(
            block_id=block.block_id,
            block_height=height,
            tip_id=self._tip_id,  # type: ignore[arg-type]
            tip_height=self._nodes[self._tip_id].height if self._tip_id else 0,
            cumulative_work=(
                self._nodes[self._tip_id].cumulative_work if self._tip_id else 0
            ),
            became_tip=new_tip_id == block.block_id,
            reorganized=reorg is not None,
            reorg=reorg,
        )
        self._last_results[block.block_id] = result

        # 缓冲关系可能在本块接入过程中变化（整棵子树移除），按剩余缓冲重建。
        children: dict[str, set[str]] = {}
        for bid, blk in self._orphans.items():
            children.setdefault(blk.parent_id, set()).add(bid)
        self._orphan_children = children
        self._connect_children(block.block_id)
        return result

    def _connect_children(self, parent_id: str) -> None:
        """按块 ID 字典序递归接入一个已验证块的缓冲直接后代。"""
        child_ids = sorted(self._orphan_children.get(parent_id, ()))
        for child_id in child_ids:
            child = self._orphans.get(child_id)
            if child is None:
                continue
            try:
                self._connect(child)
            except ChainLimitError:
                # 预算耗尽不是非法：保留该子树在缓冲区，停止本分支级联。
                break
            except BlockValidationError:
                # 子块已证明非法，其后代已在 _connect 内标记；继续兄弟分支。
                continue

    def _lookup(self, node_id: str, candidate: _Node | None) -> _Node:
        if candidate is not None and node_id == candidate.block.block_id:
            return candidate
        return self._nodes[node_id]

    def _path_to(
        self, tip_id: str, candidate: _Node | None = None
    ) -> tuple[str, ...]:
        """自创世至 ``tip_id`` 的区块 ID 元组（允许包含尚未提交的候选节点）。"""
        ids: list[str] = []
        current_id: str | None = tip_id
        while current_id is not None:
            node = self._lookup(current_id, candidate)
            ids.append(current_id)
            current_id = node.parent_id
        ids.reverse()
        return tuple(ids)

    def _replay_chain(
        self,
        path_ids: tuple[str, ...],
        candidate: _Node | None = None,
    ) -> _UtxoState:
        """从空状态按给定创世→tip 路径重放，返回全新状态。"""
        state = _UtxoState()
        expected_height = 0
        for node_id in path_ids:
            node = self._lookup(node_id, candidate)
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
