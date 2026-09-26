# 有界 UTXO 链重组的撤销账本（bounded UTXO reorg ledger）

一个**仅用 Python 3.14.7 标准库**实现的内存区块树 + UTXO 账本，支持侧枝接收、
累计工作量选链与“撤销旧链 → 应用新链”的原子重组。不验证 PoW，区块 ID 为
合成唯一字符串。核心、测试、演示全部离线运行，无第三方依赖。

本轮迭代在不改变旧接口默认行为的前提下，为交易输入增加**非负相对高度要求**
（`TxInput.min_age`）：UTXO 记住创建区块高度，候选块高度达到“创建高度 +
要求”才可花费；重组时创建高度随 UTXO 按区块历史重建。

## 运行环境

- Python **3.14.7**（仅依赖标准库；3.10+ 语法即可运行，但以 3.14.7 为准）
- Linux 原生，安装 Python 后**无需安装任何其他依赖**
- 不需要网络、账号、真实密钥、数据库、中间件、Docker 或云 API

## 准确命令

```bash
# 可选：标准库 venv（无需安装任何第三方包）
python3.14 -m venv .venv
source .venv/bin/activate

# 必要测试（34 个，含确定性差分测试与相对年龄专项测试）
python -m unittest discover -s tests -v

# 本轮迭代演示：相对高度年龄要求（差一块/恰好到期/同块链式/重组后年龄缩短）
# 实测 < 0.1 秒，远在 90 秒预算内；无 sleep、无预录
python demo_iteration.py

# 上一轮演示（真实重组 + 实际触发的拒绝/异常边界）继续可用
python demo.py
```

## 目录结构

```
reorg_ledger/
  __init__.py     公开接口导出
  models.py       数据结构 + 入口契约校验（类型/取值范围）
  engine.py       核心算法：区块树、祖先状态验证、选链、原子重组
  errors.py       错误语义（异常分类与可定位字段）
tests/
  reference.py    独立参考实现（从创世逐链重放 + 独立选链），不调用被测核心
  test_models.py  入口契约测试
  test_engine.py  场景测试
  test_differential.py  固定种子随机树差分测试（参考实现作预言机）
  test_relative_age.py 相对年龄专项（恰好到期/差一块/同块链式/重组年龄缩短）
demo.py           固定输入演示
demo_iteration.py 本轮迭代演示：输入相对高度要求（min_age）
```

## 公开数据结构

均为不可变（frozen）数据类，构造时执行**入口契约校验**：

| 类型 | 字段 | 约束 |
|---|---|---|
| `OutPoint` | `tx_id: str`, `index: int` | 非空字符串；`index >= 0` |
| `TxInput` | `prev_tx: str`, `prev_index: int`, `min_age: int = 0` | 指向一个 UTXO；`min_age >= 0` 为**非负相对高度要求**，默认 0 |
| `TxOutput` | `amount: int`, `lock_height: int` | `amount >= 1`；`lock_height >= 0`（绝对高度锁，本轮未改） |
| `UTXO` | `amount: int`, `lock_height: int`, `created_height: int = 0` | 与输出同构；`created_height` 是输出所在区块高度，由引擎按重放链写入 |
| `Transaction` | `tx_id: str`, `inputs: tuple[TxInput,...]`, `outputs: tuple[TxOutput,...]` | 至少 1 个输出；`inputs` 为空即 **coinbase** |
| `Block` | `block_id: str`, `parent_id: str \| None`, `work: int`, `transactions: tuple[Transaction,...]` | `work >= 1`；至少 1 笔交易；不可自为父；`parent_id=None` 表示创世 |

## 接口

