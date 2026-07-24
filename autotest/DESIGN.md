# 页面自动化数据验证工具 —— 设计文档

> 目标：给一个后台管理系统的页面地址，自动识别页面结构、生成验证用例、
> 自动登录并执行，产出可读的报告。
>
> 版本：v1.0 ｜ 代码量约 2900 行 ｜ 状态：核心链路已跑通

---

## 一、要解决什么问题

以订单列表页为例，测试同学目前的手工流程：

1. 打开页面，看表格有没有数据
2. 在「下单人手机号」填 222，点搜索
3. 肉眼核对每行手机号是不是都含 222
4. 点导出，下载 Excel，打开，对着页面一行行核对金额
5. 换个筛选条件，重复 1–4
6. 明天再来一遍

问题不在于「慢」，而在于三点：

- **不可重复**：肉眼核对 50 行数据，漏看一行没人知道
- **不覆盖**：导出 Excel 有 1800 行，实际只会抽查前几行
- **不回归**：改了个不相干的功能，没人会把所有筛选条件重跑一遍

这个工具把上述流程变成：写一次规则 → 一条命令（或点一个按钮）→ 几分钟后拿到报告。

### 明确不做的事

- **不做全自动断言**。见第三章「边界」。
- **不做性能/压力测试**。只验证数据正确性。
- **不替代人工探索测试**。它只能验证你告诉它的规则。

---

## 二、整体架构

```
                    ┌─────────────────┐
                    │  Web 控制台      │  浏览器操作，不用 SSH
                    │  server.py      │  SSE 实时日志
                    └────────┬────────┘
                             │ subprocess
                    ┌────────▼────────┐
                    │     cli.py      │  命令行入口
                    └────────┬────────┘
         ┌───────────────────┼───────────────────┐
         │                   │                   │
   ┌─────▼─────┐      ┌──────▼──────┐     ┌──────▼──────┐
   │ scanner   │      │   runner    │     │   report    │
   │ 识别页面   │      │  执行编排    │     │  生成报告    │
   │ 生成配置   │      └──────┬──────┘     └─────────────┘
   └───────────┘             │
                    ┌────────┼────────┬──────────┐
                    │        │        │          │
              ┌─────▼───┐ ┌──▼───┐ ┌──▼─────┐ ┌──▼────────┐
              │ login   │ │actions│ │adapters│ │ normalize │
              │ 自动登录 │ │动作库 │ │UI适配层│ │  值归一化  │
              └─────────┘ └───────┘ └────────┘ └───────────┘
```

### 模块职责

| 模块 | 行数 | 职责 |
|---|---:|---|
| `engine/actions.py` | 385 | 动作与断言注册表，26 个可用动作 |
| `engine/scanner.py` | 341 | 打开页面识别结构，生成配置草稿 |
| `server.py` | 295 | Web 控制台后端 |
| `engine/login.py` | 291 | UI 自动登录、懒登录、验证码检测 |
| `engine/runner.py` | 259 | 执行编排、上下文、用例隔离 |
| `engine/adapters/element_ui.py` | 217 | Element UI 控件操作封装 |
| `engine/export_verify.py` | 185 | 导出下载与 Excel 比对 |
| `cli.py` | 161 | 命令行入口 |
| `engine/normalize.py` | 141 | 值归一化与比较 |
| `engine/report.py` | 107 | HTML / JSON 报告 |
| `engine/models.py` | 88 | 数据结构定义 |
| `web/index.html` | 425 | Web 控制台前端 |

### 三条关键分层

**1. 适配层隔离 UI 框架**

`.el-form-item__label` 这类选择器全部关在 `adapters/element_ui.py` 里，
上层动作只调用抽象接口：

```python
ui.fill(page, "下单人手机号", "222")     # 而不是 page.locator(".el-form-item...")
ui.column_values(page, "订单金额")        # 而不是解析 DOM
```

换成 Ant Design 只需照同样接口写一个 `antd.py`，配置和用例一行都不用改。

**2. 配置驱动，不写脚本**

用例是 YAML 声明而非 Python 代码，测试同学不用会 Playwright 也能加用例：

```yaml
- name: 搜索-下单人手机号
  steps:
    - fill: { label: 下单人手机号, value: "222" }
    - search: null
    - assert_column_all: { column: 下单人手机号, contains: "222" }
```

