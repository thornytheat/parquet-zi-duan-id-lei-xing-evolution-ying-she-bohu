# schemaevo · Parquet 字段 ID 与类型演进映射

湖仓表治理小工具：上游提交 Parquet 表 schema 变更时，先算清「字段怎么变了、兼容级别是什么、迁移 SQL 按什么顺序执行、谁会读坏」。
字段在结构层级间搬迁时，再生成「旧路径 → 新路径」的别名映射并原子发布，旧报表不改 SQL 也能读到新路径的值。

## 能力

- 字段匹配：`field_id` 优先，无 id 时按名称精确匹配；改名、字段 ID 变更、新增、删除逐条标注依据。
- 类型提升矩阵：代码内显式表（int8/int16/int32/int64、float/double、decimal 精度、string→int、date→timestamp 等），表外类型对一律 `unknown`（不猜）。
- 兼容级别：按「旧写新读 / 新写旧读」双向判定，输出 `full` / `backward` / `forward` / `none`。
- 迁移计划：有序 `ALTER TABLE` 序列（改名 → 加列/默认值 → 回填 → 类型变更 → 删列最后）、需回填对象、读写方升级顺序；分区字段被删或改类型直接阻断。
- 列引用检查：数据文件清单里引用被删/被阻断列的 → `broken_consumers`；引用改名列的 → 需同步改名。
- 路径别名（结构搬层）：嵌套 schema 展平成路径，按 `field_id` 核对每个字段的父级链、编号、类型；冲突或缺失整批隔离，绝不把旧值读到相近列；发布原子生效、失败保留旧版映射、同一冻结输入重试不新增版本。
- 5 个页面：首页（场景 + 样例 + 演示步骤）、对比工作台、结果页、路径别名工作台（方案 → 发布 → 历史）、复核与导出（历史 + SQL 下载 + 矩阵说明）。
- 运行记录以 JSON 落在 `/app/data/runs/` 与 `/app/data/alias_runs/`（compose 命名卷），同一输入可反复重跑，历史可回看；生效映射按表版本化落在 `/app/data/aliases/{table}/`。

## 核心规则（纯函数模块）

| 模块 | 文件 | 规则 |
|---|---|---|
| 字段匹配 | `app/matching.py` | id 相同优先；id 同名称变 → renamed；名称同 id 变 → id_changed（人工确认）；无匹配 → added/removed |
| 类型矩阵 | `app/types.py` | 显式提升/阻断表；decimal 精度提升兼容、收窄阻断、标度变更 unknown |
| 兼容判定 | `app/compat.py` | 双向各自判定后合成级别；分区字段增删改类型双向阻断 |
| 迁移计划 | `app/migration.py` | 固定顺序出 ALTER，阻断项不产出 ALTER，删列最后 |
| 引用检查 | `app/consumers.py` | 逐文件逐列比对匹配结果，输出 broken / needs_update / unknown |
| 报表编排 | `app/report.py` | 汇总成可重放的运行记录 + 下载脚本 |
| 路径别名 | `app/aliases.py` | 展平嵌套 schema；搬迁候选逐条核对编号/类型/父级链；冲突或缺失整批隔离；不按名称相近猜测归属 |
| 别名发布 | `app/alias_store.py` | 版本文件 → 索引 → current 指针原子切换；指纹幂等（重试/回滚不新增版本）；失败保留旧版映射 |

## 路径别名口径

- **身份只认 field_id**：同一 id 在两侧各出现一次且路径不同才是搬迁候选；无 id 字段不参与判定。
- **逐条核对**：编号（id 两侧相同且唯一）、类型（完全一致，别名只做路径映射不做转换）、父级链（新路径祖先逐段存在且都是 struct）；分区字段跨层搬迁直接算冲突。
- **整批隔离**：任一冲突（类型不一致、结构/叶子互变、父级链断裂、分区搬迁）或缺失（id 在新 schema 丢失、无 id 路径消失）→ 本批一条别名都不生效；相近列（同名不同 id）只标注为新增，绝不顶替旧字段。
- **发布语义**：先写版本文件再切 `current` 指针，任一步失败旧版映射继续生效；同一冻结输入（指纹相同）重试或回滚都复用已有版本号，不新增版本。
- **一致性**：工作台预演、历史记录、下载产物（`alias.json` / `rewrite.sql`）都来自同一条运行记录，对应同一次生效结果。

## 启动

```bash
docker compose up -d --build
curl -s http://127.0.0.1:18216/health
```

页面：<http://127.0.0.1:18216/>

## 演示步骤