```python
from reorg_ledger import (
    ReorgLedger, Block, Transaction, TxInput, TxOutput,
)

ledger = ReorgLedger(subsidy=50, max_blocks=100)
result = ledger.submit(block)          # 提交区块（见下文错误语义）

# 输入的相对高度要求：输出创建后至少经过 min_age 个区块才可被花费
# （第三参缺省为 0；0 允许同块按依赖顺序消费新输出）
aged_input = TxInput("prev-tx-id", 0, 2)

ledger.tip_id          # 当前 tip 区块 ID（空树为 None）
ledger.tip             # 当前 tip 的 Block
ledger.height          # 活跃链块数
ledger.cumulative_work # 活跃链累计 work
ledger.active_path     # tuple[str,...]：创世 → tip
ledger.snapshot_utxos()# dict[OutPoint, UTXO]：tip 处 UTXO 的副本
ledger.contains(bid)   # 区块是否在树中（含非活跃侧枝）
ledger.get_block(bid)  # 取块；不存在抛 KeyError
```

`submit` 返回 `SubmitResult`：

```python
SubmitResult(block_id, block_height, tip_id, tip_height,
             cumulative_work, became_tip, reorganized, reorg)
```

发生重组时 `reorg` 为 `ReorgPath(fork_id, old_tip, new_tip,
deactivated, activated)`：

- `deactivated`：旧 tip 向下至分叉点之后的被撤销块（撤销顺序）
- `activated`：分叉点之后向上至新 tip 的被应用块（应用顺序）
- 同链向前延伸（无撤销块）时 `reorganized=False`、`reorg=None`

## 协议语义（本题契约子集）

1. **按序交易**：块内交易按顺序应用，后一笔可花前一笔同块输出。
2. **coinbase**：零输入交易，必须位于索引 0 且每块至多一笔；其输出即刻
   入账。块末结算铸币上限：`sum(coinbase 输出金额) <= subsidy + 本块手续费`，
   其中手续费 = Σ(输入金额 − 输出金额)。超额整块拒绝。
3. **普通交易**：输入在同一交易内不可重复；每个输入必须引用一个存在且未花费
   的输出；输出金额之和 ≤ 输入金额之和。
4. **锁定高度（绝对，沿用上一轮）**：被引用输出的 `lock_height <= 所在块高度`
   时方可花费（相等允许，边界即如此定义）。本轮**不**引入任何真实时间或
   绝对日期锁。
5. **相对高度要求（本轮新增）**：每个输入可带非负整数 `min_age`（默认 0）。
   UTXO 记住其**创建区块高度** `created_height`；仅当
   `候选块高度 >= created_height + min_age` 时该输入可花费（相等即**恰好到期**，
   允许）。`min_age=0` 允许在同一区块内**按原有区块内顺序**消费新输出
   （coinbase 输出与普通交易输出都在交易提交时即刻带创建高度入账）；
   `min_age>=1` 的新输出不能在同块被花。一个输出在内存里/别的祖先下可花，
   不代表放进当前高度的候选块合法——年龄一律以**候选块自身高度**判定。
6. **重组时创建高度按历史重建**：UTXO 的 `created_height` 不是输出携带的
   静态字段，而是引擎在“从创世沿当前链重放”时按输出所在块高度写入。重组
   在临时状态上重放整条新链，因此同一输出 ID 若在侧枝的不同高度被创建，
   切换后其年龄按新链缩短/拉长，**绝不沿用另一分支上的高度**。
7. **交易 ID 唯一**：同一链历史中 `tx_id` 不可复用（含跨块）。
8. **选链**：累计 `work`（沿父链求和）最大者为活跃链；累计 work 相同取
   **tip 区块 ID 字典序较小**者。选链结果只取决于区块集合，与提交顺序无关。
9. **侧枝接收**：区块先在“从创世沿父链重放得到的祖先状态”上验证；通过即
   入树（即使未成为 tip），未通过则不入树、账本零变更。
10. **原子重组**：新 tip 产生时，在临时状态上从创世重放整条新链，成功后
   一次性切换 tip 与 UTXO 集合；中途异常保留旧状态（不会留下部分变更）。

### 年龄不足的错误语义

候选块高度小于 `created_height + min_age` 时抛 `BlockValidationError`：

