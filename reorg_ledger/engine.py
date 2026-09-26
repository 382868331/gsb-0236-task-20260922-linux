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
    BlockValidationError,
    ChainLimitError,
    DuplicateBlockError,
    InputContractError,
    LedgerStateError,
    OrphanBlockError,
)
from .models import Block, OutPoint, Transaction, UTXO

__all__ = [
    "DEFAULT_SUBSIDY",
    "MAX_CHAIN_BLOCKS",
    "ReorgPath",
    "SubmitResult",
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


def _check_asset_rules(
    tx: "Transaction",
    input_utxos: list[UTXO],
    *,
    block: Block,
    height: int,
) -> None:
    """单笔交易的多资产守恒与发行授权规则（协议语义校验）。

    对每一种资产独立成立：

    * 守恒：输入资产 + 本次铸造 == 输出资产 + 显式销毁（burns）。
      短缺不能用别的资产补（asset-exceeds-input）；未声明的销毁不允许
      （asset-burn-undeclared）。
    * 铸造必须消费被铸资产的发行授权 UTXO（mint-without-authority）。
    * 发行：铸出数量 + 后继授权剩余额度 == 被消费授权的原额度
      （allowance-mismatch）；后继授权至多一个（multiple-successors），
      因此授权不可复制。
    * 创设：授权输出的 issue_asset 必须等于 "<tx_id>:<输出下标>"，且只能
      出现在有普通输入的交易里（issue-asset-mismatch / issue-in-coinbase）。
    """
    in_assets: dict[str, int] = {}
    consumed_auth: dict[str, int] = {}
    for utxo in input_utxos:
        for aid, amount in utxo.assets:
            in_assets[aid] = in_assets.get(aid, 0) + amount
        if utxo.issue_asset is not None:
            if utxo.issue_asset in consumed_auth:
                raise _ve(
                    "duplicate-authority",
                    f"issuance authority for asset {utxo.issue_asset!r} is "
                    f"consumed twice in the same transaction",
                    block=block, height=height, tx=tx,
                )
            consumed_auth[utxo.issue_asset] = utxo.issue_allowance  # type: ignore[arg-type]

    out_assets: dict[str, int] = {}
    successors: dict[str, tuple[int, int]] = {}
    for out_index, out in enumerate(tx.outputs):
        for aid, amount in out.assets:
            out_assets[aid] = out_assets.get(aid, 0) + amount
        if out.issue_asset is None:
            continue
        derived = f"{tx.tx_id}:{out_index}"
        if out.issue_asset == derived:
            # 创设新授权：资产 ID 由本交易 ID 与输出位置唯一确定，
            # 不能指定别人的 asset_id；且必须从普通基础币输入建立。
            if tx.is_coinbase:
                raise _ve(
                    "issue-in-coinbase",
                    "an issuance authority can only be created by a "
                    "transaction spending ordinary base-coin inputs, "
                    "not by a coinbase",
                    block=block, height=height, tx=tx,
                )
            if out.issue_allowance < 1:  # type: ignore[operator]
                raise _ve(
                    "issue-allowance",
                    f"new issuance authority for {out.issue_asset!r} must "
                    f"start with allowance >= 1",
                    block=block, height=height, tx=tx,
                )
        elif out.issue_asset in consumed_auth:
            # 发行后继授权：延续同一资产，至多一个。
            if out.issue_asset in successors:
                raise _ve(
                    "multiple-successors",
                    f"more than one successor authority for asset "
                    f"{out.issue_asset!r} (authority cannot be duplicated)",
                    block=block, height=height, tx=tx,
                )
            successors[out.issue_asset] = (out_index, out.issue_allowance)  # type: ignore[assignment]
        else:
            raise _ve(
                "issue-asset-mismatch",
                f"output #{out_index} declares authority for asset "
                f"{out.issue_asset!r}, which is neither this transaction's "
                f"own derived id {derived!r} nor an authority consumed by "
                f"this transaction",
                block=block, height=height, tx=tx,
            )

    burns: dict[str, int] = {}
    for aid, amount in tx.burns:
        burns[aid] = burns.get(aid, 0) + amount

    asset_ids = (
        set(in_assets) | set(out_assets) | set(burns) | set(consumed_auth)
    )
    for aid in sorted(asset_ids):
        in_amt = in_assets.get(aid, 0)
        out_amt = out_assets.get(aid, 0)
        burn_amt = burns.get(aid, 0)
        minted = out_amt + burn_amt - in_amt
        if aid in consumed_auth:
            if minted < 0:
                raise _ve(
                    "asset-burn-undeclared",
                    f"asset {aid!r}: inputs exceed outputs plus declared "
                    f"burns by {-minted} (destruction must be declared "
                    f"explicitly)",
                    block=block, height=height, tx=tx,
                )
            successor_allowance = (
                successors[aid][1] if aid in successors else 0
            )
            if minted + successor_allowance != consumed_auth[aid]:
                raise _ve(
                    "allowance-mismatch",
                    f"asset {aid!r}: minted {minted} + successor allowance "
                    f"{successor_allowance} != consumed authority allowance "
                    f"{consumed_auth[aid]} (allowance must be fully "
                    f"accounted for)",
                    block=block, height=height, tx=tx,
                )
        else:
            if minted > 0:
                raise _ve(
                    "mint-without-authority",
                    f"asset {aid!r}: outputs+burns exceed inputs by {minted} "
                    f"but no issuance authority for this asset is consumed",
                    block=block, height=height, tx=tx,
                )
            if minted < 0:
                raise _ve(
                    "asset-burn-undeclared",
                    f"asset {aid!r}: inputs exceed outputs plus declared "
                    f"burns by {-minted} (destruction must be declared "
                    f"explicitly; one asset's surplus cannot cover another "
                    f"asset's deficit)",
                    block=block, height=height, tx=tx,
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
            # coinbase 无输入：任何资产/授权输出都会在这里被证伪。
            _check_asset_rules(tx, [], block=block, height=height)
            for out_index, out in enumerate(tx.outputs):
                op = OutPoint(tx.tx_id, out_index)
                if op in work_state.utxos:
                    raise _ve(
                        "duplicate-txid",
                        "coinbase output point collides with an existing UTXO",
                        block=block, height=height, tx=tx, outpoint=op,
                    )
                work_state.utxos[op] = UTXO(
                    out.amount, out.lock_height, out.assets,
                    out.issue_asset, out.issue_allowance,
                )
            work_state.applied_tx_ids.add(tx.tx_id)
            continue

        # --- 普通交易：输入唯一性 / 存在性 / 锁定高度 / 金额守恒 ---
        seen: set[OutPoint] = set()
        input_sum = 0
        resolved: list[OutPoint] = []
        resolved_utxos: list[UTXO] = []
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
            resolved_utxos.append(utxo)

        output_sum = sum(o.amount for o in tx.outputs)
        if output_sum > input_sum:
            raise _ve(
                "output-exceeds-input",
                f"output sum {output_sum} exceeds input sum {input_sum}",
                block=block, height=height, tx=tx,
            )
        total_fees += input_sum - output_sum

        # --- 多资产守恒 + 发行授权规则（逐资产独立，销毁须显式声明） ---
        _check_asset_rules(tx, resolved_utxos, block=block, height=height)

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
            work_state.utxos[op] = UTXO(
                out.amount, out.lock_height, out.assets,
                out.issue_asset, out.issue_allowance,
            )
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
        self._subsidy = subsidy
        self._max_blocks = max_blocks
        self._nodes: dict[str, _Node] = {}
        self._tip_id: str | None = None
        self._state = _UtxoState()

    # ------------------------------------------------------------------ 查询

    @property
    def subsidy(self) -> int:
        return self._subsidy

    @property
    def max_blocks(self) -> int:
        return self._max_blocks

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

    def submit(self, block: Block) -> SubmitResult:
        """提交一个区块。

        * 父块缺失 → :class:`OrphanBlockError`（既未证明合法也未证明非法）。
        * 超出 100 块界 → :class:`ChainLimitError`（预算耗尽）。
        * 祖先状态下证伪 → :class:`BlockValidationError`（证明无解/非法）。
        * ID 重复 → :class:`DuplicateBlockError`。

        任何失败都不会造成部分变更。成功且新链 work 胜出时原子完成重组。
        """
        if not isinstance(block, Block):
            raise InputContractError(
                f"submit: expected Block, got {type(block).__name__}"
            )

        if block.block_id in self._nodes:
            raise DuplicateBlockError(block.block_id)

        if block.parent_id is None:
            if self._nodes:
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
                raise OrphanBlockError(block.block_id, block.parent_id)

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

        # 1) 以祖先状态验证：从创世重放父链，再应用本块（纯临时状态）。
        ancestor_state = self._replay_chain(
            () if parent_node is None else self._path_to(parent_node.block_id)
        )
        _apply_block_to_state(ancestor_state, block, height, self._subsidy)

        # 2) 选链：全部已验证节点 + 候选节点，累计 work 降序、ID 字典序升序。
        new_tip_id = self._select_tip(candidate)

        # 3) 需要切换时，先在临时状态重放整条新链（撤销旧链 + 应用新链）。
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

        return SubmitResult(
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

    # -------------------------------------------------------------- 内部工具

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
