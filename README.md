# 有界 UTXO 链重组的撤销账本（bounded UTXO reorg ledger）

一个**仅用 Python 3.14.7 标准库**实现的内存区块树 + UTXO 账本，支持侧枝接收、
累计工作量选链与“撤销旧链 → 应用新链”的原子重组；并支持**最终确认深度**：
在显式确认深度之外保存 UTXO 检查点、裁剪撤销日志，检查点可序列化/恢复后继续
接块；深度重组缺历史时返回 `requires_history`。不验证 PoW，区块 ID 为合成唯一
字符串。核心、测试、演示全部离线运行，无第三方依赖。

## 运行环境

- Python **3.14.7**（仅依赖标准库；3.10+ 语法即可运行，但以 3.14.7 为准）
- Linux 原生，安装 Python 后**无需安装任何其他依赖**
- 不需要网络、账号、真实密钥、数据库、中间件、Docker 或云 API

## 准确命令

```bash
# 全部测试（60 个：契约/场景/差分 + 最终确认检查点）
python -m unittest discover -s tests -v

# 固定输入演示（真实重组 + 检查点裁剪/恢复 + 实际触发的拒绝/异常边界）
python demo.py
```

## 目录结构

```
reorg_ledger/
  __init__.py     公开接口导出
  models.py       数据结构 + 入口契约校验（类型/取值范围）
  engine.py       核心算法：区块树、祖先状态验证、选链、原子重组、
                  最终确认锚点/撤销日志裁剪、检查点导出与恢复
  checkpoint.py   检查点字节信封（magic + SHA-256 + 规范化 JSON）与语义指纹
  errors.py       错误语义（异常分类与可定位字段）
tests/
  reference.py    独立参考实现（从创世逐链重放 + 独立选链），不调用被测核心
  test_models.py  入口契约测试
  test_engine.py  场景测试
  test_differential.py  固定种子随机树差分测试（参考实现作预言机）
  test_finality.py      最终确认/裁剪/requires_history/检查点恢复与损坏测试
demo.py           固定输入演示
```

## 公开数据结构

均为不可变（frozen）数据类，构造时执行**入口契约校验**：

| 类型 | 字段 | 约束 |
|---|---|---|
| `OutPoint` | `tx_id: str`, `index: int` | 非空字符串；`index >= 0` |
| `TxInput` | `prev_tx: str`, `prev_index: int` | 指向一个 UTXO |
| `TxOutput` | `amount: int`, `lock_height: int` | `amount >= 1`；`lock_height >= 0` |
| `UTXO` | `amount: int`, `lock_height: int` | 与输出同构 |
| `Transaction` | `tx_id: str`, `inputs: tuple[TxInput,...]`, `outputs: tuple[TxOutput,...]` | 至少 1 个输出；`inputs` 为空即 **coinbase** |
| `Block` | `block_id: str`, `parent_id: str \| None`, `work: int`, `transactions: tuple[Transaction,...]` | `work >= 1`；至少 1 笔交易；不可自为父；`parent_id=None` 表示创世 |

## 接口

```python
from reorg_ledger import (
    ReorgLedger, Block, Transaction, TxInput, TxOutput,
)

ledger = ReorgLedger(subsidy=50, max_blocks=100)
outcome = ledger.submit(block)      # 提交区块，返回 SubmitOutcome（见下文）

ledger.tip_id          # 当前 tip 区块 ID（空树为 None）
ledger.tip             # 当前 tip 的 Block
ledger.height          # 活跃链块数（绝对高度，含已裁剪脊链）
ledger.cumulative_work # 活跃链累计 work
ledger.active_path     # tuple[str,...]：锚点(或创世) → tip
ledger.snapshot_utxos()# dict[OutPoint, UTXO]：tip 处 UTXO 的副本
ledger.contains(bid)   # 区块是否在窗口树中（含非活跃侧枝；不含已裁剪块）
ledger.get_block(bid)  # 取块；不存在抛 KeyError
```

`submit` 返回 `SubmitOutcome`（联合状态）：

```python
outcome.status            # "accepted" 或 "requires_history"
outcome.accepted          # bool
outcome.requires_history  # bool
outcome.result            # accepted 时为 SubmitResult；否则 None
outcome.history           # requires_history 时为 HistoryRequired；否则 None
```

为向后兼容，`accepted` 时可直接在 `outcome` 上读取传统 `SubmitResult` 字段
（`outcome.became_tip`、`outcome.reorg`、`outcome.tip_id` …），即旧调用
`ledger.submit(block).became_tip` 无需改动；`requires_history` 时读取这些字段
抛 `LedgerStateError`。

