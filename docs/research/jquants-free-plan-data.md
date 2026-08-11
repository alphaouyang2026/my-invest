# J-Quants API 免费版可用数据调查

调查日期：2026-08-11  
范围：面向个人的 J-Quants API（不是 J-Quants Pro 或 DataCube）；以 V2 API 为实现基线。  
来源限制：仅采用 JPX/JPX Market Innovation & Research（JPXI）官网及 J-Quants 官方 GitHub 组织。

## 结论摘要

免费版适合做一个**日本股票、日线、周度调仓的原型**，但不适合严肃验证长期策略：它只提供滚动约 2 年历史，并且最新数据延迟 12 周。当前 V2 免费版的核心数据是：

1. 上市标的主数据（Listed Issue Master）；
2. 股票日线 OHLC（全日行情，含调整前后价格/成交量）；
3. 财务摘要（Financial Data, Summary only）；
4. 决算发表日历（Earnings Calendar）。

当前套餐比较页还明确：免费版限 **5 API calls/min**，不支持 CSV 批量下载。官方 V2 Python 客户端把免费端点列为 `get_eq_master`、`get_eq_bars_daily`、`get_fin_summary`、`get_eq_earnings_cal`；这与当前套餐页基本一致。[当前套餐比较页](https://elb.test-dlv.jpx-jquants.com/)；[J-Quants 官方 Python 客户端](https://github.com/J-Quants/jquants-api-client-python)

> 设计建议：首版只承诺“约 2 年、滞后 12 周的日本股票日线研究”。不要在规格中声称免费版能提供 TOPIX、通用指数、信用/卖空、投资者类别、股息明细、完整 BS/PL/CF、分钟/Tick 或 CSV 批量下载。

## 免费版数据清单（V2）

| 数据类别 | V2 客户端方法 | 免费版 | 对系统的用途 | 关键边界 |
|---|---|---:|---|---|
| 上市标的主数据 | `get_eq_master` | 是 | 证券代码、名称、市场/行业分类等标的维度；构建股票池 | 套餐页称 Listed Issue Master；不能由此推断一定含完整退市历史或所有公司行动事件 |
| 股票日线 | `get_eq_bars_daily` | 是 | OHLC、成交量/成交额、复权回测 | 只有全日（Full Day）日线；不是前后场拆分，更不是实时、分钟或 Tick |
| 财务摘要 | `get_fin_summary` | 是 | 季度业绩、预测及基础面因子 | 仅 Summary；完整财务报表 BS/PL/CF 属 Premium |
| 决算发表日历 | `get_eq_earnings_cal` | 是 | 避开决算日、事件研究 | 这是预定发表日期数据，不应视为完整、不可修订的历史事实版本 |

官方客户端 README 是目前最清楚的 V2 权限映射：它把上述四个 wrapper 列在 “Free plan or higher”，而把指数、TOPIX、营业日历和 bulk 下载列在 “Light plan or higher”。[官方客户端的 V2 支持 API 与套餐分组](https://github.com/J-Quants/jquants-api-client-python)

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

当前官方套餐页写明 Free 为 **2 years History [12 weeks delayed]**，并在 FAQ 明确免费版拿不到今日股价，因为数据延迟 12 周。[当前套餐比较与 FAQ](https://elb.test-dlv.jpx-jquants.com/)

对 2026-08-11 的系统设计，可保守解释为：

- 最晚可见数据约截至 2026-05-19（“12 周”按 84 个自然日作近似；实际可见交易日以 API 返回为准）；
- 可用历史窗口约 2 年；
- 这不是实时或准实时纸面交易数据源；最适合封闭历史区间回测/市场重放。

官方页面没有在公开套餐文字中精确定义“2 年”的窗口是以当前日还是延迟截止日为终点，也没有说明边界按自然日、交易日或订阅日计算。因此实现中不应硬编码精确起止日：同步程序应探测每个端点实际返回的最早/最晚日期并记录数据覆盖元数据。JPX 过去的正式套餐公告也提醒，某些数据可能达不到表列历史期间。[JPX 2025-08-22 套餐公告](https://www.jpx.co.jp/corporate/news/news-releases/6020/20250822-01.html)

## 调用和交付限制

- **频率：**当前套餐页列出 Free 为 5 calls/min。应实现限流、指数退避和 HTTP 429 重试。[当前套餐比较页](https://elb.test-dlv.jpx-jquants.com/)
- **批量文件：**Free 不含 CSV Download；2026-01-19 的 JPX 公告明确 CSV 批量获取仅 Light 及以上。[JPX V2/CSV/分钟与 Tick 发布公告](https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260119.html)
- **分页：**官方 FAQ 指出大响应会分页；客户端的范围方法会重复/并行发请求，长区间容易触发限流。[J-Quants 官网 FAQ](https://jpx-jquants.com/?lang=ja%2F)；[官方 Python 客户端](https://github.com/J-Quants/jquants-api-client-python)
- **认证：**V2 使用 dashboard 签发的 API key。2025-12-22 以后注册的用户只能使用 V2；V1 仍在过渡期，但停止日期待公告。[JPX 2026-01-19 公告](https://www.jpx.co.jp/english/corporate/news/news-releases/6020/20260119.html)；[官方客户端](https://github.com/J-Quants/jquants-api-client-python)
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

### 1. 交易日历曾在 Free，当前 V2 显示为 Light

JPX 在 2025-08-22 公告的旧套餐表中，把“交易日历”列为 Free 可用；但 2025 年 12 月 V2 上线后的当前套餐页和官方 V2 Python 客户端都把 Trading Calendar / `get_mkt_calendar` 放在 Light 及以上。[2025-08-22 旧套餐表](https://www.jpx.co.jp/corporate/news/news-releases/6020/20250822-01.html)；[当前套餐页](https://elb.test-dlv.jpx-jquants.com/)；[官方 V2 客户端](https://github.com/J-Quants/jquants-api-client-python)

本调查以当前 V2 权限为准：**不要把交易日历列入免费版依赖**。免费实现可从实际日线日期推导“有行情的日期”，但它不等价于官方交易日历（尤其不能可靠区分未来交易日、临时休市或无成交标的）。

### 2. 当前套餐页的公开入口/索引状态存在不一致

JPX 2026 年公告确认 V2 已于 2025 年 12 月上线；搜索可见的新版英文套餐矩阵位于 JPXI 控制的 `elb.test-dlv.jpx-jquants.com`，而 `jpx-jquants.com` 的公开索引仍可能展示旧 FAQ（例如声称尚无分钟/Tick）。因此端点权限以新版套餐矩阵、2026 JPX 公告和最新官方客户端三者交叉验证，旧首页 FAQ 不用于判定分钟/Tick 的当前可用性。

### 3. “免费数据能否支持无偏回测”不能只从端点列表得出

公开材料没有承诺免费主数据具备完整的历史成分快照、退市证券全集或逐时点修订历史。日线含调整价也不等于提供完整公司行动事件流。因此在实际账户上抽样验证前，系统规格应把“避免幸存者偏差”“点时一致的财务版本”写成验收要求，而不是已由数据源保证的事实。

## 对当前个人系统的直接建议

1. 第一版市场固定为日本（TSE），频率固定为周度，底层只支持日线。
2. 数据适配器先实现 V2 的四个 Free 端点；交易日历做可替换接口，不把 Light 权限当免费能力。
3. 数据库保存 `source_disclosed_at`、`ingested_at`、数据有效日期和原始版本标识，避免财务披露的未来函数。
4. 回测界面展示每次运行的数据最早日、最晚日和 12 周延迟警告。
5. 免费版阶段把结果定位为流程验证和短期策略原型。周度策略两年通常只有约 100 个调仓观察点，统计把握度不足以支持强结论。
6. 若需要长期策略验证，最小升级通常是 Light（5 年和 TOPIX）；但在采购前应再次核对当前套餐，因为 J-Quants 在 2025-2026 已发生 V1→V2、权限分组和附加包变化。

