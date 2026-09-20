# -*- coding: utf-8 -*-
"""A股每日板块复盘：行业Top5/Bottom5 + Tavily检索 + Gemini归因 + WxPusher推送。"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 直接执行 sector_review/main.py 时，将仓库根目录加入模块搜索路径。
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import requests
import pandas as pd

from data_provider.base import DataFetcherManager
from src.search_service import SearchService

TZ = ZoneInfo("Asia/Shanghai")
TOP_N = 5
GOLD_KEYWORDS = ("黄金", "贵金属", "金矿", "黄金概念")
HISTORY_DIR = Path("data/sector_review_history")
REPORT_DIR = Path("reports/sector_review")


def env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def pct(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def latest_trade_date(now: datetime) -> str:
    """返回不晚于当前日期的最近一个上交所交易日。"""
    try:
        import exchange_calendars as xcals
        cal = xcals.get_calendar("XSHG")
        day = pd.Timestamp(now.date())
        if cal.is_session(day):
            return day.strftime("%Y-%m-%d")
        return cal.date_to_session(day, direction="previous").strftime("%Y-%m-%d")
    except Exception as exc:
        print(f"[warn] 交易日历读取失败，回退自然日: {exc}")
        return now.strftime("%Y-%m-%d")


def fetch_market():
    """获取概念/题材板块涨跌榜；失败时回退行业板块。"""
    mgr = DataFetcherManager()
    top, bottom = mgr.get_concept_rankings(TOP_N)
    ranking_type = "概念"
    if not top and not bottom:
        print("[warn] 概念板块获取失败，回退行业板块")
        top, bottom = mgr.get_sector_rankings(TOP_N)
        ranking_type = "行业"
    if not top and not bottom:
        raise RuntimeError("未获取到板块涨跌榜")
    stats = mgr.get_market_stats(purpose="sector_review:cn") or {}
    indices = mgr.get_main_indices(region="cn") or []
    return top or [], bottom or [], stats, indices, ranking_type, mgr

def is_gold_sector(name: str) -> bool:
    text = str(name or "")
    return any(k in text for k in GOLD_KEYWORDS)


def find_gold_rows(mgr: DataFetcherManager, top: list[dict], bottom: list[dict]) -> list[dict]:
    """尽量定位黄金/贵金属板块；即使不在Top5也单独跟踪。"""
    found = {}
    for row in (top or []) + (bottom or []):
        name = str(row.get("name") or "")
        if is_gold_sector(name):
            found[name] = dict(row)
    try:
        ctop, cbottom = mgr.get_concept_rankings(200)
        for row in (ctop or []) + (cbottom or []):
            name = str(row.get("name") or "")
            if is_gold_sector(name):
                found[name] = dict(row)
    except Exception as exc:
        print(f"[warn] 黄金概念全榜定位失败: {exc}")
    return sorted(found.values(), key=lambda x: pct(x.get("change_pct", x.get("涨跌幅", 0))), reverse=True)


def gold_search_news(service: SearchService | None, trade_date: str):
    if not service or not service.is_available:
        return []
    queries = [
        f"{trade_date} 黄金 金价 COMEX 现货黄金 美元 美债 实际利率 美联储 原因",
        f"{trade_date} A股 黄金 贵金属 金矿 板块 上涨 下跌 原因 紫金黄金 山东黄金 中金黄金",
        f"{trade_date} 央行 黄金储备 地缘政治 避险 黄金 ETF",
    ]
    out, seen = [], set()
    for query in queries:
        try:
            response = service.search_topic_news(query, max_results=5, focus_keywords=["黄金", "贵金属"])
        except Exception as exc:
            print(f"[warn] 黄金专题搜索失败: {exc}")
            continue
        for item in getattr(response, "results", []) or []:
            url = getattr(item, "url", "") or ""
            title = getattr(item, "title", "") or ""
            key = url or title
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({
                "title": title,
                "snippet": getattr(item, "snippet", ""),
                "url": url,
                "source": getattr(item, "source", ""),
                "published_date": getattr(item, "published_date", None),
            })
    return out[:10]


def build_search() -> SearchService | None:
    keys = [x.strip() for x in env("TAVILY_API_KEYS").split(",") if x.strip()]
    if not keys:
        return None
    return SearchService(tavily_keys=keys, news_max_age_days=3, news_strategy_profile="ultra_short")


def search_news(service: SearchService | None, sector: str, trade_date: str):
    if not service or not service.is_available:
        return []
    queries = [
        f"{trade_date} A股 {sector} 板块 涨停 领涨 上涨 下跌 原因",
        f"{trade_date} {sector} 政策 产业 数据 涨价 订单 公告 财联社 证券时报",
    ]
    out, seen = [], set()
    for query in queries:
        try:
            response = service.search_topic_news(
                query,
                max_results=5,
                focus_keywords=[sector, "A股"],
            )
        except Exception as exc:
            print(f"[warn] {sector} 搜索失败: {exc}")
            continue
        for item in getattr(response, "results", []) or []:
            url = getattr(item, "url", "") or ""
            title = getattr(item, "title", "") or ""
            key = url or title
            if not key or key in seen:
                continue
            seen.add(key)
            out.append({
                "title": title,
                "snippet": getattr(item, "snippet", ""),
                "url": url,
                "source": getattr(item, "source", ""),
                "published_date": getattr(item, "published_date", None),
            })
    return out[:8]

def gemini_reason(sector: str, change_pct: float, news: list[dict]) -> dict:
    key = env("GEMINI_API_KEY")
    if not key:
        keys = [x.strip() for x in env("GEMINI_API_KEYS").split(",") if x.strip()]
        key = keys[0] if keys else ""
    if not key:
        return {"reason": "暂无明确直接催化，主要表现为资金/市场风格因素", "confidence": "中", "sources": []}

    evidence = "\n".join(
        f"- {n.get('source') or '未知来源'} | {n.get('published_date') or '日期未知'} | {n.get('title')} | {n.get('snippet')}"
        for n in news[:6]
    ) or "无可靠检索结果"

    prompt = f"""你是A股盘后复盘助手。只允许基于下面给出的检索材料归因，禁止补充未提供的事实。
