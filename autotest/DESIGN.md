# 页面自动化数据验证工具 —— 设计文档

> 目标：给一个后台管理系统的页面地址，自动识别页面结构、生成验证用例、
> 自动登录并执行，产出可读的报告。
>
> 版本：v2.0 ｜ 代码量约 6200 行 ｜ 状态：第一期（列表/搜索/导出）+
> 第二期（新增/修改/详情/删除闭环、状态流转）已跑通

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
| `engine/actions.py` | 965 | 动作与断言注册表，39 个可用动作（含第二期 CRUD 闭环、多语言检查，全部通过 `ctx.label_of`/`ctx.column_of` 等做多语言解析） |
| `engine/scanner.py` | 813 | 打开页面识别结构 + 表单 Schema，生成配置草稿；配了 `languages` 会自动扫描合并 `label_variants`/`header_variants` |
| `web/index.html` | 760 | Web 控制台前端 |
| `server.py` | 654 | Web 控制台后端 |
| `engine/adapters/element_ui.py` | 588 | Element UI 控件操作封装，label/column 支持多语言候选文案列表 |
| `engine/crawler.py` | 468 | 菜单爬取 |
| `engine/explain.py` | 380 | 把 YAML 用例翻译成人话，供不看代码的人核对 |
| `engine/runner.py` | 373 | 执行编排、上下文、用例隔离、WARN 分级、`target_language` 执行时切语言 |
| `engine/login.py` | 291 | UI 自动登录、懒登录、验证码检测 |
| `engine/batch.py` | 273 | 批量扫描/执行，并发跑多个页面 |
| `cli.py` | 231 | 命令行入口 |
| `engine/project.py` | 195 | 项目（登录信息、语言配置 + 菜单地图）管理 |
| `engine/export_verify.py` | 185 | 导出下载与 Excel 比对 |
| `engine/normalize.py` | 141 | 值归一化与比较 |
| `engine/datafactory.py` | 138 | 测试数据工厂：按字段类型生成合法值 |
| `engine/report.py` | 112 | HTML / JSON 报告 |
| `engine/models.py` | 96 | 数据结构定义（含 Status.WARN、`label_variants`/`header_variants`） |
| `engine/state.py` | 50 | 登录态文件的安全读写 |
| `engine/i18n_terms.py` | 39 | 按钮/菜单关键词的中英文对照表 |
| `engine/lang_variants.py` | 38 | 表单 label / 表头列名的多语言文案查找表（candidates / reverse_map） |
| `engine/browser.py` | 36 | 统一的 Chromium 启动参数 |
| `engine/progress.py` | 25 | 结构化执行进度上报 |
| `engine/tz.py` | 25 | 统一北京时间 |

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

**级联下拉**（如「城市」依赖「国家」先选，没选父级前是 disabled 的）：
第一遍点不开、options 是空的下拉，会依次尝试先选前面某个下拉的第一个真实
选项，等一下再重新探测——测出来了就记下"先选谁选的什么"（`depends_on`），
生成用例时自动把这一步补在前面：

```yaml
- name: 筛选-城市（联动国家）
  steps:
  - select: {label: 国家, option: 中国}
  - wait: 500
  - check_select_options: {label: 城市}
  - assert_column_all: {column: 城市, equals: "${selected_城市}"}
```

