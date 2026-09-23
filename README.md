# 有界 UTXO 链重组的撤销账本（utxoledger）

一个**从零实现**、仅用 Python 标准库的可复用内存库：维护区块树与 UTXO 账本，
在收到更高累计 work 的竞争链时，以“**撤销旧链 → 应用新链**”的原子重组切换
活动链。不验证 PoW，区块 ID 为合成的唯一字符串。

## 运行环境

* **Python 3.14.7**（Linux 原生；仅使用 3.10+ 即有的标准库特性，如
  `slots=True` dataclass）。
* **零第三方依赖**：安装 Python 后无需 `pip install` 任何东西，不联网、
  不需要账号 / 密钥 / 数据库 / Docker。

## 准确命令

```bash
# 必要测试（41 个用例，含独立参考对照与随机差异测试）
python -m unittest discover -s tests -v

# 固定输入演示：一个正常重组结果 + 两个实际触发的拒绝边界，约 0.01s
python demo.py
```

所有数据均由仓库内确定性代码现场生成，无附件、无在线语料、无预录结果、
无 `sleep`。

## 快速上手

```python
from utxoledger import BlockTree
from utxoledger.types import Block, Input, OutPoint, Output, Transaction

tree = BlockTree(subsidy=50)
genesis = Block("G", None, 1, (
    Transaction("cbG", (), (Output(50, 0),), is_coinbase=True),
))
tree.add_block(genesis)

b1 = Block("B1", "G", 10, (
    Transaction("t1", (Input(OutPoint("cbG", 0)),), (Output(50, 0),)),
))
tree.add_block(b1)

tree.tip                 # 'B1'
tree.active_chain_ids()  # ('G', 'B1')
[(u.outpoint, u.value, u.lock_height) for u in tree.utxos()]
```

## 公开数据结构（`utxoledger/types.py`，均为冻结 dataclass）

| 类型 | 字段 | 说明 |
|---|---|---|
| `OutPoint` | `txid: str`, `output_index: int >= 0` | 交易输出的全局引用 |
| `Output` | `value: int >= 0`, `lock_height: int >= 0` | 金额（整数）与锁定高度 |
| `Input` | `outpoint: OutPoint` | 引用一个已存在的输出；不做脚本/签名验证 |
| `Transaction` | `txid`, `inputs`, `outputs`, `is_coinbase=False` | 输入/输出均为有序 tuple |
| `Block` | `block_id: str`, `parent: str \| None`, `work: int >= 1`, `transactions` | 创世块 `parent=None` |
| `UTXO` | `outpoint`, `output` | 账本视图条目（另有 `value` / `lock_height` 便捷属性） |
| `Reorg` | `old_tip`, `new_tip`, `deactivated`, `activated` | 一次 tip 切换路径；`is_reorg` 表示是否真撤销过旧块 |

## 接口（`utxoledger/chain.BlockTree`）

* `BlockTree(*, subsidy=50, max_chain_blocks=100, search_budget=1_000_000)`
* `add_block(block) -> Reorg`：静态校验 → 查重 / parent 检查 →
  **从祖先状态（自创世重放整条候选链）验证** → 全树选 tip →
  必要时在临时副本上撤销旧链 / 应用新链，**全部成功后一次性提交**。
  任何失败都不留下部分变更（区块不入树、活动状态不变）。
  合法但未胜出的侧枝会入树保留，返回的 `Reorg` 中 `old_tip == new_tip`。
* `tip: str | None`、`active_chain_ids() -> tuple[str, ...]`（创世→tip）。
* `utxos() -> tuple[UTXO, ...]`（活动链 tip 视图，按 `txid,index` 排序）；
  `utxo(txid, i) -> UTXO | None`。
* `cumulative_work(block_id)`、`get_block(block_id)`、`len(tree)`、
  `block_id in tree`。
* `choose_tip(budget=None) -> str`：显式全树搜索。树空抛 `NoSolutionError`
  （可证明无解）；访问节点数超预算抛 `BudgetExhaustedError`
  （**不代表无解**，调大预算可能成功），二者严格区分。

## 协议契约（本题的有限数学子集）

* **work**：正整数；链的累计 work 为链上各块之和。tip 取累计 work 最大者；
  完全同分取 block_id 字典序最小者。
* **交易输入不可重复**（同一笔交易内引用同一 OutPoint 直接静态拒绝）；
  引用的 OutPoint 必须在执行起点 UTXO 集合中存在。
* **输出和 ≤ 输入和**，差额为本笔手续费。
* **锁定高度**：输出只能在链上高度 `block_height >= lock_height` 的区块中
  被花费（创世高度为 0）。
* **coinbase**：无输入、每块至多一个且必须位于交易列表首位；输出总额
  ≤ `固定补贴 subsidy + 本块普通交易手续费之和`。
* **txid 链上唯一**：同一链（及同一块内）不得重放。
* 普通交易按列表顺序执行，前一笔的输出可被同块后序交易花费；coinbase
  输出在块内最后落账，故**不能在同块内被花费**（见设计取舍）。

## 错误语义（`utxoledger/errors.py`）

全部继承公共基类 `LedgerError`：