代价是灵活性受限，所以留了注册机制兜底：写个函数加 `@action("xxx")`
装饰器，配置里立刻可用，不用改引擎任何其他地方。

**3. 归一化层独立**

页面 `73.06 ¥`、Excel `73.06`、接口 `7306`（分）—— 判断这三者是否一致
的逻辑集中在 `normalize.py`，被断言、导出比对、接口比对三处复用。

---

## 三、核心设计决策

### 3.1 为什么是「机器生成骨架 + 人工补业务断言」

这是整个工具最重要的一个判断。

**机器能推导的（结构性规则）：**

| 规则 | 依据 |
|---|---|
| 搜索「手机号=222」后，该列每行都应含 222 | 搜索项 label 能映射到表格列名 |
| 时间筛选后，创建时间应落在所选区间内 | 列的数据类型可从样本值猜出 |
| 导出行数应等于分页总数 | 页面上有「共 1832 条」 |
| 重置后，所有输入框应为空 | 输入框可枚举 |

**机器推导不了的（业务规则）：**

- 订单金额是否应等于实付金额 + 平台抵扣总额
- 「已取消」状态下司机字段是否允许为空
- 某个筛选条件下是否应该排除测试订单

这些要读需求文档才知道。硬做全自动的结果，是生成一堆
`assert_row_count: {min: 0}` 这种永远通过的废用例——跑得再快也抓不到 bug。

**所以分工是**：扫描器生成约 80% 的结构性用例（最枯燥、最耗时的部分），
人工补 20% 的业务断言（最有价值的部分）。

对应到代码，`scanner.to_config()` 生成的用例里，需要人工补充的会
标记 `skip: true`，填完信息再打开。

### 3.2 值归一化是成败关键

这一层最容易被低估，但不做的话**任何比对都会 100% 误报**。

同一个订单金额，三处表示完全不同：

| 位置 | 值 |
|---|---|
| 页面 | `2356.1 ¥` |
| Excel | `2356.10`（或 float `2356.1`） |
| 接口 | `235610`（单位：分） |

`normalize.py` 处理：

- **货币**：符号、千分位逗号、全角半角
- **日期**：7 种格式 + 秒/毫秒时间戳
- **空值别名**：`-` / `--` / `—` / `无` / `N/A` / `暂无`
- **截断值**：列宽不够时页面显示 `用户Bcl...`，退化为前缀匹配

**一个刻意的决定**：金额比对容差设为 `0.001` 而非 `0.01`。

```python
tolerance = 0.001 if kind == "money" else 0.01
```

1 分钱的差异正是要抓的四舍五入 bug。开发时第一版用了 0.01，
测试发现 `73.06` 和 `73.07` 被判定为「一致」，等于把目标 bug 放过去了。

### 3.3 等接口比等元素稳

Element UI 表格没有可靠的 loading 结束信号，`wait_for_timeout(2000)` 是玄学。

方案是等列表接口的响应：

```python
with page.expect_response(lambda r: "/order/list" in r.url and r.status == 200) as info:
    page.click("button:has-text('搜索')")
api_data = info.value.json()      # 顺手拿到接口原始数据
```

好处有二：判断准确；并且**顺手拿到了接口原始数据**，这直接催生了下一条。

### 3.4 接口数据 vs 表格渲染，是 ROI 最高的断言

后台系统里，「接口返回正确但前端展示错误」的频率**远高于**接口本身出错。
典型的三类：

- 金额单位没转换（后端返回分，前端当元显示）
- 时区差 8 小时
- 状态码映射不全（新增的状态显示成空白或原始数字）

`assert_api_matches_table` 专门抓这类：

```yaml
- assert_api_matches_table:
    list_path: data.records
    mapping:
      订单金额: orderAmount      # 页面列名 → 接口字段名
      创建时间: createTime
```

它把接口 JSON 和表格 DOM 逐行逐字段比对（比对时走归一化层）。
这是唯一需要人工填接口字段名的断言，但值得填。

### 3.5 懒登录

不是每次执行都登录。流程是：

```
访问目标页
  ├─ 没跳转 & 期望元素存在  → 复用旧 cookie，跳过登录
  └─ 跳到登录页 / 期望元素缺失 → 执行 UI 登录 → 保存新 cookie
```