`SubmitResult(block_id, block_height, tip_id, tip_height,
cumulative_work, became_tip, reorganized, reorg)`：

发生重组时 `reorg` 为 `ReorgPath(fork_id, old_tip, new_tip,
deactivated, activated)`：

- `deactivated`：旧 tip 向下至分叉点之后的被撤销块（撤销顺序）
- `activated`：分叉点之后向上至新 tip 的被应用块（应用顺序）
- 同链向前延伸（无撤销块）时 `reorganized=False`、`reorg=None`

## 最终确认深度、检查点与 requires_history

```python
ledger = ReorgLedger(finalize_depth=K)   # K 为正整数；None（默认）=不裁剪
...
raw: bytes = ledger.checkpoint_bytes()                 # 导出检查点
ledger2 = ReorgLedger.from_checkpoint(raw)             # 恢复
ledger2 = ReorgLedger.from_checkpoint(                 # 也可显式校验参数
    raw, subsidy=50, max_blocks=100, finalize_depth=K)
ledger2.submit(next_block)                             # 恢复后直接继续接块
```

**语义**：

- **锚点**：活跃链上埋于 tip 之下恰好 `K` 个确认的块（路径索引
  `len(path)-1-K`）成为不可回退的最终确认锚点。每次提交后锚点只沿活跃链
  **单调前移**。属性：`anchor_id` / `anchor_height`（=`finalized_height`）。
- **检查点与裁剪**：锚点处保存该状态的完整 UTXO 集与已用交易 ID 集；锚点
  之前的脊块从可重放窗口移除（撤销日志被裁剪），只保留定位元数据
  `pruned_spine`（`PrunedBlock(block_id, parent_id, height, cumulative_work)`，
  高度升序）。与新锚点不连通的旧侧枝也一并裁剪。窗口块 ID 见
  `window_block_ids`。裁剪后所有验证/重组都从锚点状态重放窗口内的块。
- **恢复**：检查点是自描述字节流（`RLCK1` 信封 + 载荷 SHA-256 + 规范化
  JSON），内含参数、锚点及其状态、裁剪脊链、全部窗口块和 tip。恢复时
  **从锚点逐块独立重放**重建 tip 状态并核对语义指纹，因此恢复出的 tip UTXO
  与“从创世完整重放”一致，恢复后可直接继续接块。
- **requires_history**：提交块的父块（或更早祖先）落在**已裁剪**的最终确认
  历史里时（深度重组需要回退锚点之前的历史），`submit` **不抛异常**而返回
  `SubmitOutcome(status="requires_history", history=HistoryRequired(...))`，
  该块不入树、账本零变更。`HistoryRequired` 字段：`block_id`、
  `missing_parent_id`（无父块时为 `None`）、`anchor_id`、`anchor_height`、
  `finalize_depth`。
- **与孤儿块的区别**：父块**完全未知**仍是 `OrphanBlockError`（既未证明合法
  也未证明非法）；只有“祖先确实存在过但已被裁剪”才是 `requires_history`。
- **默认行为不变**：`finalize_depth=None`（默认）时不产生锚点、不裁剪、
  `checkpoint_bytes()` 抛 `LedgerStateError`，行为与旧版本完全一致。
- 尚未有任何块取得 K 个确认时 `checkpoint_bytes()` 抛 `LedgerStateError`。

## 协议语义（本题契约子集）

1. **按序交易**：块内交易按顺序应用，后一笔可花前一笔同块输出。
2. **coinbase**：零输入交易，必须位于索引 0 且每块至多一笔；其输出即刻
   入账。块末结算铸币上限：`sum(coinbase 输出金额) <= subsidy + 本块手续费`，
   其中手续费 = Σ(输入金额 − 输出金额)。超额整块拒绝。
3. **普通交易**：输入在同一交易内不可重复；每个输入必须引用一个存在且未花费
   的输出；输出金额之和 ≤ 输入金额之和。
4. **锁定高度**：被引用输出的 `lock_height <= 所在块高度` 时方可花费
   （相等允许，边界即如此定义）。
5. **交易 ID 唯一**：同一链历史中 `tx_id` 不可复用（含跨块）。
6. **选链**：累计 `work`（沿父链求和）最大者为活跃链；累计 work 相同取
   **tip 区块 ID 字典序较小**者。选链结果只取决于区块集合，与提交顺序无关。
