"""内存区块树与有界重组账本（核心模块）。

公开接口
========

:class:`BlockTree` 是唯一有状态对象：

* ``add_block(block) -> Reorg``：静态校验 → 查重 / parent 检查 →
  **从祖先状态（创世重放）验证整链** → tip 选择 → 必要时原子重组。
  任何失败都不会留下部分变更。
* ``tip`` / ``active_chain_ids`` / ``utxos``：当前最佳链视图。
* ``choose_tip(budget=...)``：显式 tip 搜索，树空时抛
  :class:`~utxoledger.errors.NoSolutionError`，访问节点超预算时抛
  :class:`~utxoledger.errors.BudgetExhaustedError`，二者严格区分。

选择规则：累计 work（链上各块正整数 work 之和）最大；完全同分时取
block_id 字典序最小者。

重组：撤销旧链（old tip → 共同祖先）再应用新链（共同祖先 → new tip），
全程在活动状态的临时副本上执行，全部成功后一次性提交。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import (
    BudgetExhaustedError,
    ChainLimitError,
    DuplicateBlockError,
    InvalidBlockError,
    NoSolutionError,
    UnknownParentError,
)
from .state import Journal, apply_block, undo_block
from .types import Block, OutPoint, Output, Reorg, UTXO
from .validation import MAX_CHAIN_BLOCKS, require_int, validate_block

#: coinbase 固定补贴（整数；模型内不随高度衰减）。
DEFAULT_COINBASE_SUBSIDY = 50

#: tip 搜索默认节点访问预算。
DEFAULT_SEARCH_BUDGET = 1_000_000


@dataclass(slots=True)
class _Node:
    block: Block
    height: int  # 创世为 0
    cum_work: int


class BlockTree:
    """内存区块树 + 活动最佳链的 UTXO 账本。

    :param subsidy: coinbase 固定补贴，非负整数。
    :param max_chain_blocks: 单条链块数上限（含创世），默认 100。
    :param search_budget: tip 搜索的节点访问预算（每次 add_block 内部
        全树枚举一次）；耗尽抛 :class:`BudgetExhaustedError`，不代表无解。
    """

    def __init__(self, *, subsidy: int = DEFAULT_COINBASE_SUBSIDY,
                 max_chain_blocks: int = MAX_CHAIN_BLOCKS,
                 search_budget: int = DEFAULT_SEARCH_BUDGET) -> None:
        require_int(subsidy, what="subsidy", location="BlockTree()",
                    predicate=lambda v: v >= 0,
                    predicate_msg="subsidy 必须为非负整数")
        require_int(max_chain_blocks, what="max_chain_blocks",
                    location="BlockTree()", predicate=lambda v: v >= 1,
                    predicate_msg="max_chain_blocks 必须 >= 1")
        require_int(search_budget, what="search_budget",
                    location="BlockTree()", predicate=lambda v: v >= 1,
                    predicate_msg="search_budget 必须 >= 1")
        self._subsidy = subsidy
        self._max_chain_blocks = max_chain_blocks
        self._search_budget = search_budget

        self._nodes: dict[str, _Node] = {}
        # 活动最佳链状态
        self._active_tip: str | None = None
        self._state: dict[OutPoint, Output] = {}
        self._used_txids: set[str] = set()
        self._journals: dict[str, Journal] = {}

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------

    @property
    def subsidy(self) -> int:
        return self._subsidy

    @property
    def tip(self) -> str | None:
        """当前最佳链 tip 的 block_id；树为空时为 ``None``。"""

        return self._active_tip

    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, block_id: object) -> bool:
        return block_id in self._nodes

    def get_block(self, block_id: str) -> Block:
        return self._nodes[block_id].block

    def active_chain_ids(self) -> tuple[str, ...]:
        """活动链 block_id，自创世向 tip 有序；树为空时为 ``()``。"""

        if self._active_tip is None:
            return ()
        return tuple(reversed(self._root_path(self._active_tip)))

    def utxos(self) -> tuple[UTXO, ...]:
        """活动链 tip 处的全部 UTXO，按 ``(txid, output_index)`` 排序。"""

        return tuple(
            UTXO(op, out) for op, out in sorted(
                self._state.items(),
                key=lambda kv: (kv[0].txid, kv[0].output_index)))

    def utxo(self, txid: str, output_index: int) -> UTXO | None:
        op = OutPoint(txid, output_index)
        out = self._state.get(op)
        return UTXO(op, out) if out is not None else None

    def cumulative_work(self, block_id: str) -> int:
        """某块所在链在该块处的累计 work。"""

        return self._nodes[block_id].cum_work

    # ------------------------------------------------------------------
    # tip 搜索（预算耗尽 vs 无解，严格分开）
    # ------------------------------------------------------------------

    def choose_tip(self, budget: int | None = None) -> str:
        """全树枚举选择最佳 tip。

        :raises NoSolutionError: 树中没有任何候选块（可证明无解）。
        :raises BudgetExhaustedError: 节点访问数超过 ``budget``
            （不代表无解，调大预算可能成功）。
        """

        budget = self._search_budget if budget is None else require_int(
            budget, what="budget", location="choose_tip()",
            predicate=lambda v: v >= 1, predicate_msg="budget 必须 >= 1")
        winner, _ = self._best_tip(self._nodes, budget)
        return winner

    @staticmethod
    def _best_tip(nodes: dict[str, _Node], budget: int
                  ) -> tuple[str, dict[str, int]]:
        if not nodes:
            raise NoSolutionError("区块树为空，不存在任何候选 tip")
        visited = 0
        cum: dict[str, int] = {}

        def cumulative(start: str) -> None:
            # 沿 parent 向上走到已记忆节点或根，再向下回填；
            # 每个节点在整次枚举中只计一次访问。
            nonlocal visited
            path: list[str] = []
            nid: str | None = start
            while nid is not None and nid not in cum:
                visited += 1
                if visited > budget:
                    raise BudgetExhaustedError(
                        f"tip 搜索节点访问预算耗尽（预算 {budget}）",
                        budget=budget, visited=visited)
                path.append(nid)
                parent = nodes[nid].block.parent
                nid = parent if parent is not None and parent in nodes else None
            total = cum[nid] if nid is not None else 0
            for x in reversed(path):
                total += nodes[x].block.work
                cum[x] = total

        for nid in nodes:
            cumulative(nid)
        # work 为正整数，最大累计值必然出现在某个叶块；
        # 同分时 block_id 字典序最小。
        winner = min(nodes, key=lambda nid: (-cum[nid], nid))
        return winner, cum

    # ------------------------------------------------------------------
    # 区块接收
    # ------------------------------------------------------------------

    def add_block(self, block: Block) -> Reorg:
        """接收一个区块，必要时触发重组；返回本次变化路径 :class:`Reorg`。

        成功后区块一定已入树；若新区块所在链未成为最佳链，则活动状态
        不变（返回的 Reorg ``old_tip == new_tip``）。失败时树与活动
        状态均保持调用前原样（原子性）。
        """

        validate_block(block)  # 静态结构错误，先于一切变更

        if block.block_id in self._nodes:
            raise DuplicateBlockError(
                f"block_id {block.block_id} 已存在于区块树",
                location=f"block={block.block_id}")

        has_genesis = any(
            b.parent is None for b in (n.block for n in self._nodes.values()))
        if block.parent is None:
            if has_genesis:
                raise InvalidBlockError(
                    f"创世块已存在，区块 {block.block_id} 不能再次声明 "
                    "parent=None", location=f"block={block.block_id}",
                    )
            parent_node = None
        else:
            parent_node = self._nodes.get(block.parent)
            if parent_node is None:
                raise UnknownParentError(
                    f"区块 {block.block_id} 的 parent {block.parent} "
                    "不在区块树中",
                    location=f"block={block.block_id}")

        chain_ids = self._root_path(block.parent) if block.parent else []
        chain_ids.reverse()  # root -> parent
        candidate_height = len(chain_ids)  # 创世高度 0
        if len(chain_ids) + 1 > self._max_chain_blocks:
            raise ChainLimitError(
                f"区块 {block.block_id} 所在链长度 "
                f"{len(chain_ids) + 1} 超过上限 "
                f"{self._max_chain_blocks}（含创世块）",
                location=f"block={block.block_id}")

        # --- 以祖先状态验证：从创世重放整链 + 候选块（全部在临时副本上）---
        sim_state: dict[OutPoint, Output] = {}
        sim_txids: set[str] = set()
        sim_journals: dict[str, Journal] = {}
        for height, nid in enumerate(chain_ids):
            j = apply_block(sim_state, sim_txids,
                            self._nodes[nid].block, height, self._subsidy)
            sim_journals[nid] = j
        candidate_journal = apply_block(
            sim_state, sim_txids, block, candidate_height, self._subsidy)

        # --- tip 选择：先在“含候选块”的临时视图上枚举，保证失败无副作用 ---
        cum_work = (parent_node.cum_work if parent_node else 0) + block.work
        candidate_node = _Node(
            block=block, height=candidate_height, cum_work=cum_work)
        proposed = dict(self._nodes)
        proposed[block.block_id] = candidate_node
        winner, _ = self._best_tip(proposed, self._search_budget)

        # --- 提交：入树 + 必要时在临时副本上撤销旧链/应用新链 ---
        old_tip = self._active_tip
        plan = self._reorg_plan(old_tip, winner, proposed)

        new_state = dict(self._state)
        new_txids = set(self._used_txids)
        new_journals = dict(self._journals)
        fresh_journals: dict[str, Journal] = {}
        for nid in plan.deactivated:
            undo_block(new_state, new_txids,
                       proposed[nid].block, new_journals[nid])
        for nid in plan.activated:
            j = apply_block(new_state, new_txids, proposed[nid].block,
                            proposed[nid].height, self._subsidy)
            fresh_journals[nid] = j

        # 全部成功，原子提交
        self._nodes[block.block_id] = candidate_node
        if winner != old_tip:
            for nid in plan.deactivated:
                new_journals.pop(nid, None)
            new_journals.update(fresh_journals)
            self._state = new_state
            self._used_txids = new_txids
            self._journals = new_journals
            self._active_tip = winner
        return plan

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _root_path(self, tip: str | None) -> list[str]:
        """返回 tip -> root 的 block_id 列表（含两端）。"""

        path: list[str] = []
        nid = tip
        while nid is not None:
            path.append(nid)
            node = self._nodes.get(nid)
            nid = node.block.parent if node is not None else None
        return path

    @staticmethod
    def _reorg_plan(old_tip: str | None, new_tip: str,
                    nodes: dict[str, _Node]) -> Reorg:
        """构造撤销/重放路径，不在此处修改任何状态。"""

        if old_tip is None:
            activated = list(reversed(
                BlockTree._root_path_static(new_tip, nodes)))
            return Reorg(old_tip=None, new_tip=new_tip,
                         deactivated=(), activated=tuple(activated))

        old_up = BlockTree._root_path_static(old_tip, nodes)
        new_up = BlockTree._root_path_static(new_tip, nodes)
        old_up.reverse()  # root -> tip
        new_up.reverse()
        common = 0
        limit = min(len(old_up), len(new_up))
        while common < limit and old_up[common] == new_up[common]:
            common += 1
        deactivated = tuple(reversed(old_up[common:]))  # old tip -> fork
        activated = tuple(new_up[common:])              # fork child -> new tip
        return Reorg(old_tip=old_tip, new_tip=new_tip,
                     deactivated=deactivated, activated=activated)

    @staticmethod
    def _root_path_static(tip: str, nodes: dict[str, _Node]) -> list[str]:
        path: list[str] = []
        nid: str | None = tip
        while nid is not None:
            path.append(nid)
            nid = nodes[nid].block.parent
        return path