**为什么不每次都登**：会在业务系统里堆登录日志，如果有异地登录风控还可能触发告警。
实际效果是一天可能只登一次。

**`expect_selector` 的必要性**：开发时发现只看「是否跳转到登录页」会漏判——
单页应用可能 URL 不变，只是渲染成未登录的空壳。这样会导致后面每条用例
都在空页面上失败。所以加了「指定一个登录后才会出现的元素，找不到就当作未登录」。

```yaml
login:
  expect_selector: ".el-table"    # 订单列表页登录后必然有表格
```

**会话中途过期**也会自动重登：`run_case` 每次执行前会检查，掉回登录页就就地重登，
而不是让后续用例连锁失败。

### 3.6 用例隔离与快速失败

**隔离**：每条用例执行前重新 `goto` 页面。用例之间互相污染（上一条的筛选条件
残留到下一条）是这类工具最常见的假失败来源。

**快速失败**：用例内部一旦某步失败，立即停止后续步骤。因为后面的步骤依赖前面的
状态，继续跑只会产生一堆无意义的连带失败，淹没真正的错误。

```python
for step in case.steps:
    r = run_step(ctx, step)
    results.append(r)
    if r.status in (Status.FAIL, Status.ERROR):
        break          # 快速失败
```

**失败与错误分开**：

| 类型 | 含义 | 处理 |
|---|---|---|
| `AssertionFailed` | 业务问题 —— **你的系统可能有 bug** | 标记 FAIL，截图 |
| 其他 `Exception` | 脚本/环境问题 —— 工具自己的问题 | 标记 ERROR，截图 + 堆栈 |

报告里两者用不同颜色区分，避免把工具的问题当成系统的 bug 去排查。

---

## 四、完整工作流

### 4.1 扫描：从 URL 到配置

```
python cli.py scan http://.../web/order/list -o configs/order.yaml
```

内部做四件事：

**① 识别搜索表单**

遍历 `.el-form-item`，对每一项判断控件类型：

```python
if item.locator(".el-date-editor").count() > 0:
    kind = "date_range" if 有 .el-range-input else "date"
elif item.locator(".el-select").count() > 0:
    kind = "select"
    extra["options"] = 点开下拉枚举选项    # 供生成筛选用例
else:
    kind = "text"
```

**② 识别表格**

抓表头（只取 `.el-table__header-wrapper`，避免固定列产生的重复表头），
抓第一行样本值，据此猜每列的数据类型：

| 样本值 | 猜测类型 |
|---|---|
| `2356.1 ¥` | money |
| `2026-07-23 10:14:42` | date |
| `12345678900` | phone |
| `222` | number |

**③ 识别按钮与分页**

搜索/重置/导出/新增/编辑/删除是否存在；分页总数是多少。

**④ 探测列表接口**

监听页面所有 XHR，挑 URL 里含 `list|page|query|search` 的那个，取路径片段。

然后 `to_config()` 按这些信息组装用例。对当前订单页，实测生成 12 条：

```
- 列表默认加载        [smoke]
- 搜索-订单号         [search]
- 搜索-下单人手机号    [search]
- 搜索-司机手机号      [search]
- 筛选-国家           [search]
- 筛选-城市           [search]
- 筛选-订单状态        [search]
- 时间筛选-下单时间     [search]
- 重置条件            [search]
- 分页与排序          [list]
- 接口与表格渲染一致    [consistency]   ← skip，需填接口字段名
- 导出数据验证         [export]
```

**一个细节**：搜索用例的搜索词不是随机生成的，而是**从表格样本值里取**。
搜「222」保证有结果，能验证「搜出来的都对」；搜随机串只能验证「搜不出来」。

### 4.2 列名映射：一个刻意保守的设计

生成搜索断言需要把搜索项 label 映射到表格列名。第一版实现是：

```python
core = re.sub(r"(请输入|请选择|是否|类型|状态)", "", label)   # 去修饰词
for h in headers:
    if core in h: return h                                  # 模糊匹配
```

测试发现它把 **`订单状态` 错配到了 `订单金额`** —— 去掉「状态」后剩「订单」，
而「订单金额」也含「订单」。

这会生成一条**永远失败**的断言，让人以为系统有 bug，实际是工具错了。
**错配比不配危险得多**，所以改成只接受精确匹配和有意义的完整包含：

