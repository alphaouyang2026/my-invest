# J-Quants API 免费版可用数据调查

调查日期：2026-08-15
范围：面向个人的 J-Quants API（不是 J-Quants Pro 或 DataCube）；以 V2 API 为实现基线。  
来源限制：仅采用 JPX/JPX Market Innovation & Research（JPXI）官网及 J-Quants 官方 GitHub 组织。

## 结论摘要

免费版适合做一个**日本股票、日线、周度调仓的原型**，但不适合严肃验证长期策略：它只提供滚动约 2 年历史，并且最新数据延迟 12 周。当前 V2 免费版的核心数据是：

1. 上市标的主数据（Listed Issue Master）；
2. 股票日线 OHLC（全日行情，含调整前后价格/成交量）；
3. 财务摘要（Financial Data, Summary only）；
4. 决算发表日历（Earnings Calendar）；
5. 交易日历（Trading Calendar）。

当前套餐比较页还明确：免费版限 **5 API calls/min**，不支持股票数据的 CSV 批量下载，但交易日历可以通过 API 或 CSV 获取。当前官方产品矩阵明确把交易日历列入 Free；官方 V2 Python 客户端 README 仍把 `get_mkt_calendar` 列在 Light 及以上，两者存在文档不一致，权限判断应以当前产品矩阵和真实 Free key 契约测试为准。[当前套餐比较页](https://jpx-jquants.com/en)；[契约与数据期间官方规格](https://jpx-jquants.com/ja/spec/data-spec)；[J-Quants 官方 Python 客户端](https://github.com/J-Quants/jquants-api-client-python)

> 设计建议：首版只承诺“约 2 年、滞后 12 周的日本股票日线研究”。不要在规格中声称免费版能提供 TOPIX、通用指数、信用/卖空、投资者类别、股息明细、完整 BS/PL/CF、分钟/Tick 或 CSV 批量下载。

## 免费版数据清单（V2）

| 数据类别 | V2 客户端方法 | 免费版 | 对系统的用途 | 关键边界 |
|---|---|---:|---|---|
| 上市标的主数据 | `get_eq_master` | 是 | 证券代码、名称、市场/行业分类等标的维度；构建股票池 | 套餐页称 Listed Issue Master；不能由此推断一定含完整退市历史或所有公司行动事件 |
| 股票日线 | `get_eq_bars_daily` | 是 | OHLC、成交量/成交额、复权回测 | 只有全日（Full Day）日线；不是前后场拆分，更不是实时、分钟或 Tick |
| 财务摘要 | `get_fin_summary` | 是 | 季度业绩、预测及基础面因子 | 仅 Summary；完整财务报表 BS/PL/CF 属 Premium |
| 决算发表日历 | `get_eq_earnings_cal` | 是 | 避开决算日、事件研究 | 这是预定发表日期数据，不应视为完整、不可修订的历史事实版本 |
| 交易日历 | `get_mkt_calendar` / `GET /v2/markets/calendar` | 是 | 发现账户实际可见日期范围、识别交易日 | 当前产品矩阵列入 Free；官方客户端 README 的 Light+ 分组与之冲突，应以真实 Free key 验证 |

当前官方产品矩阵是更直接的套餐权限依据：Free 包含上市标的、全日日线、财务摘要、两类决算日历和交易日历；不包含 TOPIX、其他指数及一般 CSV 批量下载。官方客户端 README 把 `get_mkt_calendar` 放在 “Light plan or higher”，属于需要通过 live Free 契约测试消解的矛盾，而不应据此删除 Free 的交易日历能力。[契约与数据期间官方规格](https://jpx-jquants.com/ja/spec/data-spec)；[当前套餐矩阵](https://jpx-jquants.com/en)；[官方客户端的 V2 支持 API 与套餐分组](https://github.com/J-Quants/jquants-api-client-python)

### 股票日线的内容

JPX 对 Stock Prices (OHLC) 的官方描述包括：开、高、低、收，成交量与成交额，调整前/调整后价格，以及调整因子；公司行动导致的调整价格会追溯调整。免费版只给全日行情。该数据覆盖 TSE 上市的股票、ETP 和 REIT，但免费账户实际只能看到套餐允许的滚动历史和延迟截点。[JPX 的 J-Quants API 数据示例](https://www.jpx.co.jp/english/markets/other-data-services/j-quants-api/)；[JPX 数据集说明](https://pro.jpx-jquants.com/datasets/9)

这意味着回测可以用官方调整价处理拆股等价格连续性问题，但**不能据此假设免费版提供独立、完整的公司行动事件表**。现金股息明细在当前套餐矩阵中属于 Premium，而不是 Free。

### 财务摘要的内容与限制

JPX 将 Financial Summary 定义为上市公司季度决算摘要，以及业绩和股息预测修订的主要数值数据；与之不同，逐项目的 BS/PL/CF 是 Financial Statements 数据。[JPX 财务摘要/报表数据集说明](https://pro.jpx-jquants.com/datasets/5)

因此免费版可用于构造部分营收、利润、每股指标、公司预测等基础面信号，但规格必须明确：

- 免费版没有完整 BS/PL/CF 明细；
- “2 年历史”会使跨周期、五年增长、长期质量因子等无法可靠计算；
- 回测必须以披露时间为可见时间，不能按财年期末提前使用数据；
- 预测与修订值可能多次披露，导入时要保存披露时间和版本，不能只覆盖成最后一条。

## 历史深度、延迟与更新语义

当前官方规格将 Free 的股票日线和交易日历窗口明确为：从约 12 周前的数据开始，最远可回溯至约 2 年 12 周前；底层股票日线总数据存储始于 2008-05-07。FAQ 同时明确免费版拿不到今日股价，因为数据延迟 12 周。[契约与数据期间官方规格](https://jpx-jquants.com/ja/spec/data-spec)；[当前套餐比较与 FAQ](https://jpx-jquants.com/en)

对 2026-08-15 的系统设计，可保守解释为：

- 最晚可见数据约截至 2026-05-23（“12 周”按 84 个自然日作近似；实际可见交易日以 API 返回为准）；
- 可用历史窗口约 2 年；
- 这不是实时或准实时纸面交易数据源；最适合封闭历史区间回测/市场重放。

官方规格给出了相对窗口，但没有规定滚动边界按自然日、交易日或订阅日如何舍入。因此实现中仍不应硬编码精确起止日：同步程序应探测端点实际返回的最早/最晚日期并记录数据覆盖元数据。JPX 过去的正式套餐公告也提醒，某些数据可能达不到表列历史期间。[JPX 2025-08-22 套餐公告](https://www.jpx.co.jp/corporate/news/news-releases/6020/20250822-01.html)

### 初次同步如何发现实际可用范围

`GET /v2/equities/bars/daily` 不能无参数调用。官方规格要求必须指定 `code` 或 `date`，并只定义四种查询：仅 `code` 返回该证券的全部可访问历史；`code + date` 返回该证券指定日；`code + from/to` 返回该证券指定期间；仅 `date` 返回指定日全部上市证券。`pagination_key` 存在时必须保持原查询条件继续分页。[股票日线 V2 官方规格](https://jpx-jquants.com/en/spec/eq-bars-daily)

首版全市场同步可采用以下范围发现流程，不自行计算“两年”边界：

1. 无参数调用 `GET /v2/markets/calendar`。该接口明确支持无参数返回账户可访问的全期间日历；
2. 从响应中取得最早/最晚 `Date`，并按 `HolDiv` 选出交易日；
3. 对每个可见交易日，以 `date` 调用 `/v2/equities/bars/daily`，获取该日全部证券并遍历所有 `pagination_key`；
4. 将日线实际响应的最早/最晚日期持久化为本次同步的观测覆盖范围。若日历与日线边界不一致，以日线实际返回为准并记录差异。

[交易日历 V2 官方规格](https://jpx-jquants.com/en/spec/mkt-cal)明确无参数组合返回 “All data”；股票日线和交易日历在 Free 下具有相同的公开相对窗口。[契约与数据期间官方规格](https://jpx-jquants.com/ja/spec/data-spec) 若只同步少量目标证券，也可对每个证券仅传 `code`，直接取得该证券当前权限下的全部历史，但全市场逐证券调用会放大请求量。

官方公开规格**没有说明**显式 `from/to` 部分或全部超出套餐权限时会被裁剪、拒绝，还是返回空结果；也没有定义未知代码、休市日、空区间和参数校验失败的完整错误响应结构。因此不得把自动裁剪或 `200 {"data":[]}` 写成系统保证。应使用真实 Free key 为缺少 selector、未知代码、休市日、完全越界和部分越界建立契约测试，并把观测行为隔离在适配器内。公开 V2 目录也没有账户套餐或 coverage metadata 端点；无参数交易日历是范围发现数据源，不是正式 entitlement metadata API。[J-Quants V2 API Reference](https://jpx-jquants.com/en/spec)

## 调用和交付限制

- **频率：**当前套餐页列出 Free 为 5 calls/min。应实现限流、指数退避和 HTTP 429 重试。[当前套餐比较页](https://jpx-jquants.com/en)
- **批量文件：**Free 不含股票等一般 CSV Download；交易日历是官方规格注明的 Free 例外。2026-01-19 的 JPX 公告说明一般 CSV 批量获取面向 Light 及以上。[契约与数据期间官方规格](https://jpx-jquants.com/ja/spec/data-spec)；[JPX V2/CSV/分钟与 Tick 发布公告](https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260119.html)
- **分页：**官方 FAQ 指出大响应会分页；客户端的范围方法会重复/并行发请求，长区间容易触发限流。[J-Quants 官网 FAQ](https://jpx-jquants.com/?lang=ja%2F)；[官方 Python 客户端](https://github.com/J-Quants/jquants-api-client-python)
- **认证：**V2 使用 dashboard 签发的 API key。2025-12-22 以后注册的用户只能使用 V2；V1 已于 2026-06-01 结束。[J-Quants V2 API Reference](https://jpx-jquants.com/en/spec)；[JPX 2026-01-19 公告](https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260119.html)；[官方客户端](https://github.com/J-Quants/jquants-api-client-python)
- **用途：**服务限定个人私用。不得把取得的原始数据以可查看形式再分发，也不得持续向第三方提供基于其数据的投资分析服务。[J-Quants 官网 FAQ](https://jpx-jquants.com/?lang=ja%2F)；[JPX 服务定位](https://www.jpx.co.jp/english/markets/paid-info-equities/historical/index.html)

## 免费版明确不包含的内容

依据当前套餐矩阵和 V2 官方客户端权限分组，Free 不包含：

- TOPIX 日线、其他指数日线；
- 交易者类型/投资部门别数据；
- 信用交易余额、日々公表信用余额/信用监管数据；
- 行业卖空比率与大额卖空持仓报告；
- 指数期权、期货与其他期权日线；
- 前场（日中午盘）独立行情；
- 现金股息明细；
- 完整财务报表 BS/PL/CF；
- 成交拆分数据；
- 分钟、5 分钟、15 分钟及 Tick；
- CSV bulk download；
- TDnet 文档附加包。

分钟/Tick 是 Light 以上才可购买的月费附加包，且按日交付、并非实时；TDnet 同样是 Light 以上的另付费附加包。[JPX API 数据示例](https://www.jpx.co.jp/english/markets/other-data-services/j-quants-api/)；[分钟/Tick 与 CSV 公告](https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260119.html)；[TDnet 附加包公告](https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260518-01.html)

## 近期变更与歧义

### 1. 当前 V2 产品矩阵确认 Free 包含交易日历，但官方客户端 README 仍显示 Light

JPX 在 2025-08-22 公告的旧套餐表中把“交易日历”列为 Free 可用；当前 V2 产品矩阵和“契约ごとの API／数据期间”规格同样明确 Free 可获取交易日历。只有官方 V2 Python 客户端 README 仍把 `get_mkt_calendar` 分在 Light 及以上。[2025-08-22 旧套餐表](https://www.jpx.co.jp/corporate/news/news-releases/6020/20250822-01.html)；[当前套餐页](https://jpx-jquants.com/en)；[契约与数据期间官方规格](https://jpx-jquants.com/ja/spec/data-spec)；[官方 V2 客户端](https://github.com/J-Quants/jquants-api-client-python)

本调查以当前 V2 产品规格为准：**交易日历可列入 Free 首版能力**，并用于发现实际可见范围。由于官方客户端 README 与产品矩阵矛盾，发布前仍应以真实 Free key 验证；若运行时返回权限错误，应明确失败并保留可替换日历适配器，而不是静默用“有行情的日期”冒充官方日历。

### 2. 当前官网与旧索引内容可能并存

JPX 2026 年公告确认 V2 已于 2025 年 12 月上线，当前正式入口 `jpx-jquants.com` 已提供 V2 API Reference 与套餐矩阵；但搜索索引仍可能返回旧首页、测试域名或过期资料。权限与字段判断应优先采用当前正式产品矩阵和 V2 API Reference，再以 JPX 公告及真实套餐 key 做交叉验证；官方客户端 README 也可能落后于产品矩阵。

### 3. “免费数据能否支持无偏回测”不能只从端点列表得出

公开材料没有承诺免费主数据具备完整的历史成分快照、退市证券全集或逐时点修订历史。日线含调整价也不等于提供完整公司行动事件流。因此在实际账户上抽样验证前，系统规格应把“避免幸存者偏差”“点时一致的财务版本”写成验收要求，而不是已由数据源保证的事实。

## 对当前个人系统的直接建议

1. 第一版市场固定为日本（TSE），频率固定为周度，底层只支持日线。
2. 数据适配器实现 V2 的核心 Free 端点，并把交易日历作为可替换接口；当前按 Free 可用实现，同时用真实 Free key 契约测试防范官方文档冲突。
3. 数据库保存 `source_disclosed_at`、`ingested_at`、数据有效日期和原始版本标识，避免财务披露的未来函数。
4. 回测界面展示每次运行的数据最早日、最晚日和 12 周延迟警告。
5. 免费版阶段把结果定位为流程验证和短期策略原型。周度策略两年通常只有约 100 个调仓观察点，统计把握度不足以支持强结论。
6. 若需要长期策略验证，最小升级通常是 Light（5 年和 TOPIX）；但在采购前应再次核对当前套餐，因为 J-Quants 在 2025-2026 已发生 V1→V2、权限分组和附加包变化。
7. 初次全市场同步先无参数读取交易日历，再逐 `date` 拉取全市场日线并完整分页；不要通过猜测超宽 `from/to` 来探测权限边界。