不这么处理的话，生成的「筛选-城市」用例执行时会在一个 disabled 的下拉上
硬等到超时——和扫描阶段最初踩的坑一样。只探测"排在它前面"的下拉当父级，
不做多级级联的穷举，够用但不完美。

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
page.on("requestfailed", ...)  # 收集失败请求（网络层面，如断连）
page.on("response", ...)       # 收集 4xx/5xx（含图片等资源 404）
```

对应 `assert_no_console_error` 和 `assert_no_failed_request` 两个断言。

**这两个断言只降级成警告（`WARN`），不判失败**：如果 `assert_row_count` /
`assert_headers` / `assert_no_render_garbage` 这类"页面内容本身对不对"的
断言都通过了，说明用户看到的东西没问题，一条无关的资源报错（比如某张图片
404）不该让整条用例变红。`WARN` 计入通过数，报告里用黄色徽标单独标出，
不影响通过率。

真正内容不对（行数不对、表头缺列、渲染出乱码）导致断言失败时，失败消息
会自动带上当时的控制台/网络错误做上下文（`_diag_suffix`），不需要靠
`assert_no_console_error` 单独失败来定位——这样"页面出不来"时能看到线索，
"页面没问题"时不会被无关报错拖累。

**并发下的接口超时——重试 + 可调超时**：批量执行默认并发跑多个页面，
同一时间好几个 Chromium 一起打同一个后端，接口响应会比单独手动测慢一截，
偶尔卡过超时线（接口本身没问题，只是那一刻撞上了并发高峰）。`search`
动作因此：
1. 等接口响应的超时从写死的 20s 提到 30s，页面配置里可以用
   `search_timeout`（毫秒）单独再调；
2. 第一次超时不直接判失败，隔一拍重试一次，两次都超时才是真的该报的
   问题（返回消息会标"重试后成功"，不会悄悄掩盖真实问题）。
3. `batch.run_selected()` 给起步的并发 worker 错峰（`STAGGER_DELAY_SEC`），
   避免所有 worker 同时登录、同时打第一次搜索，削掉开头几秒的并发高峰。

**接口调用记录**：`Context` 现在会记录每条用例触发的 JSON 接口调用
（方法/URL/状态码/耗时/请求头/入参/响应），存进 `CaseResult.api_calls`，
报告里每条用例下面有个默认折叠的"接口调用"区块——排查"页面显示不对"
时能直接看到到底打了哪些接口、传了什么、返回了什么，不用现场开 F12
重新操作一遍抓包。请求头里的 `Cookie`/`Authorization` 等凭证字段在记录
时就被替换成 `[已隐藏]`（`_REDACT_HEADERS`），报告文件本身会被保存/
分享，不能把会话令牌原样写进去。单条用例最多记 `_API_LOG_LIMIT`（50）条，
避免循环点几十次搜索的用例把报告文件撑得很大。

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

### 4.6 第二期：新增/修改/详情/删除闭环

第一期只验证「页面显示的东西对不对」，这一期开始验证「操作之后，各处是否
真的同步变了」——这类 bug（提示成功但列表没变、列表对但详情是老值、编辑
弹窗回显不出原值）比展示错误更隐蔽，也是手工测试花时间最多的部分。

**核心判断：这一层也是结构性规则，不是业务规则**，理由和第一期一样——
必填靠 `.el-form-item.is-required`、长度上限靠 `maxlength` 属性、下拉选项
靠枚举，全是 DOM 里现成的信息，机器读得出来，不需要人告诉它"这个字段该填
什么样的值"。

**表单结构扫描**（`scan_form_schema`）：点开新增弹窗，把每个字段的
label/类型/是否必填/长度上限/下拉选项读出来，`取消` 关掉，不提交任何数据。
类型判断有顺序讲究——`el-upload` 内部也可能套一个 `<input>`，必须先判
upload 再判 input，不然上传字段会被当成文本框，自动填表时把随机字符串
塞进上传控件里。

**数据工厂**（`engine/datafactory.py`）：按字段类型和语义（label 里带
"手机"/"邮箱"这类关键词）生成合法测试值。所有值带 `auto_` 前缀，两个目的：
人在系统里一眼认出是测试数据；唯一约束字段不会因为重复跑而撞车。

**闭环验证怎么串起来**——以`加盟商`为例，一条用例走完 新增→改回显→改
一个字段→查详情→删除：

```yaml
- create_and_verify: {fields: [...扫描出来的字段结构...], identity: 加盟商名称}
- assert_form_prefilled: {}      # 编辑弹窗回显 == 列表当前值
- edit_and_verify: {fields: {负责人: "${random}"}}
- assert_detail_matches: {}      # 详情弹窗 == 列表当前值
- delete_and_verify: {}          # 清理：删掉自己建的这条
```

`identity` 是这条记录的定位依据（选一个能对上表格列名的必填文本字段），
后面每一步都靠它重新定位这一行——不记住行号，因为搜索/翻页后行号会变。

**铁律：只动自己创建的数据**，靠三层拦截，不是一句注释：

1. `find_row_by` 按 identity 列的值定位行，**匹配上不止一行就直接报错**，
   不会挑一行瞎猜——那可能是别人的真实数据
2. `delete_and_verify` 执行前检查 identity 值是否带 `auto_` 前缀，
   不带前缀（不像自动化数据）**拒绝删除**
3. 找不到待清理记录（没跑过 `create_and_verify`，或已经删过）直接跳过，
   不报错

**快速失败的例外**：`run_case` 一贯的规则是某步失败就跳过后面所有步骤
（3.6 节），但如果闭环用例最后一步是 `delete_and_verify`，中间随便哪步
失败了，这一步**照样会被补跑**——不能因为「改回显」验证失败，就把新增
那步真实创建的测试数据永远留在系统里。`delete_and_verify` 自身的安全检查
（上面第 2、3 条）保证补跑不会因此变得不安全。

### 4.7 多语言

**问题的两面**：目标系统切到别的语言，一是现有工具本身可能失效——扫描
和执行大量靠中文文案精确匹配（表单 label、按钮文字、表头列名）；二是
"切语言"这件事本身可能有 bug（漏翻译、翻译文件没跟上）。两个都要管，
但做法完全不同。

**健壮性——中英文按钮关键词统一维护**：`check_buttons` 巡检靠一份"危险
按钮"名单判断"这个别真点下去"，之前只有中文关键词，切到英文界面后
`Delete` 认不出来会被真的点下去——是安全问题，不只是功能缺失。新增
`engine/i18n_terms.py` 统一维护中英文对照表，`DESTRUCTIVE` 名单、菜单
跳过词、按钮探测这几处都从这里取词，不再各写各的。

**健壮性——表单 label / 表头列名的深度多语言匹配**：按钮关键词是固定的
有限集合，能写死词表；但表单字段名、表格列名是每个页面自己的业务文案，
没法预先枚举，得从页面自己扫出来。核心设计是"canonical 名字 + 译文
查找表"：

```yaml
# 扫描自动生成，canonical 就是默认语言下扫到的原始文案，
# 所有 case YAML 里 label/column 参数继续用这个文案，不用改
label_variants:
  国家名称: {en: "Country Name", fr: "Nom du pays"}