```python
# 短的一方至少 3 个字符才认，避免「号」「人」这类噪声命中
short, long_ = (lb, h) if len(lb) <= len(h) else (h, lb)
if len(short) >= 3 and short in long_:
    best = h if best is None or len(h) < len(best) else best   # 取最短候选
```

实测结果（10 个 label 全部正确）：

```
下单人手机号  → 下单人手机号      订单号    → None
司机手机号    → 司机手机号        国家      → None
订单状态      → 订单状态          是否代叫  → None
乘车人        → 乘车人            下单时间  → None
```

返回 `None` 意味着不生成列断言，只断言「搜索能执行、有结果」。宁可少测，不可误报。

### 4.3 执行

```
run_page()
  ├─ 启动 Chromium（headless，带 --no-sandbox --disable-dev-shm-usage）
  ├─ 载入 storage_state（已有登录态）
  ├─ ensure_logged_in()  ← 懒登录
  └─ for case in cases:
        ├─ goto 页面（隔离）
        ├─ 检查会话是否过期，过期则重登
        └─ for step in steps:
              ├─ 查 REGISTRY 找到动作函数
              ├─ 执行，捕获 AssertionFailed / Exception
              └─ 失败则截图 + break
```

执行时同时挂了三个监听器，用于健康检查：

```python
page.on("console", ...)        # 收集前端报错
page.on("requestfailed", ...)  # 收集失败请求
page.on("response", ...)       # 收集 5xx
```

对应 `assert_no_console_error` 和 `assert_no_failed_request` 两个断言。

### 4.4 导出验证

导出是这类页面最容易出问题、也最少被测的功能。四层校验：

| 层 | 检查什么 | 能抓到的问题 |
|---|---|---|
| 1 | 文件能下载、非空、能解析 | 导出接口 500、生成空文件 |
| 2 | 表头包含页面上所有业务列 | 漏导某几列 |
| 3 | 行数等于分页总数 | **只导了当前页 / 有条数上限** |
| 4 | 抽样行字段值与页面一致 | **单位错、时区错、格式错** |

**特别值得测的是「导出是否遵循搜索条件」**。筛选后导出却导了全库，
是常见且严重的数据泄露问题，所以单独留了一条用例。

**两种导出模式**：

```yaml
export_mode: auto     # direct | async | auto
```

- `direct`：点按钮直接触发浏览器下载
- `async`：点按钮只创建任务，需轮询任务接口拿下载链接（大数据量后台常见）
- `auto`：先试 direct（20 秒），超时自动降级到 async

异步模式下用 `page.request` 而非 `requests` 去下载，是为了复用登录态 cookie。

**文件解析的兼容处理**（都是实测踩到的）：

- xlsx / xls / csv 三种格式
- CSV 编码依次尝试 `utf-8-sig` → `gbk` → `utf-8`（国内后台常见 GBK）
- 首行是合并大标题、真表头在第二行的情况

第三点值得展开：pandas 读这种文件时，**首列会拿到标题文本本身，只有后续列才是
`Unnamed:N`**。第一版判断 `columns[0].startswith("Unnamed")` 完全失效，
改成按 Unnamed 占比判断：

```python
unnamed = sum(1 for c in df.columns if c.startswith("Unnamed"))
if len(df) > 0 and unnamed >= max(1, len(df.columns) // 2):
    df.columns = df.iloc[0]; df = df.iloc[1:]
```

### 4.5 Web 控制台

避免每次 SSH 到服务器敲命令。

**后端设计要点**：

- 执行放**后台线程**，HTTP 接口立即返回。浏览器不会挂在那儿等五分钟
- 日志走 **SSE 流式推送**，能看到「正在跑第 3 条用例」而不是黑盒等待
- **同时只允许一个任务**。Chromium 很吃内存，并发容易 OOM；且多任务
  操作同一套测试数据会互相干扰。第二个请求返回 409
- 刷新页面能**接上正在跑的任务**（`/api/status` 返回已有日志）

**安全边界**（都是必要的，因为参数直接进 subprocess）：

```python
# 配置文件名：只允许 configs/ 下的 yaml，禁止路径穿越
if "/" in cfg or "\\" in cfg or not cfg.endswith(".yaml"): reject

# 扫描 URL：必须 http(s)，防止 file:// 读本地文件
if not re.match(r"^https?://", url): reject

# 保存配置：先 yaml.safe_load 校验语法再落盘
```