1. 首页点「用样例会 B 执行对比」，工作台预填 `dwd_payment` 旧/新 schema 与数据文件清单。
2. 点「执行对比」→ 结果页：改名 1、新增 2、回填 1、`decimal(10,2)→(18,2)`，兼容级别 `backward`，ALTER 4 条。
3. 换成样例 C（`dwd_shipment`）→ 级别 `none`、3 条阻断原因、`broken_consumers` 若干、`can_execute=false`。
4. 到「复核与导出」看历史、下载 `*.sql`、查看完整矩阵；同一输入可重复执行。
5. 到「路径别名工作台」选样例 D（`dwd_events` 搬层）→「生成方案」→ 4 条别名核对通过 →「发布生效」得 `v0001`；再发布一次同一冻结输入，版本不变。
6. 换样例 E → 整批隔离（冲突 1 + 缺失 3），发布被拒绝，当前生效映射不变；下载的 `rewrite.sql` 只含隔离说明，不含任何生效 SELECT。

样例：

| 样例 | 表 | 预期 |
|---|---|---|
| A | `dwd_orders` | `full`，0 条迁移 SQL |
| B | `dwd_payment` | `backward`，rename 1 / added 2 / 回填 1 / ALTER 4 |
| C | `dwd_shipment` | `none`，3 条阻断 + broken_consumers |
| D | `dwd_events` | 别名 `validated`，4 条别名可发布 |
| E | `dwd_events` | 别名 `quarantined`，冲突 1 + 缺失 3，不产出任何别名 |

## API / 页面

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 首页（场景、样例、演示步骤） |
| GET | `/workbench?sample=A|B|C` | 对比工作台（预填样例） |
| POST | `/api/compare` | `{sample}` 或 `{old,new,consumers}`；返回 `run_id`/`summary`/`redirect`；非法输入 400 |
| GET | `/result/{run_id}` | 结果页（匹配表、类型结论、兼容级别、迁移、broken_consumers） |
| GET | `/review` | 历史对比 + SQL 下载 + 矩阵说明 |
| GET | `/api/runs`、`/api/runs/{run_id}` | 运行记录 |
| GET | `/api/runs/{run_id}/migration.sql` | 下载迁移脚本 |
| GET | `/api/matrix` | 完整类型矩阵 |
| GET | `/api/samples/{key}` | 样例原文 |
| GET | `/aliases?sample=D|E` | 路径别名工作台（预演 + 当前生效映射 + 历史） |
| POST | `/api/aliases/plan` | `{sample}` 或 `{old,new}`；生成方案并落运行记录；非法输入 400 |
| GET | `/aliases/runs/{run_id}` | 方案页（核对明细、发布、产物下载、冻结输入） |
| POST | `/api/aliases/publish` | `{run_id}`；发布生效（幂等）；隔离批次 409；失败保留旧版映射 |
| GET | `/api/aliases/runs`、`/api/aliases/runs/{run_id}` | 别名运行记录 |
| GET | `/api/aliases/runs/{run_id}/alias.json`、`/rewrite.sql` | 下载别名映射 / 兼容视图 |
| GET | `/api/aliases/current?table=` | 当前生效的别名映射（版本 + 别名 + 指纹） |
| GET | `/api/aliases/samples/{key}` | 别名样例原文 |
| GET | `/health` | 健康检查 |

## 目录

```text
app/            main.py + 8 个纯函数模块 + templates/ + static/
sample_data/    a/b/c 三组 old/new/consumers.json + index.json；alias/ 下 d/e 两组嵌套 schema
tests/          test_matching / test_types / test_compat / test_migration / test_api / test_aliases
data/           运行记录与生效映射（本地直跑时生成，已 gitignore）
```

## 已知限制

- 只做 schema 级别的静态判定，不读取 Parquet 文件字节，也不校验数据文件真实 id 编码；`consumers.json` 里的列引用来自人工/采集清单。
- 类型矩阵是显式小表，未覆盖的合法类型对（如 `string→double`、`timestamp→date`）返回 `unknown`，需人工确认，不自动推断。
- `ALTER TABLE` 为通用 SQL 片段（Spark/Trino 语义），不连接具体引擎执行；回填动作只给建议 SQL 与对象清单。
- 路径别名只处理「同 id 跨层搬迁」：类型变化、删列等请走演进对比流程；别名视图 SQL 同样不连接引擎执行。
- 嵌套 schema 用 `{"type": "struct", "fields": [...]}` 描述，最深 8 层；分区字段只能是顶层叶子；字段名不允许含点号。
- 运行记录保存最近 200 条（别名方案同样 200 条），超出按修改时间裁剪；`data/` 被 gitignore，容器重建需通过命名卷保留。
- 无登录/权限，单机演示定位。