header_variants:
  状态: {en: Status}
```

`engine/lang_variants.py` 提供两个方向的查询：`candidates()` 把
canonical 展开成"所有已知文案"给 Playwright 定位元素用（哪个语言不用
关心，命中哪个用哪个）；`reverse_map()` 把任意已知文案（不管当前渲染的
是哪种语言）翻译回 canonical，给断言按统一 key 取值用。`Context` 在这
之上包了一层 `label_of()`/`column_of()`/`table_data()`/`find_row_by()`/
`canonical_headers()`/`dialog_field_values()`/`detail_values()`/
`form_error_labels()`，`actions.py` 里所有碰表单 label、表头列名的动作
（`fill`/`select`/`check_select_options`/`assert_column_*`/CRUD 闭环
等）统一通过这层解析，不直接传裸字符串给适配器——这样切换语言不需要
改任何一条已有用例。

`label_variants`/`header_variants` 由扫描器自动生成，不用手写：
`scanner.scan()` 配了项目级 `languages` 之后，会在默认语言扫完之后，
依次切到每种配置语言，重新扫一遍搜索表单 label / 表格表头 / 新增弹窗
字段 label（只取文案，不重复探测类型/选项，避免副作用），按 DOM 位置
和默认语言那次的结果对齐——位置数量对不上就整批跳过，宁可缺失也不
错配（`_merge_positional`）。

**执行时选语言**：`run_page`/`batch.run_selected` 新增 `target_language`
参数，选了之后每条用例重新导航到页面后、跑自己的步骤之前，会先用
`switch_language` 切到这门语言（每条用例都要切一次，因为大多数系统的
语言状态挂在前端内存/localStorage，每次 goto 都会被冲掉）。CLI 用
`--lang <code>`，Web 控制台在"只执行勾选的类别"上方新增"执行语言"
下拉（项目没配 `languages` 就不显示），先选语言再勾类别，跟"多语言
检查"（4.7 翻译正确性那条，遍历所有语言各查一次）是两件独立的事：
前者验证"切到这门语言后，其它功能是否还正常"，后者验证"翻译本身对
不对"。

**翻译正确性——新的一类断言，不需要懂业务**：

```yaml
- switch_language: {to: en}
- search: null
- assert_no_i18n_leak: null          # 漏翻译的 key（如 common.search）没被替换
- assert_no_mixed_language: {expect: en}   # 切到英文后不该还有中文残留
```

`switch_language` 需要项目配置里指定切换控件（语言切换器五花八门，没有
像表单/表格那样的统一 DOM 约定，没法零配置自动识别）：

```yaml
languages:
  switcher_trigger: ".lang-switch"   # F12 找触发元素的选择器
  options: {zh: 中文, en: English}    # 每种语言在切换菜单里显示的文字