板块：{sector}
当日涨跌幅：{change_pct:+.2f}%
材料：
{evidence}

要求：
1. 输出1句话核心原因，最多45字；
2. confidence只能是“高”或“中”；
3. 高：有明确政策/公告/产业数据/权威媒体事实且与走势直接对应；
4. 中：有可靠媒体线索但因果链不够直接；
5. 如果材料不足，reason固定输出“暂无明确直接催化，主要表现为资金/市场风格因素”；
6. 不得把市场传闻写成已确认事实；
7. sources最多返回2条真正支持该原因的材料URL；没有直接证据则返回空数组；
8. 只输出JSON：{{"reason":"...","confidence":"高或中","sources":["url1","url2"]}}。"""

    models = [env("GEMINI_MODEL") or "gemini-2.5-flash", env("GEMINI_MODEL_FALLBACK") or "gemini-2.5-flash"]
    for model in dict.fromkeys(models):
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
            }
            r = requests.post(url, json=payload, timeout=45)
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
            reason = str(data.get("reason") or "").strip()
            conf = str(data.get("confidence") or "").strip()
            if reason and conf in {"高", "中"}:
                return {"reason": reason, "confidence": conf, "sources": [str(x) for x in (data.get("sources") or [])[:2] if str(x).startswith("http")]}
        except Exception as exc:
            print(f"[warn] Gemini {model} 归因失败: {exc}")
    return {"reason": "暂无明确直接催化，主要表现为资金/市场风格因素", "confidence": "中", "sources": []}


def market_summary(top: list[dict], bottom: list[dict]) -> str:
    strong = "、".join(str(x.get("name") or "") for x in top[:3] if x.get("name"))
    weak = "、".join(str(x.get("name") or "") for x in bottom[:3] if x.get("name"))
    parts = []
    if strong:
        parts.append(f"强势集中在{strong}")
    if weak:
        parts.append(f"相对弱势为{weak}")
    return "；".join(parts) + "。" if parts else "板块分化信息暂缺。"


def load_previous():
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(HISTORY_DIR.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text(encoding="utf-8"))
    except Exception:
        return None


def continuity(top: list[dict], previous: dict | None) -> str:
    if not previous:
        return "暂无历史样本，今日起开始记录主线连续性。"
    prev_top = [x.get("name") for x in previous.get("top", [])]
    cur_top = [x.get("name") for x in top]
    stayed = [x for x in cur_top if x in prev_top]
    new = [x for x in cur_top if x not in prev_top]
    parts = []
    if stayed:
        parts.append("延续：" + "、".join(stayed[:3]))
    if new:
        parts.append("新进Top5：" + "、".join(new[:3]))
    return "；".join(parts) or "Top5轮动明显。"


def render(trade_date: str, top, bottom, stats, indices, reasons, continuity_text: str, ranking_type: str, gold_rows: list[dict], gold_reason: dict) -> str:
    idx_map = []
    for x in indices[:3]:
        try:
            idx_map.append(f"{x.get('name','指数')} {pct(x.get('change_pct')):+.2f}%")
        except Exception:
            pass
    header = "｜".join(idx_map)
    up_count = stats.get("up_count")
    down_count = stats.get("down_count")
    breadth = f"上涨 {up_count}｜下跌 {down_count}" if up_count is not None and down_count is not None and (up_count or down_count) else ""
    amount = stats.get("total_amount", 0)
    amount_text = f"｜成交 {amount:.0f}亿" if isinstance(amount, (int, float)) and amount else ""

    lines = [
        f"📊 A股板块复盘｜{trade_date}",
        f"口径：{ranking_type}板块",
        header,
        (breadth + amount_text) if (breadth or amount_text) else "市场宽度数据暂缺",
        "",
        "🔥 强势 Top5",
    ]
    for i, s in enumerate(top, 1):
        name = s.get("name", "")
        p = pct(s.get("change_pct", s.get("涨跌幅", 0)))
        rr = reasons.get(name, {})
        src = rr.get("sources") or []
        evidence = "📎" if src else ""
        lines.append(f"{i}. {name} {p:+.2f}%｜{rr.get('reason','暂无明确直接催化，主要表现为资金/市场风格因素')}【{rr.get('confidence','中')}】{evidence}")
    lines += ["", "📉 弱势 Top5"]
    for i, s in enumerate(bottom, 1):
        name = s.get("name", "")
        p = pct(s.get("change_pct", s.get("涨跌幅", 0)))
        rr = reasons.get(name, {})
        label = "相对弱势" if p > -1 else "弱势"
        src = rr.get("sources") or []
        evidence = "📎" if src else ""
        lines.append(f"{i}. {name} {p:+.2f}%｜{label}；{rr.get('reason','暂无明确直接催化，主要表现为资金/市场风格因素')}【{rr.get('confidence','中')}】{evidence}")
    lines += ["", "🥇 黄金重点跟踪"]
    if gold_rows:
        for row in gold_rows[:3]:
            gname = str(row.get("name") or "黄金")
            gp = pct(row.get("change_pct", row.get("涨跌幅", 0)))
            lines.append(f"{gname} {gp:+.2f}%｜{gold_reason.get('reason','暂无明确直接催化，主要表现为资金/市场风格因素')}【{gold_reason.get('confidence','中')}】" + ("📎" if gold_reason.get("sources") else ""))
    else:
        lines.append("黄金/贵金属板块当前未能从榜单定位，仍保留宏观催化跟踪。")
        lines.append(f"驱动观察｜{gold_reason.get('reason','暂无明确直接催化，主要表现为资金/市场风格因素')}【{gold_reason.get('confidence','中')}】" + ("📎" if gold_reason.get("sources") else ""))
    lines += ["", "🎯 今日结构", market_summary(top, bottom), "", "🔁 主线连续性", continuity_text, "", "注：📎表示归因有检索证据；无证据不强行解释。"]
    return "\n".join([x for x in lines if x is not None])


def push_wxpusher(content: str):
    spt = env("WXPUSHER_SPT")
    if not spt:
        raise RuntimeError("WXPUSHER_SPT 未配置")
    payload = {
        "content": content,
        "summary": "A股每日板块复盘",
        "contentType": 1,
        "spt": spt,
    }
    r = requests.post("https://wxpusher.zjiecode.com/api/send/message/simple-push", json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"WxPusher发送失败: {data}")
    print("✅ WxPusher 推送成功")


def main():
    now = datetime.now(TZ)
    trade_date = latest_trade_date(now)
    today = now.strftime("%Y-%m-%d")
    if env("GITHUB_EVENT_NAME") == "schedule" and trade_date != today:
        print(f"⏭️ {today} 非A股交易日，最近交易日为 {trade_date}，定时任务不重复推送。")
        return
    top, bottom, stats, indices, ranking_type, mgr = fetch_market()
    search = build_search()

    reasons = {}
    for s in top + bottom:
        name = str(s.get("name") or "")
        change = pct(s.get("change_pct", s.get("涨跌幅", 0)))
        news = search_news(search, name, trade_date)
        reasons[name] = gemini_reason(name, change, news)

    gold_rows = find_gold_rows(mgr, top, bottom)
    gold_news = gold_search_news(search, trade_date)
    gold_change = pct(gold_rows[0].get("change_pct", gold_rows[0].get("涨跌幅", 0))) if gold_rows else 0.0
    gold_reason = gemini_reason("黄金/贵金属", gold_change, gold_news)

    previous = load_previous()
    cont = continuity(top, previous)
    report = render(trade_date, top, bottom, stats, indices, reasons, cont, ranking_type, gold_rows, gold_reason)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{trade_date}.txt").write_text(report, encoding="utf-8")

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"date": trade_date, "ranking_type": ranking_type, "top": top, "bottom": bottom, "reasons": reasons, "gold": {"rows": gold_rows, "reason": gold_reason}}
    (HISTORY_DIR / f"{trade_date}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(report)
    push_wxpusher(report)


if __name__ == "__main__":
    main()
