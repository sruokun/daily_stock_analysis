# A股板块复盘

独立于原项目主流程的轻量复盘模块。

功能：
- 复用原项目 DataFetcherManager 获取A股行业板块涨跌Top5/Bottom5
- 使用 Tavily 搜索当日板块催化与行业新闻
- 使用 Gemini 仅基于检索材料生成1句归因，并标记高/中置信度
- 证据不足时固定输出“暂无明确直接催化，主要表现为资金/市场风格因素”
- 通过 WxPusher SPT 推送到手机
- 保存每日历史，用于判断“延续 / 新进Top5”等连续性

GitHub Secrets：
- GEMINI_API_KEY（或 GEMINI_API_KEYS）
- TAVILY_API_KEYS
- WXPUSHER_SPT

Workflow：`.github/workflows/sector-review.yml`
默认工作日北京时间 15:20 运行，也支持手动触发。
