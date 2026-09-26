# 有界 UTXO 链重组的撤销账本（bounded UTXO reorg ledger）

一个**仅用 Python 3.14.7 标准库**实现的内存区块树 + UTXO 账本，支持侧枝接收、
累计工作量选链与“撤销旧链 → 应用新链”的原子重组。不验证 PoW，区块 ID 为
合成唯一字符串。核心、测试、演示全部离线运行，无第三方依赖。

## 运行环境

- Python **3.14.7**（仅依赖标准库；3.10+ 语法即可运行，但以 3.14.7 为准）
- Linux 原生，安装 Python 后**无需安装任何其他依赖**
- 不需要网络、账号、真实密钥、数据库、中间件、Docker 或云 API

## venv 准备（可选）

库本身只用标准库，venv 仅用于隔离解释器；仓库根的 `.venv` 是指向已备
解释器的符号链接，也可自行创建：

```bash
python3.14 -m venv .venv        # 或任何 >= 3.10 的 python3
.venv/bin/python -m unittest discover -s tests -v
```

不建 venv 时直接用系统 `python` 执行下文命令即可，无需 pip 安装任何包。

## 准确命令

```bash
# 必要测试（37 个，含确定性差分测试与相对高度测试）
python -m unittest discover -s tests -v

# 固定输入演示（真实重组 + 实际触发的拒绝/异常边界，实测 < 1 秒）
python demo.py

# 本轮迭代演示：相对高度要求 + 一项边界（差一块拒绝），实测 < 1 秒
python demo_iteration.py
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
  test_relative_height.py 相对高度要求与重组创建高度测试
demo.py           固定输入演示（基础功能）
demo_iteration.py 固定输入演示（相对高度要求 + 边界）
```

## 公开数据结构

均为不可变（frozen）数据类，构造时执行**入口契约校验**：

| 类型 | 字段 | 约束 |
|---|---|---|
| `OutPoint` | `tx_id: str`, `index: int` | 非空字符串；`index >= 0` |
| `TxInput` | `prev_tx: str`, `prev_index: int`, `min_relative_height: int = 0` | 指向一个 UTXO；`min_relative_height >= 0`，默认 0（旧构造方式不变） |
| `TxOutput` | `amount: int`, `lock_height: int` | `amount >= 1`；`lock_height >= 0` |
| `UTXO` | `amount: int`, `lock_height: int`, `created_height: int = 0` | 与输出同构并记住创建区块高度；由引擎在入账时写入 |
| `Transaction` | `tx_id: str`, `inputs: tuple[TxInput,...]`, `outputs: tuple[TxOutput,...]` | 至少 1 个输出；`inputs` 为空即 **coinbase** |
| `Block` | `block_id: str`, `parent_id: str \| None`, `work: int`, `transactions: tuple[Transaction,...]` | `work >= 1`；至少 1 笔交易；不可自为父；`parent_id=None` 表示创世 |

## 接口

```python
from reorg_ledger import (
    ReorgLedger, Block, Transaction, TxInput, TxOutput,
)

ledger = ReorgLedger(subsidy=50, max_blocks=100)
result = ledger.submit(block)          # 提交区块（见下文错误语义）

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
4. **锁定高度**：被引用输出的 `lock_height <= 所在块高度` 时方可花费
   （相等允许，边界即如此定义）。
5. **相对高度要求**：每个交易输入带非负整数 `min_relative_height`（默认 0）。
   UTXO 入账时记住其**创建区块高度** `created_height`；候选区块高度 `h`
   必须满足 `h >= created_height + min_relative_height` 才允许花费该输入。
   `0` 表示无相对要求，同一块内按依赖顺序消费本块新输出仍然合法（沿用
   原有块内按序规则）。本规则只按区块高度计龄，**不读取真实时间，也不引入
   绝对日期/时间戳锁**。
6. **交易 ID 唯一**：同一链历史中 `tx_id` 不可复用（含跨块）。
7. **选链**：累计 `work`（沿父链求和）最大者为活跃链；累计 work 相同取
   **tip 区块 ID 字典序较小**者。选链结果只取决于区块集合，与提交顺序无关。
8. **侧枝接收**：区块先在“从创世沿父链重放得到的祖先状态”上验证；通过即
   入树（即使未成为 tip），未通过则不入树、账本零变更。
9. **原子重组**：新 tip 产生时，在临时状态上从创世重放整条新链，成功后
   一次性切换 tip 与 UTXO 集合；中途异常保留旧状态（不会留下部分变更）。
   因为验证与重组都从创世重放，UTXO 的 `created_height` 总是按**当前活跃
   链**的区块高度重建——重组后不会沿用另一条分支上的创建高度（同一笔交易
   在不同分支入账高度不同，花费资格随之改变，即“年龄缩短/变长”）。

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
  `coinbase-overflow`、`coinbase-position`、`multiple-coinbase`、
  `duplicate-txid`、`multiple-genesis`、`relative-height-not-mature`。
- **相对高度不足**（`relative-height-not-mature`）：错误信息指出是哪个
  输入（`tx_id` + `outpoint`）、输出创建高度、候选高度以及**还差几块**
  （`"N block(s) short"`）与可花费高度。注意“内存里能构造出的交易”不
  等于“放入当前高度区块合法”——合法性总是相对候选区块高度判定。
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
- **锁定只按区块高度**：绝对锁定高度（`lock_height`）与输入级相对高度
  要求（`min_relative_height`）两种；不支持时间戳/真实日期锁、脚本/签名。
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
- 相对高度覆盖：恰好到期可花、差一块拒绝（错误含“还差几块”与输入定位）、
  零要求同块链式消费、正要求同块即花拒绝、重组后创建高度随新链恢复
  （年龄缩短，旧分支高度不残留）、失败区块零部分消费、旧未设置相对锁的
  交易行为不变（默认 0），以及 `tests/reference.py:replay_heights` 按区块
  历史独立重建 UTXO 创建高度作为参照（差分比对随机区块树）。

## demo_iteration.py 输出内容

1. **正常功能**：相对要求恰好到期时花费成功；零要求输入同块链式消费；
   真实重组后 UTXO 创建高度按新链恢复。
2. **实际触发的边界**：差一块的花费被 `BlockValidationError(
   reason='relative-height-not-mature')` 拒绝，错误指出输入定位与
   “还差几块”，且失败区块不留下部分消费。

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
  （Bitcoin 等）兼容**。
- 不缓存孤儿块：父块未知直接抛 `OrphanBlockError`，父块到达后需重新提交子块。
- 不验证 PoW；`work` 是调用方给定的正整数，只用于累计比较。
- 重放式实现面向 ≤100 块的有界场景；远超该规模时性能不做承诺。
- 区块与交易结构由调用方负责构造，合成 ID 的全局唯一性由提交侧保证；同一 ID
  重复提交会被 `DuplicateBlockError` 拒绝。