- `reason = "output-too-young"`；
- `blocks_short` = `created_height + min_age - 候选块高度`（**这个输入还差
  几块**，差一块时为 1，恰好到期为 0 且不报错）；
- 同时填充 `block_id` / `height` / `tx_id` / `outpoint` 定位字段；
- 区块内任何一笔交易年龄不足都使**整块**被拒：已模拟应用的靠前交易（含
  coinbase 铸币）一并回滚，不留下部分消费。

## 输入上限

- 单链最多 **100** 个块（含创世，`max_blocks` 可在构造时改小用于测试）；
  第 101 块（任何使链长达到 101 的提交）抛 `ChainLimitError`，**该块既不
  入树也不改变 tip**。这是“搜索/处理预算耗尽”，不是非法判定。
- 金额、work、高度均为 Python 任意精度整数（无溢出），但必须满足各自下界。
- 本实现没有内建“每块交易数”上限；100 块界是唯一的链规模协议边界。

## 错误语义

所有异常继承 `LedgerError`：

| 异常 | 含义 | 关键属性 |
|---|---|---|
| `InputContractError` | 违反接口契约：类型错误、整数越界、结构不符。信息含字段路径 | — |
| `NonFiniteNumberError` | 传入 `NaN`/`±Infinity`（`InputContractError` 子类，单独分类） | — |
| `BlockValidationError` | **证明非法**：区块在祖先状态下被确定性证伪 | `reason` 短码 + `block_id`/`height`/`tx_id`/`outpoint` |
| `OrphanBlockError` | 父块未知；**既未证明合法也未证明非法**，不缓存孤儿 | `block_id`, `parent_id` |
| `ChainLimitError` | **预算耗尽**：链长超过 100，与“非法”严格区分 | `depth`, `limit` |
| `DuplicateBlockError` | 同一区块 ID 重复提交 | `block_id` |

- **整数纯度**：所有整数参数拒绝 `bool`（`True/False` 不当作 1/0），拒绝
  浮点（不做静默取整）；浮点 `NaN/Infinity` 给 `NonFiniteNumberError`，
  有限浮点给 `InputContractError`。
- **`BlockValidationError.reason` 稳定短码**：`duplicate-input`、
  `missing-input`、`output-locked`、`output-exceeds-input`、
  `output-too-young`（相对年龄不足，附 `blocks_short`）、
  `coinbase-overflow`、`coinbase-position`、`multiple-coinbase`、
  `duplicate-txid`、`multiple-genesis`。
- 失败的 `submit` 不产生未说明的部分变更：区块不入树、tip/UTXO/活跃路径
  保持提交前状态（测试 `test_failed_submit_is_atomic_against_reference`
  等逐一验证）。

## 设计取舍

- **重放式验证而非增量回滚日志**：每次接收与重组都从创世沿链重放到目标状态。
  在 100 块的有界前提下，实现直接、正确性易论证，且“撤销 + 应用”天然原子。
  代价是最坏复杂度约为 O(链长 × 交易数)；未采用全量 undo 日志做 O(1) 增量
  切换，因为题面只要求 100 块界内的正确性与原子性。
- **UTXO 集以 dict 全量快照切换**：重组提交是一次引用替换，旧链状态不残留。
- **侧枝保留**：通过祖先验证的非活跃块留在树中，之后可在其上接续更强链
  触发重组；非法块从不入树。
- **锁定语义有两种且正交**：输出上的绝对 `lock_height`（沿用上一轮）与
  输入上的非负相对 `min_age`（本轮新增）；不支持时间戳/真实时间/日期锁、
  脚本/签名。
- **创建高度不持久化在交易数据里**：`TxOutput` 不含创建高度；UTXO 的
  `created_height` 完全由重放过程推导，天然满足“重组按历史恢复”。
- **金额单位无小数**：整数最小单位，全链路无浮点，因此**不存在浮点误差口径**
  ——所有余额/手续费比较都是精确整数比较。

## 测试与独立参考