实测均已拦截：

```
block '../../etc/passwd'   → 400 配置文件名不合法
block 'x.yaml/../../y'     → 400 配置文件名不合法
block file:// scan         → 400 URL 必须以 http:// 开头
reject bad yaml            → 400 YAML 语法错误
```

**接口清单**：

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/configs` | 配置列表（含用例数、目标 URL） |
| GET | `/api/reports` | 历史报告（含通过/失败统计） |
| GET | `/api/status` | 当前任务状态 + 已有日志 |
| GET | `/api/stream` | SSE 实时日志 |
| POST | `/api/run` | 触发 run / test-login / scan |
| POST | `/api/stop` | 终止当前任务 |
| GET/POST | `/api/config/<name>` | 读写配置（后端已就绪，前端编辑器待做） |

---

## 五、能力清单

### 26 个可用动作

**动作类（14）**

| 动作 | 用途 |
|---|---|
| `goto` | 打开页面 |
| `fill` | 按 label 填输入框 |
| `select` | 按 label 选下拉（支持按文本或索引） |
| `date_range` | 选日期范围 |
| `click` | 点击（支持选择器别名或文本） |
| `search` | 点搜索 + 等接口返回（复合动作） |
| `wait` / `wait_api` | 等待时间 / 等待接口 |
| `capture` | 抓当前表格快照存变量 |
| `capture_all_pages` | 翻页抓全量 |
| `fill_form` | 弹窗内批量填表 |
| `confirm` | 确认弹窗 |
| `export_and_verify` | 导出 + 四层校验 |
| `screenshot` | 手动截图 |

**断言类（12）**

| 断言 | 用途 |
|---|---|
| `assert_row_count` | 行数（min/max/equals） |
| `assert_headers` | 表头包含/完全匹配 |
| `assert_column_all` | 某列所有值满足条件（equals/contains/matches） |
| `assert_column_range` | 某列值在范围内（日期/数值） |
| `assert_column_not_empty` | 空值率不超过阈值 |
| `assert_sorted` | 排序正确 |
| `assert_inputs_empty` | 输入框已清空（重置用） |
| `assert_api_matches_table` | 接口与表格渲染一致 |
| `assert_message` | 提示消息内容 |
| `assert_in_list` | 新增/修改后能在列表搜到 |
| `assert_no_console_error` | 无前端报错 |
| `assert_no_failed_request` | 无失败请求 |

### 变量占位符

| 占位符 | 展开为 |
|---|---|
| `${random}` | 6 位随机字符串 |
| `${timestamp}` | 时间戳 |
| `${email}` / `${phone}` | 随机邮箱 / 手机号 |
| `${today}` / `${now}` | 今天 / 当前时刻 |
| `${days_ago_N}` | N 天前 |
| `${selected_XX}` | 下拉刚选中的文本 |
| `${form_XX}` | 表单刚填入的值 |

后两个用于闭环断言：选了下拉，回来断言列值等于所选项；填了表单，
提交后回列表断言能搜到刚填的值。

**为什么新增/修改必须做闭环**：只断言 toast「操作成功」是没有意义的——
提示成功但数据没落库的 bug 很常见。所以流程是
`填表 → 提交 → 断言提示 → 回列表搜索 → 断言字段值一致`。

---

## 六、开发过程中修掉的问题

这些都是实测发现并修复的，记录下来是因为它们代表了这类工具的典型陷阱。

| # | 问题 | 后果 | 修复 |
|---|---|---|---|
| 1 | 金额容差 0.01 | 1 分钱的舍入 bug 被放过 | 金额专用容差 0.001 |
| 2 | Excel 首行大标题判断错 | 表头读成 `Unnamed:1`，比对全错 | 按 Unnamed 占比判断 |
| 3 | 列名模糊匹配 | `订单状态`→`订单金额`，产生永远失败的断言 | 只接受精确/完整包含 |
| 4 | 只看 URL 判断登录态 | 单页应用空壳页漏判，后续用例全挂 | 加 `expect_selector` |
| 5 | `networkidle` 等待 | 有轮询/长连接的页面永远超时 | 改 `domcontentloaded` + 60s |
| 6 | Docker 镜像与包版本错配 | 浏览器可执行文件找不到 | 两边都钉死 1.61.0 |

第 5 条值得展开：`networkidle` 要求「500ms 内无任何网络请求」。而后台系统常有
消息轮询、心跳、地图组件，永远达不到这个条件。改用 `domcontentloaded` 后，
后续每个动作 Playwright 都会自动等元素出现，不需要靠 goto 保证加载完成。

---

## 七、边界与已知限制

| 限制 | 说明 | 缓解 |
|---|---|---|
| 只认 Element UI | 其他框架需写 adapter | 接口已抽象，照 `element_ui.py` 写 |
| 有验证码则无法自动登录 | 图形验证码/滑块 | 提前检测并明确报错；退回本地登录传 `auth/state.json` |
| 新增/修改只能生成骨架 | 字段名、校验规则机器猜不出 | 生成时标 `skip: true` |
| 强依赖 label 文本 | 页面文案改了配置要跟着改 | 配置驱动的固有代价 |
| 会污染测试环境 | 造的数据留在库里 | 用独立账号；数据打 `auto_` 前缀便于清理 |
| 控制台无鉴权 | 谁能访问 5000 端口谁能触发 | 防火墙限 IP，或只监听 127.0.0.1 + SSH 隧道 |

---

## 八、部署

### Docker（推荐）

```bash
docker compose up -d --build
# 浏览器打开 http://服务器IP:5000
```

`docker-compose.yml` 挂载四项：

- `configs/` —— 改配置不用重新 build
- `reports/` —— 报告留在宿主机
- `.env` —— 账号密码（只读挂载）
- `auth/state.json` —— 登录态持久化，容器重启不用重登

另设 `shm_size: 1gb`，Chromium 开大页面时不加会崩。

### 裸机

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium     # --with-deps 不能省
python server.py
```