* `ValidationError` 族（带稳定 `code` 与可定位 `location` 字符串，
  如 `block=B2 tx=t3 input=0`）：`InvalidBlockError`、`DuplicateBlockError`、
  `UnknownParentError`、`ChainLimitError`、`InvalidTransactionError`、
  `DuplicateInputError`、`MissingOutPointError`、`LockHeightError`
  （附带 `block_height/lock_height`）、`ValueBalanceError`
  （附带 `input_sum/output_sum`）、`CoinbaseExcessError`
  （附带 `claimed/cap`）。
* `NoSolutionError`：搜索空间为空，证明无解。
* `BudgetExhaustedError`：搜索预算耗尽，附带 `budget/visited`，与无解分开。

输入硬规则：整数参数必须是真 `int`，**拒绝 `bool`**（防止 `True` 当 1）；
数值输入若是 `float`，**NaN 与 ±Infinity 分别以 `nan_value` /
`infinity_value` 拒绝**，普通 float 同样拒绝（金额只接受整数，不隐式取整）。
字符串参数必须为非空 `str`。非法输入一律报错，不做静默纠正。

## 输入上限

* 单条链最多 **100 个块（含创世块）**，超限块在祖先状态验证前以
  `ChainLimitError` 拒绝；该上限按各候选链的祖先长度独立计数，
  不是全树节点总数。可通过构造参数调整。
* 金额为 Python 任意精度整数，无 64 位溢出问题。
* tip 搜索有节点访问预算（默认 1,000,000），耗尽报 `BudgetExhaustedError`。

## 浮点 / 误差口径

**本库全部数值（金额、work、锁定高度、补贴、预算）均为整数，API 不产生
也不接受浮点结果**，因此没有近似计算与浮点误差区间；不存在“用宽容差掩盖
边界错误”的空间。唯一与浮点有关的行为是：调用方误传 `float`（含
NaN / Infinity）时立即拒绝。

## 原子性与验证策略

* 侧枝 / 新区块先在**临时状态副本**上从创世重放整条祖先链再执行候选块，
  验证通过前不触碰树与活动账本。
* 重组提交同样在活动状态副本上进行：先按 journal 逆序撤销旧链块
  （删除新输出、原样恢复被花费输出、回收 txid），再正向应用新链块；
  任一步失败则丢弃副本，对外状态保持调用前原样。
* 每个已应用块保存撤销日志 `Journal(added, removed)`；非活动侧枝不预计算
  日志，在未来被激活时随重放生成。

## 测试与独立参考

* `tests/reference.py` 是**独立第二实现**：朴素地“逐链从创世重放”，
  不导入、不调用 `utxoledger` 任何代码（有测试强制检查其源码不含
  `import utxoledger`）。核心的期望值（tip、UTXO 集合、链顺序）来自该参考。
* `test_reorg.py` 含 40 个固定种子的随机差异测试（随机生成分叉、手续费、
  锁定高度及双花 / 超额 / 重放等非法块），逐 trial 比对两边结果。
* 覆盖：分支双花（同块与跨块）、coinbase 超额（含手续费边界 50+fee）、
  锁定高度（含重组场景）、无效侧枝高 work 不能上位、失败无部分变更、
  100 块上限、同分字典序、预算耗尽 vs 无解、NaN/Infinity/bool 拒绝。
* `apply_block` / `undo_block` 的撤销往返只作为**补充**断言。

## 设计取舍

1. **coinbase 输出块内不可花费**：coinbase 额度依赖整块手续费，故采用
   “先顺序执行普通交易累计手续费，再校验并落账 coinbase”的两段式执行。
   这与真实链 coinbase 需成熟的约束方向一致，也避免自引用式手续费循环。
2. **验证全量重放、提交增量执行**：候选链验证从创世重放（语义最简单、
   与独立参考同构）；正常的活动链延伸在提交时只执行新增块，重组时只
   撤销 / 重放分叉之后的块，100 块上限下重放成本可忽略。
3. **只维护活动链日志**：内存为 O(活动链 UTXO + 树元数据)；侧枝随时可由
   重放派生，不缓存其状态。
4. **UTXO 无脚本 / 签名**：输入只需引用存在且未锁定的 OutPoint，聚焦本题
   的撤销账本与重组语义。

## 已知限制（不宣称完整行业标准兼容）

* 纯内存、单进程；无持久化、无并发控制、无网络与 PoW 校验。
* 区块 ID 是调用方提供的合成唯一字符串，库不验证格式或哈希。
* 无脚本、签名、SegWit、时间戳 / 难度调整 / 补贴减半 / 币基数（maturity）
  等真实比特币规则；仅实现本题列明的有限协议子集。
* 链上限固定为接口边界（默认 100），不做超出上限链的分段存储。
* 同块内普通交易可链式花费前序输出（仅受锁定高度约束），这是本题
  “按序交易”的直义；若需 Mempool 式拓扑限制不在本库范围。

## 目录结构

```
utxoledger/          被测库（可 import 的本题模块）
  __init__.py        公开 API 汇总
  types.py           冻结数据结构
  errors.py          错误语义
  validation.py      静态输入校验（NaN/Infinity/bool/定位串）
  state.py           区块执行 + 撤销日志
  chain.py           区块树、tip 搜索、原子重组
tests/
  reference.py       独立参考（逐链从创世重放，不依赖核心）
  factories.py       测试构造工具
  test_*.py          校验 / 语义 / 重组 / 边界 共 41 个用例
demo.py              固定输入演示（真实重组 + 实际触发的拒绝）
```