```

配了这个，扫描时会给每个页面自动补一条「多语言检查」用例，遍历
`options` 里的每种语言各切一次、各查一次，不用逐页手写。

`switcher_trigger` 只能手动 F12 找（没有统一 DOM 约定），但 `options`
里每种语言在菜单里的精确文案可以自动读出来——控制台头部「探测语言选项」
按钮，给了 `switcher_trigger` 之后打开首页点开菜单，把候选文案打印在
运行日志里，照抄进 `project.yaml` 即可，不用再自己一个个抄、抄错一个字
`switch_language` 就永远找不到那个菜单项（`scanner.probe_languages()` /
`cli.py probe-lang`）。

**项目设置怎么改**：`languages` 是项目级配置，加进已有项目不能靠重新
扫描（扫描不会覆盖 login/languages 这类项目级字段）。控制台头部新增
「项目设置」按钮，复用页面用例那套 YAML 编辑弹窗直接改 `project.yaml`——
这顺带补上了一个更早就有的缺口：之前项目创建之后，登录账号密码等信息
没有任何界面能改，只能 SSH 上去手改文件。

---

## 五、能力清单

### 39 个可用动作

**动作类（21）**

| 动作 | 用途 |
|---|---|
| `goto` | 打开页面 |
| `fill` | 按 label 填输入框 |
| `select` | 按 label 选下拉（支持按文本或索引） |
| `date_range` | 选日期范围 |
| `click` | 点击（支持选择器别名或文本） |
| `search` | 点搜索 + 等接口返回（复合动作） |
| `wait` / `wait_api` | 等待时间 / 等待接口 |
| `check_buttons` | 巡检工具栏按钮可用性（破坏性按钮只验存在） |
| `check_select_options` | 遍历下拉每个选项逐一筛选、验不报错 |
| `capture` | 抓当前表格快照存变量 |
| `capture_all_pages` | 翻页抓全量 |
| `fill_form` | 弹窗内批量填表 |
| `confirm` | 确认弹窗 |
| `export_and_verify` | 导出 + 四层校验 |
| `screenshot` | 手动截图 |
| `create_and_verify` | 数据工厂生成值填表提交，验证列表数据一致（见 4.6） |
| `edit_and_verify` | 改指定字段提交，验证列表已同步（见 4.6） |
| `delete_and_verify` | 清理本次创建的记录，只删 auto_ 前缀数据（见 4.6） |
| `toggle_status_and_verify` | 点状态切换按钮（设为失效等），验证状态列真的变了 |
| `switch_language` | 切换页面语言（见 4.7，需项目配置切换控件选择器） |

**断言类（18）**

| 断言 | 用途 |
|---|---|
| `assert_row_count` | 行数（min/max/equals） |
| `assert_headers` | 表头包含/完全匹配 |
| `assert_column_all` | 某列所有值满足条件（equals/contains/matches） |
| `assert_column_range` | 某列值在范围内（日期/数值） |
| `assert_column_not_empty` | 空值率不超过阈值 |
| `assert_no_render_garbage` | 无渲染异常（undefined/[object Object]/裸时间戳等） |
| `assert_sorted` | 排序正确 |
| `assert_inputs_empty` | 输入框已清空（重置用） |
| `assert_api_matches_table` | 接口与表格渲染一致 |
| `assert_message` | 提示消息内容 |
| `assert_in_list` | 新增/修改后能在列表搜到 |
| `assert_no_console_error` | 无前端报错（WARN，不算失败，见 3.6） |
| `assert_no_failed_request` | 无失败请求（WARN，不算失败，见 3.6） |
| `assert_form_errors` | 必填校验：空表单提交，该报错的字段都报了 |
| `assert_form_prefilled` | 编辑弹窗回显和列表当前值一致 |
| `assert_detail_matches` | 详情弹窗和列表当前值一致 |
| `assert_no_i18n_leak` | 无漏翻译的 key（如 common.search）或未渲染的模板占位符 |
| `assert_no_mixed_language` | 切到非中文后，列表里没有残留中文 |

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

**已完成**（原计划里的，不再重复排期）：配置编辑器（YAML 编辑 + 「查看说明」
人话预览，含项目级设置编辑）、批量执行（含并发）、控制台鉴权（fail-closed +
限流）、多语言按钮关键词健壮性 + 翻译正确性检查 + 表单 label/表头列名的
深度多语言匹配（扫描自动合并 + 执行时选语言，见 4.7）。

按投入产出比排序，剩下的：

1. **字段级负向校验**：第二期做了"必填都报错""填对的能存对"，还没做
   "长度超限/格式错误该被拦下"这类负向用例（`assert_input_maxlength` 之类）
2. **跨页面场景层**：新的一层 YAML（场景），串联多个页面的用例、页面间
   传变量——比如"国家管理新增一个国家，加盟商管理的国家下拉能选到它"。
   见设计讨论：这是新增的一层，不是对现有页面配置的改造
3. **权限维度**：模型标注 `roles`，配多账号后同一套用例换账号跑，验证
   "该看到的看到、不该操作的操作不了"
4. **趋势对比**：同一配置多次执行的通过率曲线，能看出是偶发还是持续失败
5. **定时任务纳入界面**：现在还是 crontab，可以做成页面上配置
6. **Ant Design 完整适配器**：`scanner.scan_table()` 已经能兼容识别 Ant
   Design 的表格，但表单扫描（`scan_form_schema`）、运行期适配器
   （`element_ui.py`）都还是 Element UI 专用，要支持 Ant Design 系统
   得照 `ElementUIAdapter` 的接口再写一个

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
│   ├── i18n_terms.py              按钮关键词中英文对照表
│   ├── lang_variants.py           表单 label / 表头列名多语言文案查找表
│   └── adapters/
│       └── element_ui.py          Element UI 适配
└── web/
    └── index.html                 控制台前端
```