7. **侧枝接收**：区块先在“从锚点（无锚点则从创世）沿父链重放得到的祖先
   状态”上验证；通过即入树（即使未成为 tip），未通过则不入树、账本零变更。
8. **原子重组**：新 tip 产生时，在临时状态上重放整条新链（自锚点或创世），
   成功后一次性切换 tip 与 UTXO 集合；中途异常保留旧状态（不会留下部分
   变更）。分叉点在锚点之前（需要已裁剪历史）时不重放，直接返回
   `requires_history`。

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
| `DuplicateBlockError` | 同一区块 ID 重复提交（含重交已被裁剪的块） | `block_id` |
| `CheckpointCorruptError` | 检查点损坏/内部不一致，恢复被拒绝（不产生半成品） | `kind` 稳定短码 + `message` |

- **`CheckpointCorruptError.kind` 短码**：`bad-encoding`（非 bytes）、
  `bad-wrapper`（无 RLCK1 信封/信封截断）、`bad-checksum`（SHA-256 不符，
  字节翻转/截断）、`bad-json` / `bad-structure`（载荷不是期望结构）、
  `bad-magic` / `bad-version`、`param-mismatch`（恢复参数与检查点不一致）、
  `bad-spine` / `bad-anchor` / `bad-blocks` / `bad-tip`（脊链不连续、锚点
  不自洽、窗口块无法从锚点连通重放、tip 不在窗口）、`bad-replay`（窗口块从
  锚点重放被证伪）、`bad-fingerprint`（语义指纹不符——字节合法但内容被
  篡改）、`bad-value`（字段越界）。

- **整数纯度**：所有整数参数拒绝 `bool`（`True/False` 不当作 1/0），拒绝
  浮点（不做静默取整）；浮点 `NaN/Infinity` 给 `NonFiniteNumberError`，
  有限浮点给 `InputContractError`。
- **`BlockValidationError.reason` 稳定短码**：`duplicate-input`、
  `missing-input`、`output-locked`、`output-exceeds-input`、
  `coinbase-overflow`、`coinbase-position`、`multiple-coinbase`、
  `duplicate-txid`、`multiple-genesis`。
- 失败的 `submit` 不产生未说明的部分变更：区块不入树、tip/UTXO/活跃路径
  保持提交前状态（测试 `test_failed_submit_is_atomic_against_reference`
  等逐一验证）。

## 设计取舍

- **锚点检查点 + 窗口重放**：启用 `finalize_depth` 后，早期脊块的完整数据与
  撤销日志被裁剪，只在锚点保留一份 UTXO/已用交易集检查点；接收与重组只需从
  锚点重放窗口（≤ K 个确认 + 侧枝深度）内的块。锚点**只沿活跃链单调前移**，
  因此最终确认的历史不可能被回退——需要它的提交得到明确的
  `requires_history` 状态，而不是悄悄重放或错误接受。
- **重放式验证而非逐交易 undo 日志**：即使在窗口内，也采用“从锚点状态重放
  到目标”而不是为每笔交易维护逆操作日志。100 块有界前提下实现直接、正确性
  易论证，且“撤销旧枝 + 应用新枝”在临时状态上天然原子；裁剪则通过锚点
  检查点把“从创世重放”缩短为“从锚点重放”。
- **UTXO 集以 dict 全量快照切换**：重组提交是一次引用替换，旧链状态不残留。
- **检查点双重防护**：字节信封的 SHA-256 检测翻转/截断；载荷内的语义指纹
  覆盖参数、锚点及其状态、脊链、tip 重放状态与**全部窗口块内容**，检测
  “字节合法但语义被篡改”。恢复时窗口块从锚点**逐块独立重放验证**，损坏即
  拒、无半成品对象。校验和/指纹非密码学承诺（无密钥、不防恶意伪造者），
  职责是把损坏数据变成明确错误。
- **侧枝保留**：通过祖先验证的非活跃块留在窗口树中，之后可接续更强链触发
  重组；锚点前移时与新锚点不连通的旧侧枝被裁剪，其 ID 留作重复提交检测。
- **锁定时间只实现绝对高度**一种形式；不支持时间戳、相对锁、脚本/签名。
- **金额单位无小数**：整数最小单位，全链路无浮点，因此**不存在浮点误差口径**
  ——所有余额/手续费比较都是精确整数比较。

## 测试与独立参考