- `tests/reference.py` 是**独立**的从创世重放实现与独立选链函数，只把数据类
  当纯数据读取，不 import 也不调用 `reorg_ledger.engine` 的任何算法。
- 差分测试用固定种子（种子 0–6 等）在仓库代码内确定性生成区块树（线性链、
  分叉、乱序拓扑提交、work 反超），期望值（活跃链、tip、UTXO 集、累计 work、
  重组路径、各非法场景的拒绝短码）全部由独立参考给出；引擎只作为被比对对象。
- 往返一致性（UTXO 快照独立两次取得相等）仅作为补充断言
  （`test_roundtrip_only_as_supplement`），不用于推导期望值。
- 场景覆盖：分支双花（跨块重复花费、交易内重复输入）、coinbase 超额（含
  恰好等于补贴+手续费的通过边界）、锁定高度（`lock == height` 可花、
  `lock > height` 拒绝）、无效侧枝不入树且不污染主链、孤儿块、重复块、
  同分字典序决胜、真实重组与重组回旧链、100 块界、失败原子性、入口契约。
- 相对年龄专项（`tests/test_relative_age.py`，期望值由独立参考 `replay`
  独立重建 `created_height` 给出）：恰好到期（`height == created+age`）、
  差一块（`output-too-young` 且 `blocks_short==1`，含整块回滚零部分消费与
  “差多块”数量）、同块 `min_age=0` 链式消费（及同块 `min_age=1` 被拒）、
  重组后创建高度重建导致年龄缩短、内存别处合法不等于当前高度合法、旧两参
  `TxInput`/`UTXO` 构造与未设相对锁交易的回归行为。固定种子差分夹具也在
  输入上随机加入非零 `min_age`，扩大与独立参考的交叉比对面。

## demo_iteration.py 输出内容

1. **实际触发的边界**：高度 2 花费创建于高度 1、要求 `min_age=2` 的输出，
   真实抛出 `BlockValidationError(reason='output-too-young')`，报告
   `blocks_short=1`，并展示失败块不入树、无部分消费。
2. **恰好到期**：同一要求在高度 3（`1 + 2 == 3`）成功。
3. **同块链式**：`min_age=0` 时 coinbase → t1 → t2 → t3 同块按序消费。
4. **重组后年龄缩短**：同名输出在主链高度 3 创建（高度 4、`min_age=2`
   差 1 块被拒），侧枝在高度 2 创建并高 work 重组；重组后续块在高度 3 以
   `min_age=1` 恰好花掉它。

## demo.py 输出内容

1. **正常结果**：主链 `G→A`（累计 work 12）被高 work 侧枝 `G→B→C`
   （累计 work 50）真实重组；打印 tip 切换、`ReorgPath` 的分叉点/撤销/激活
   序列、重组前后 UTXO 集合差。
2. **实际触发的边界**：在重组后链上提交双花块，真实抛出
   `BlockValidationError(reason='missing-input', block, tx, height, outpoint)`，
   并展示 tip/活跃链零变化、坏块不入树；另真实触发 `ChainLimitError`
   （小块界演示预算语义）与 `InputContractError`（bool 被拒作整数）。

## 已知限制

- 仅实现本题的有限协议子集：无签名/脚本验证、无时间戳与时间锁、无对等网络、
  无持久化、无内存池/费用市场、无难度调整，**不宣称与任何行业完整协议
  （Bitcoin 等）兼容**。`min_age` 只是按区块高度计数的相对确认数约束，
  与 Bitcoin CSV/BIP112 的脚本语义无关；不读取真实时间、无绝对日期锁。
- 不缓存孤儿块：父块未知直接抛 `OrphanBlockError`，父块到达后需重新提交子块。
- 不验证 PoW；`work` 是调用方给定的正整数，只用于累计比较。
- 重放式实现面向 ≤100 块的有界场景；远超该规模时性能不做承诺。
- 区块与交易结构由调用方负责构造，合成 ID 的全局唯一性由提交侧保证；同一 ID
  重复提交会被 `DuplicateBlockError` 拒绝。