### 定时执行

```bash
0 8 * * * cd /data/autotest && ./venv/bin/python cli.py run configs/order_list.yaml \
  >> logs/cron.log 2>&1 || curl -X POST "机器人webhook" -d '{"msgtype":"text",...}'
```

两个坑：必须用 venv 里 python 的**全路径**；cron 读不到 shell 的 `export`，
所以程序启动时会自动读 `.env` 文件。

`run` 失败返回非 0 退出码，可直接接 CI 或告警。

---

## 九、落地路径

```
第 1 步  配 .env，test-login 验证能登进去
第 2 步  跑通订单页：补接口字段映射（F12 看 JSON 字段名），开启一致性用例
第 3 步  遍历左侧菜单，逐页 scan 生成骨架配置
第 4 步  按页面重要性排序，逐个补业务断言
第 5 步  接 CI / crontab，失败告警到群
```

**优先级建议**：先覆盖「有导出功能」和「有金额字段」的页面。这两类
出问题的概率最高，且人工核对成本最大。

---

## 十、后续可做

按投入产出比排序：

1. **配置编辑器**：后端 `GET/POST /api/config/<name>` 已就绪，
   前端加个带 YAML 语法校验的编辑器即可，不用再 SSH 改文件
2. **批量执行**：一次跑多个配置，汇总成一份报告
3. **定时任务纳入界面**：现在还是 crontab，可以做成页面上配置
4. **趋势对比**：同一配置多次执行的通过率曲线，能看出是偶发还是持续失败
5. **Ant Design 适配器**：如果有其他技术栈的系统
6. **控制台鉴权**：简单的 Basic Auth 或 token 即可

---

## 附：文件清单

```
autotest/
├── DESIGN.md                      本文档
├── README.md                      使用说明
├── cli.py                         命令行入口
├── server.py                      Web 控制台后端
├── Dockerfile / docker-compose.yml
├── requirements.txt
├── .env.example                   账号密码模板（复制成 .env）
├── configs/
│   └── order_list.yaml            订单页配置（13 条用例）
├── engine/
│   ├── models.py                  数据结构
│   ├── scanner.py                 页面识别 + 配置生成
│   ├── runner.py                  执行编排
│   ├── login.py                   自动登录
│   ├── actions.py                 动作与断言注册表
│   ├── normalize.py               值归一化
│   ├── export_verify.py           导出校验
│   ├── report.py                  报告生成
│   └── adapters/
│       └── element_ui.py          Element UI 适配
└── web/
    └── index.html                 控制台前端
```