- `tests/reference.py` 是**独立**的从创世重放实现与独立选链函数，只把数据类
  当纯数据读取，不 import 也不调用 `reorg_ledger.engine` 的任何算法。
- 差分测试用固定种子（种子 0–6 等）在仓库代码内确定性生成区块树（线性链、
  分叉、乱序拓扑提交、work 反超），期望值（活跃链、tip、UTXO 集、累计 work、
  重组路径、各非法场景的拒绝短码）全部由独立参考给出；引擎只作为被比对对象。
- 最终确认相关的期望值不用引擎自证：锚点/裁剪边界按确认深度定义**手算**；
  恢复与“继续接块”后的 tip UTXO 由独立 `reference.replay` 从创世**完整重放**
  给出；损坏样本由测试手工翻转/截断字节、解码后改写语义字段再重新编码得到。
- 往返一致性（UTXO 快照独立两次取得相等）仅作为补充断言
  （`test_roundtrip_only_as_supplement`），不用于推导期望值。
- 新增场景覆盖（`test_finality.py`）：确认深度裁剪边界（K=1/K=2、锚点恰好
  出现的高度、脊链元数据手算）、窗口内浅重组成功且旧枝被裁剪、最终确认不可
  回退（requires_history 的载荷与零变更）、未知父块仍为孤儿、恢复后继续接块
  与未中断账本等价、二次保存/恢复串联、参数不匹配、字节翻转/截断/垃圾、
  锚点金额/tip/窗口块/参数语义篡改、坏 magic/version、断连窗口、坏结构内容、
  裁剪后块界仍按绝对高度生效、向后兼容的结果字段。
- 原有场景覆盖保持不变：分支双花、coinbase 超额边界、锁定高度边界、无效
  侧枝不入树、孤儿块、重复块、同分字典序决胜、真实重组与重组回旧链、100
  块界、失败原子性、入口契约。

## demo.py 输出内容

1. **正常结果**：主链 `G→A`（累计 work 12）被高 work 侧枝 `G→B→C`
   （累计 work 50）真实重组；打印 tip 切换、`ReorgPath` 的分叉点/撤销/激活
   序列、重组前后 UTXO 集合差。
2. **实际触发的边界**：在重组后链上提交双花块，真实抛出
   `BlockValidationError(reason='missing-input', block, tx, height, outpoint)`，
   并展示 tip/活跃链零变化、坏块不入树；另真实触发 `ChainLimitError`
   （小块界演示预算语义）与 `InputContractError`（bool 被拒作整数）。
3. **最终确认检查点**：`finalize_depth=2` 建链并展示锚点前移、脊链裁剪与
   窗口块集合；窗口内侧枝触发浅重组；深重组缺历史真实返回
   `requires_history`（与未知父块的 `OrphanBlockError` 对照）；保存检查点、
   新账本恢复、继续接块，并用**手算**的 coinbase/转账输出存在性与金额核对
   tip UTXO；最后翻转一个字节，恢复被 `CheckpointCorruptError` 拒绝。

## 已知限制

- 仅实现本题的有限协议子集：无签名/脚本验证、无时间戳与时间锁、无对等网络、
  无内存池/费用市场、无难度调整，**不宣称与任何行业完整协议（Bitcoin 等）
  兼容**。
- 持久化仅限**显式导出/恢复检查点字节流**（`checkpoint_bytes` /
  `from_checkpoint`），无自动落盘、无写时崩溃恢复（partial-write/power-loss）
  模型；原子写文件由调用方负责（如先写临时文件再 rename）。
- 检查点的 SHA-256/语义指纹用于检测损坏与非一致篡改，**不是**防恶意伪造的
  密码学认证（无 MAC/签名/密钥）；安全威胁模型不在本题范围。
- 不缓存孤儿块：父块未知直接抛 `OrphanBlockError`，父块到达后需重新提交子块。
- 不验证 PoW；`work` 是调用方给定的正整数，只用于累计比较。
- 启用裁剪后，`active_path` 自当前锚点起（更早的最终确认块只保留
  `pruned_spine` 元数据），`get_block`/`contains` 不覆盖已裁剪块；需要完整
  创世→tip 路径的调用方应拼接 `pruned_spine`，或不启用 `finalize_depth`。
- 重放式实现面向 ≤100 块的有界场景；远超该规模时性能不做承诺。
- 区块与交易结构由调用方负责构造，合成 ID 的全局唯一性由提交侧保证；同一 ID
  重复提交（含重交已裁剪块）会被 `DuplicateBlockError` 拒绝。
