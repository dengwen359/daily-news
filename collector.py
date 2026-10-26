#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日新闻收集器
----------------
纯标准库实现，零依赖、零费用、可长期运行。
抓取 RSS -> 按关键词过滤 -> 去重 -> 按主题分组 -> 生成 Markdown 日报 ->（可选）发邮件

用法：
    python collector.py              # 正常抓取
    python collector.py --dry-run    # 只看结果不写文件
"""

import os
import re
import sys
import json
import html
import ssl
import time
import smtplib
import hashlib
import argparse
import datetime as dt
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from email.header import Header
from email.utils import parsedate_to_datetime, formatdate, make_msgid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
STATE_PATH = os.path.join(BASE_DIR, ".state", "seen.json")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


# ---------------------------------------------------------------- 极简 YAML 解析
# 只支持本配置用到的子集：map / list / 字符串 / 数字 / 布尔
# 这样就不需要装 PyYAML，保证"装完就能一直用"
def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f]

    root = {}
    # stack of (indent, container)
    stack = [(-1, root)]
    pending_key = [None]  # 需要挂载子结构的父键

    def parse_scalar(s):
        s = s.strip()
        if not s:
            return ""
        # 去掉行尾注释（引号内的 # 不算）
        if s[0] not in ('"', "'") and " #" in s:
            s = s.split(" #", 1)[0].strip()
        if not s:
            return ""
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            return s[1:-1]
        low = s.lower()
        if low in ("true", "yes"):
            return True
        if low in ("false", "no"):
            return False
        if low in ("null", "~"):
            return None
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
        return s

    i = 0
    while i < len(lines):
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.strip().startswith("#"):
            continue

        indent = len(raw) - len(raw.lstrip(" "))
        content = raw.strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        cur = stack[-1][1]

        if content.startswith("- "):
            item = content[2:].strip()
            if isinstance(cur, list):
                # 只有 "key: value" 形式（冒号后有空格或行尾）才当字典，URL 的 :// 不算
                m = re.match(r"^([\w\u4e00-\u9fff._-]+):(\s+.*|)$", item)
                if m:
                    k = m.group(1)
                    v = (m.group(2) or "").strip()
                    d = {}
                    d[k] = parse_scalar(v) if v else None
                    cur.append(d)
                    stack.append((indent, d))
                else:
                    cur.append(parse_scalar(item))
            continue

        if ":" in content:
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                # 决定是 dict 还是 list：看下一行
                nxt = None
                for j in range(i, len(lines)):
                    if lines[j].strip() and not lines[j].strip().startswith("#"):
                        nxt = lines[j]
                        break
                if nxt is not None and nxt.strip().startswith("- "):
                    new = []
                else:
                    new = {}
                if isinstance(cur, dict):
                    cur[key] = new
                stack.append((indent, new))
            else:
                if isinstance(cur, dict):
                    cur[key] = parse_scalar(val)
    return root


# ---------------------------------------------------------------- 抓取
def fetch(url, timeout=20, retries=2):
    """带重试的抓取，遇到 429/网络抖动会自动退避重试"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
            for enc in ("utf-8", "gb18030", "latin-1"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="ignore")
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2.5 * (attempt + 1))  # 退避
    raise last_err


def strip_html(s):
    if not s:
        return ""
    # 先把被转义的标签还原成真标签，再统一剥离
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S)
    for _ in range(2):  # 处理双重转义
        s = html.unescape(s)
    s = re.sub(r"<script.*?</script>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]*>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def tag_text(block, tag):
    # 贪婪匹配到最后一个闭合标签，避免内部嵌套标签导致截断
    m = re.search(rf"<{tag}[^>]*>(.*)</{tag}>", block, flags=re.S | re.I)
    return m.group(1) if m else ""


def parse_feed(xml_text):
    """返回 [(title, link, summary, pubdate)]"""
    items = []
    # RSS 2.0
    for m in re.finditer(r"<item[ >].*?</item>", xml_text, flags=re.S | re.I):
        blk = m.group(0)
        title = strip_html(tag_text(blk, "title"))
        link = strip_html(tag_text(blk, "link"))
        if not link:
            lm = re.search(r'<link[^>]*href="([^"]+)"', blk, re.I)
            link = lm.group(1) if lm else ""
        summary = strip_html(tag_text(blk, "description")) or strip_html(tag_text(blk, "content:encoded"))
        pub = tag_text(blk, "pubDate") or tag_text(blk, "dc:date")
        items.append((title, link, summary, pub))
    if items:
        return items
    # Atom
    for m in re.finditer(r"<entry[ >].*?</entry>", xml_text, flags=re.S | re.I):
        blk = m.group(0)
        title = strip_html(tag_text(blk, "title"))
        lm = re.search(r'<link[^>]*href="([^"]+)"', blk, re.I)
        link = lm.group(1) if lm else ""
        summary = strip_html(tag_text(blk, "summary")) or strip_html(tag_text(blk, "content"))
        pub = tag_text(blk, "updated") or tag_text(blk, "published")
        items.append((title, link, summary, pub))
    return items


def parse_time(s):
    s = (s or "").strip()
    if not s:
        return None
    # 有些源用 CDATA 包裹时间，先剥掉，否则时区会被误判成本地时间
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s, flags=re.S).strip()
    s = re.sub(r"<[^>]*>", "", s).strip()
    if not s:
        return None

    local_tz = dt.datetime.now().astimezone().tzinfo

    def to_local(d):
        """统一转成带时区的本地时间，保证可比较"""
        if d.tzinfo is None:
            d = d.replace(tzinfo=local_tz)   # 无时区信息 -> 当作本地时间
        return d.astimezone(local_tz)

    try:
        return to_local(parsedate_to_datetime(s))
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y年%m月%d日 %H:%M", "%Y年%m月%d日"):
        try:
            return to_local(dt.datetime.strptime(s.replace("GMT", "+0000").strip(), fmt))
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 过滤打分
def _kw_pattern(kw):
    """
    关键词匹配规则：
    - 纯英文/数字（如 AI、IPO、5G）要求词边界，避免 "AI" 命中 "Steam"、"每日AI" 里的字母片段
    - 中文词直接子串匹配
    """
    if re.fullmatch(r"[A-Za-z0-9]+", kw):
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])", re.I)
    return re.compile(re.escape(kw), re.I)


_KW_CACHE = {}


def kw_hit(kw, text):
    if not kw:
        return False
    p = _KW_CACHE.get(kw)
    if p is None:
        p = _kw_pattern(kw)
        _KW_CACHE[kw] = p
    return p.search(text) is not None


def match_topics(text, topics, weak=None, title=""):
    """
    命中哪些主题 + 命中了哪些词
    weak: 泛词集合，这些词只在标题里出现才算命中
    """
    hits = []
    words = []
    weak = weak or set()
    for name, cfg in topics.items():
        kws = cfg.get("keywords", []) or []
        for kw in kws:
            if kw in weak:
                if not kw_hit(kw, title):
                    continue
            elif not kw_hit(kw, text):
                continue
            if name not in hits:
                hits.append(name)
            if kw not in words:
                words.append(kw)
    return hits, words


def title_hits(title, topics):
    """标题命中数，用于加权（标题命中比正文命中更重要）"""
    n = 0
    for cfg in topics.values():
        for kw in (cfg.get("keywords", []) or []):
            if kw_hit(kw, title):
                n += 1
    return n


def item_id(link, title):
    return hashlib.md5((link or title).encode("utf-8", "ignore")).hexdigest()[:16]


def norm_title(t):
    """标题归一化：去掉标点空格，便于比较相似度"""
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", (t or "").lower())
    return t


def longest_common(a, b):
    """最长公共子串长度：同一事件的不同稿件往往共享一长串原文"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                v = prev[j - 1] + 1
                cur[j] = v
                if v > best:
                    best = v
        prev = cur
    return best


def similar(a, b):
    """判断两个归一化标题是否表达同一件事"""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    # 短标题被长标题完整包含，且长度接近（防止"AI"这种短词误判）
    if len(short) >= 8 and short in long_ and len(short) / len(long_) >= 0.72:
        return True
    # 快速预筛：字符重合太少，不可能是同一件事
    if len(set(a) & set(b)) < 4:
        return False
    # 长度差太大，跳过昂贵计算
    if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.75:
        return False
    # 连续相同片段够长，说明是同一事件的不同写法
    lcs = longest_common(a, b)
    if lcs >= 6 and lcs / min(len(a), len(b)) >= 0.3:
        return True
    # 编辑距离近似（只比较长度接近的）
    if abs(len(a) - len(b)) <= max(4, int(len(long_) * 0.2)):
        if ratio_close(a, b) >= 0.85:
            return True
    return False


def ratio_close(a, b):
    """快速相似度：基于二元组重合率"""
    if len(a) < 2 or len(b) < 2:
        return 1.0 if a == b else 0.0
    A = {a[i:i + 2] for i in range(len(a) - 1)}
    B = {b[i:i + 2] for i in range(len(b) - 1)}
    inter = len(A & B)
    return 2.0 * inter / (len(A) + len(B))


# ---------------------------------------------------------------- 状态
def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state, max_age_days=7):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    cutoff = (dt.datetime.now() - dt.timedelta(days=max_age_days)).timestamp()
    state = {k: v for k, v in state.items() if isinstance(v, (int, float)) and v >= cutoff}
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ---------------------------------------------------------------- 生成报告
def build_report(items, cfg, run_time, feed_stats):
    hw = cfg.get("hours_window", 30)
    topics = cfg.get("topics", {}) or {}

    # 分级抽出：2=必看（命中必收词） 1=要闻（权威源） 0=普通
    lv2 = [it for it in items if it.get("level", 0) == 2]
    lv1 = [it for it in items if it.get("level", 0) == 1]
    key_items = lv2 + lv1
    key_ids = {it["id"] for it in key_items}
    rest = [it for it in items if it["id"] not in key_ids]

    grouped = {name: [] for name in topics}
    grouped["其他"] = []
    for it in rest:
        placed = False
        for t in it["topics"]:
            if t in grouped:
                grouped[t].append(it)
                placed = True
        if not placed:
            grouped["其他"].append(it)

    total = len(items)
    sources_ok = sum(1 for v in feed_stats.values() if v["ok"])
    sources_fail = len(feed_stats) - sources_ok

    def render_item(r, mark_key=False):
        L = []
        t = r["dt"].strftime("%m-%d %H:%M") if r["dt"] else ""
        L.append(f"- [{r['title']}]({r['link']})")
        meta = []
        if t:
            meta.append(t)
        meta.append(r["source"])
        if r.get("also"):
            meta.append("多家报道")
        if mark_key:
            if r.get("why"):
                meta.append(f"**{r['why']}**")
            elif r.get("tier") == "must":
                meta.append("**权威源**")
        elif r["words"]:
            meta.append(f"命中 {'、'.join(r['words'][:4])}")
        L.append(f"  <sub>{' · '.join(meta)}</sub>")
        if r["summary"]:
            s = r["summary"][:170]
            L.append(f"  {s}{'…' if len(r['summary']) > 170 else ''}")
        L.append("")
        return L

    L = []
    L.append(f"# 每日新闻简报 · {run_time.strftime('%Y-%m-%d')}")
    L.append("")
    L.append(f"> 生成时间：{run_time.strftime('%Y-%m-%d %H:%M')}　|　"
             f"必看 **{len(lv2)}** 条 · 要闻 **{len(lv1)}** 条 · 全部 **{total}** 条　|　"
             f"时间窗：最近 {hw} 小时")
    L.append(f"> 信息源：{sources_ok} 个正常" + (f"，{sources_fail} 个暂不可用（已自动跳过）" if sources_fail else ""))
    L.append("")

    # ---- 必看（命中央行、美联储、CPI、全会等硬新闻关键词） ----
    if lv2:
        lv2.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))
        L.append(f"## ★★ 必看（{len(lv2)} 条）")
        L.append("")
        L.append("> 命中货币政策、监管政策、宏观数据、重大政治事件等必收关键词。**这部分保证不遗漏。**")
        L.append("")
        for r in lv2:
            L.extend(render_item(r, mark_key=True))
        L.append("")

    # ---- 要闻（权威源当天的其他新闻） ----
    if lv1:
        lv1.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))
        L.append(f"## ★ 要闻（{len(lv1)} 条）")
        L.append("")
        L.append("> 来自中国新闻网、国家统计局、美联储等权威源的原发内容。")
        L.append("")
        for r in lv1:
            L.extend(render_item(r, mark_key=True))
        L.append("")

    # 概览
    L.append("## 今日概览")
    L.append("")
    if lv2:
        L.append(f"- **★★ 必看**：{len(lv2)} 条（见上）")
    if lv1:
        L.append(f"- **★ 要闻**：{len(lv1)} 条（见上）")
    for name in list(topics.keys()) + ["其他"]:
        if grouped[name]:
            L.append(f"- **{name}**：{len(grouped[name])} 条")
    L.append("")

    # 各主题明细
    for name in list(topics.keys()) + ["其他"]:
        rows = grouped[name]
        if not rows:
            continue
        rows.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))
        L.append(f"## {name}（{len(rows)} 条）")
        L.append("")
        for r in rows:
            L.extend(render_item(r))

    # 源状态
    L.append("---")
    L.append("")
    L.append("<details><summary>信息源状态</summary>")
    L.append("")
    for name, st in feed_stats.items():
        if st["ok"]:
            L.append(f"- ✅ {name}：{st['count']} 条")
        else:
            L.append(f"- ⚠️ {name}：{st['error']}")
    L.append("")
    L.append("</details>")
    L.append("")
    return "\n".join(L)


def build_mail_body(items, cfg, run_time):
    """精简版邮件正文。

    完整日报有上百条、上百个链接，直接塞进邮件会被反垃圾机制拦截
    （实测 139 邮箱返回 450 Mail rejected）。所以邮件只负责"提醒 + 重点"：
    必看逐条给标题和原文链接，要闻只列标题，完整内容引导去网页看。
    """
    lv2 = [it for it in items if it.get("level", 0) == 2]
    lv1 = [it for it in items if it.get("level", 0) == 1]
    lv0 = [it for it in items if it.get("level", 0) == 0]
    hour = run_time.hour
    edition = "早报" if hour < 15 else "晚报"
    site = cfg.get("site_url", "")

    L = []
    L.append(f"新闻{edition} · {run_time.strftime('%Y年%m月%d日')}")
    L.append("=" * 30)
    L.append(f"共 {len(items)} 条：必看 {len(lv2)} · 要闻 {len(lv1)} · 兴趣 {len(lv0)}")
    L.append(f"生成于 {run_time.strftime('%H:%M')}，覆盖最近 {cfg.get('hours_window', 30)} 小时")
    L.append("")

    if lv2:
        L.append("【必看】")
        L.append("-" * 30)
        for i, r in enumerate(lv2, 1):
            L.append(f"{i}. {r['title']}")
            L.append(f"   {r['source']}" + (f" · {r['why']}" if r.get("why") else ""))
        L.append("")

    if lv1:
        L.append(f"【要闻】共 {len(lv1)} 条")
        L.append("-" * 30)
        for r in lv1:
            L.append(f"· {r['title']}")
        L.append("")

    if site:
        L.append("=" * 30)
        L.append(f"完整日报（含原文链接、可翻历史）：{site}")
        L.append("")

    L.append("— 新闻收集器自动发送")
    return "\n".join(L), edition


def send_mail(cfg, subject, body):
    ec = cfg.get("email", {}) or {}
    if not ec.get("enabled"):
        return

    # 优先读环境变量（GitHub Actions 用 Secrets 注入，密码不会进仓库）
    sender = os.environ.get("NEWS_MAIL_SENDER") or ec.get("sender")
    password = os.environ.get("NEWS_MAIL_PASSWORD") or ec.get("password")
    receiver_raw = os.environ.get("NEWS_MAIL_RECEIVER") or ec.get("receiver") or sender
    host = os.environ.get("NEWS_MAIL_HOST") or ec.get("smtp_host", "smtp.qq.com")
    port = int(os.environ.get("NEWS_MAIL_PORT") or ec.get("smtp_port", 465))

    # 支持多个收件人：用逗号或分号分隔
    receivers = [x.strip() for x in re.split(r"[,;，；]", receiver_raw or "") if x.strip()]
    if not receivers:
        receivers = [sender]

    if not password or not sender:
        print("  [邮件] 未配置账号密码，跳过")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = ", ".join(receivers)
    # 缺 Date 和 Message-ID 会被邮件服务商当成垃圾邮件的典型特征，
    # 139 邮箱实测会因此拒收（550 Mail rejected score）。
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])
    msg["X-Mailer"] = "news-collector"
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=25) as s:
            s.login(sender, password)
            s.sendmail(sender, receivers, msg.as_string())
        print(f"  [邮件] 已发送至 {len(receivers)} 个地址：{', '.join(receivers)}")
    except Exception as e:
        print(f"  [邮件] 发送失败：{e}")


def load_today_json(cfg, date_str):
    """读回当天已保存的数据，转成内部结构，用于同一天多次运行时累加"""
    data_dir = cfg.get("data_dir", "data")
    if not os.path.isabs(data_dir):
        data_dir = os.path.join(BASE_DIR, data_dir)
    fp = os.path.join(data_dir, f"{date_str}.json")
    if not os.path.exists(fp):
        return []
    try:
        with open(fp, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []

    out = []
    for it in d.get("items", []):
        d_obj = None
        if it.get("time"):
            try:
                d_obj = dt.datetime.strptime(
                    f"{date_str} {it['time']}", "%Y-%m-%d %H:%M"
                ).astimezone()
            except Exception:
                d_obj = None
        title = it.get("title", "")
        link = it.get("link", "")
        out.append({
            "id": it.get("id") or item_id(link, title),
            "title": title,
            "link": link,
            "summary": it.get("summary", ""),
            "source": it.get("source", ""),
            "topics": it.get("topics", []) or [],
            "words": it.get("words", []) or [],
            "score": it.get("score", 0),
            "dt": d_obj,
            "ts": d_obj.timestamp() if d_obj else 0,
            "also": it.get("also", []) or [],
            "ntitle": norm_title(title),
            "is_key": it.get("level", 0) > 0,
            "level": it.get("level", 0),
            "why": it.get("why", ""),
            "tier": it.get("tier", ""),
        })
    return out


def write_json(items, cfg, run_time, feed_stats):
    """把当天结果存成 data/YYYY-MM-DD.json，供网页看板渲染"""
    data_dir = cfg.get("data_dir", "data")
    if not os.path.isabs(data_dir):
        data_dir = os.path.join(BASE_DIR, data_dir)
    os.makedirs(data_dir, exist_ok=True)

    payload = {
        "date": run_time.strftime("%Y-%m-%d"),
        "generated_at": run_time.strftime("%Y-%m-%d %H:%M"),
        "hours_window": int(cfg.get("hours_window", 30)),
        "count": len(items),
        "sources_ok": sum(1 for v in feed_stats.values() if v["ok"]),
        "sources_total": len(feed_stats),
        "topics_order": list((cfg.get("topics", {}) or {}).keys()) + ["其他"],
        "items": [
            {
                "id": it["id"],
                "title": it["title"],
                "link": it["link"],
                "summary": it["summary"][:300],
                "source": it["source"],
                "also": it.get("also", []),
                "topics": it["topics"],
                "words": it["words"][:5],
                "time": it["dt"].strftime("%H:%M") if it["dt"] else "",
                "score": round(it["score"], 1),
                "level": it.get("level", 0),
                "why": it.get("why", ""),
                "tier": it.get("tier", ""),
            }
            for it in items
        ],
    }
    fp = os.path.join(data_dir, f"{run_time.strftime('%Y-%m-%d')}.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print(f"数据已保存：{fp}")


def cleanup_old(output_dir, keep_days):
    if not os.path.isdir(output_dir):
        return
    cutoff = dt.datetime.now() - dt.timedelta(days=keep_days)
    for fn in os.listdir(output_dir):
        if not fn.endswith(".md"):
            continue
        fp = os.path.join(output_dir, fn)
        try:
            if dt.datetime.fromtimestamp(os.path.getmtime(fp)) < cutoff:
                os.remove(fp)
        except Exception:
            pass


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="不写文件，只打印统计")
    ap.add_argument("--no-dedup", action="store_true",
                    help="忽略历史记录，生成完整报告（云端定时任务用）")
    ap.add_argument("--no-merge", action="store_true",
                    help="不合并当天已生成的内容，直接覆盖")
    ap.add_argument("--no-mail", action="store_true",
                    help="只抓取不发邮件（用于白天加密抓取，避免频繁打扰）")
    ap.add_argument("--config", default=CONFIG_PATH)
    args = ap.parse_args()

    cfg = load_config(args.config)
    topics = cfg.get("topics", {}) or {}
    excludes = cfg.get("exclude_keywords", []) or []
    title_excludes = cfg.get("title_excludes", []) or []
    weak = set(cfg.get("weak_keywords", []) or [])
    must_kw = (cfg.get("must_watch", {}) or {}).get("keywords", []) or []
    feeds = [f for f in (cfg.get("feeds", []) or []) if f.get("enabled", True)]
    hours_window = int(cfg.get("hours_window", 30))
    # 没有时间字段的源，每个源最多取多少条
    max_undated = int(cfg.get("max_undated", 15))

    run_time = dt.datetime.now().astimezone()
    cutoff = run_time - dt.timedelta(hours=hours_window)
    state = load_state()

    print(f"开始抓取 {len(feeds)} 个信息源…")
    feed_stats = {}
    pool = {}
    intl_fail = 0          # 连续失败的海外源数量

    for fd in feeds:
        name = fd.get("name", fd.get("url", "?"))
        is_intl = bool(fd.get("intl"))
        # 海外源连续失败 3 个，说明当前网络到不了境外，剩下的直接跳过
        if is_intl and intl_fail >= 3:
            feed_stats[name] = {"ok": False, "count": 0, "error": "网络无法访问境外源，已跳过"}
            print(f"  ⏭️ {name}：跳过（境外源不可达）")
            continue
        # 支持 url 写成列表：主地址失败时按顺序切备用地址
        urls = fd.get("url", "")
        if isinstance(urls, str):
            urls = [urls]
        try:
            xml_text = None
            last_err = None
            # 境外源用短超时、不重试：连不上通常是网络不通，重试没意义
            tmo = 8 if is_intl else 20
            rty = 0 if is_intl else 2
            for u in urls:
                try:
                    xml_text = fetch(u, timeout=tmo, retries=rty)
                    break
                except Exception as e:
                    last_err = e
            if xml_text is None:
                raise last_err
            entries = parse_feed(xml_text)
            if not entries:
                raise ValueError("未解析到条目")
            is_must_src = fd.get("tier") == "must"
            cnt = 0
            undated = 0
            for title, link, summary, pub in entries:
                if not title or len(title) < 4:
                    continue
                pdt = parse_time(pub)
                if pdt:
                    if pdt < cutoff:
                        continue
                else:
                    # 源没提供时间（如新华网），无法判断新鲜度，
                    # 只按顺序取前若干条（RSS 一般最新在前），避免倒出整库旧闻
                    if undated >= max_undated:
                        continue
                    undated += 1
                text = f"{title} {summary}"
                # 排除
                if any(kw and kw in text for kw in excludes):
                    continue
                # 过滤评论、图解、纪录片等非新闻栏目
                if any(kw and kw in title for kw in title_excludes):
                    continue

                hits, words = match_topics(text, topics, weak, title)

                # 必收词只在标题里匹配：正文提到"证监会""GDP"往往只是顺带一句，
                # 标题命中才说明这条新闻本身讲的就是这件事
                hit_must = [k for k in must_kw if kw_hit(k, title)]

                # 分级：2=必看（命中必收词） 1=要闻（权威源） 0=普通兴趣
                if hit_must:
                    level = 2
                elif is_must_src:
                    level = 1
                elif not hits:
                    continue
                else:
                    level = 0
                    # 只在摘要里蹭到单个泛词而标题毫无关联的，丢弃
                    if not title_hits(title, topics) and len(words) == 1 and len(words[0]) <= 4:
                        continue

                iid = item_id(link, title)
                score = 0
                for t in hits:
                    score += topics[t].get("weight", 1)
                score += title_hits(title, topics) * 2
                # 分级加权：必看最优先，要闻次之
                if level == 2:
                    score += 10
                elif level == 1:
                    score += 4
                if hit_must:
                    score += min(len(hit_must), 3) * 2
                # 时效加权：越新越靠前
                if pdt:
                    age_h = (run_time - pdt).total_seconds() / 3600
                    score += max(0, 3 - age_h / 8)

                if iid in pool:
                    old = pool[iid]
                    if score > old["score"]:
                        old["score"] = score
                    if level > old.get("level", 0):
                        old["level"] = level
                    if hit_must and not old.get("why"):
                        old["why"] = hit_must[0]
                    continue

                pool[iid] = {
                    "id": iid,
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "source": name,
                    "topics": hits,
                    "words": words,
                    "score": score,
                    "dt": pdt,
                    "ts": pdt.timestamp() if pdt else 0,
                    "level": level,
                    "why": hit_must[0] if hit_must else "",
                    "tier": fd.get("tier", ""),
                }
                cnt += 1
            feed_stats[name] = {"ok": True, "count": cnt, "error": ""}
            print(f"  ✅ {name}：{cnt} 条")
            if is_intl:
                intl_fail = 0
        except Exception as e:
            msg = str(e)[:60] or e.__class__.__name__
            feed_stats[name] = {"ok": False, "count": 0, "error": msg}
            print(f"  ⚠️ {name}：跳过（{msg}）")
            if is_intl:
                intl_fail += 1
        time.sleep(0.8)  # 源之间留间隔，避免触发限流

    items = list(pool.values())

    # ---- 跨源同题去重：保留权重最高的一条，记录还有哪些源也报了 ----
    items.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))
    deduped = []
    for it in items:
        nt = norm_title(it["title"])
        dup_of = None
        for kept in deduped:
            if similar(nt, kept["ntitle"]):
                dup_of = kept
                break
        if dup_of is not None:
            if it["source"] not in dup_of["also"] and it["source"] != dup_of["source"]:
                dup_of["also"].append(it["source"])
            continue
        it["ntitle"] = nt
        it["also"] = []
        deduped.append(it)
    dropped = len(items) - len(deduped)
    if dropped:
        print(f"  同题去重：合并 {dropped} 条重复报道")
    items = deduped

    # 过滤已推送过的（首次运行不启用，避免第一天空白）
    # --no-dedup 时跳过这一步：云端每天只跑一次，不需要增量过滤
    is_first_run = len(state) == 0
    if not args.no_dedup and not is_first_run:
        fresh = [it for it in items if it["id"] not in state]
        skipped = len(items) - len(fresh)
        if skipped:
            print(f"  去重：跳过 {skipped} 条已推送过的")
        items = fresh

    items.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))
    print(f"\n本次新增 {len(items)} 条")

    # 同一天多次运行时，把新内容累加到已有日报，而不是覆盖。
    # 一天跑两次时，晚上那次不会因为某个源临时失败而丢掉早上的内容。
    date_str = run_time.strftime("%Y-%m-%d")
    if not args.no_merge:
        prev = load_today_json(cfg, date_str)
        if prev:
            have = {it["id"] for it in items}
            merged = items + [p for p in prev if p["id"] not in have]
            merged.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))
            if len(merged) > len(items):
                print(f"  累加当天已收录的 {len(prev)} 条，共 {len(merged)} 条")
            items = merged

    # 没有内容时不覆盖当天已有的日报（避免把好报告刷成空）
    output_dir = cfg.get("output_dir", "reports")
    if not os.path.isabs(output_dir):
        output_dir = os.path.join(BASE_DIR, output_dir)
    today_fp = os.path.join(output_dir, f"{date_str}.md")

    if not items:
        if os.path.exists(today_fp):
            print("今天没有新内容，保留已有日报不覆盖")
        else:
            print("今天没有新内容，且无历史日报，跳过生成")
        # 仍然可以发一封"无更新"的邮件，这里选择不发
        return

    report = build_report(items, cfg, run_time, feed_stats)

    if args.dry_run:
        print("\n--- 预览前 5 条 ---")
        for it in items[:5]:
            print(f"  [{it['source']}] {it['title']}")
        return

    os.makedirs(output_dir, exist_ok=True)
    with open(today_fp, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"报告已保存：{today_fp}")

    # 同时存一份结构化数据，供网页看板读取
    write_json(items, cfg, run_time, feed_stats)

    # 更新状态
    now_ts = run_time.timestamp()
    for it in items:
        state[it["id"]] = now_ts
    save_state(state)

    cleanup_old(output_dir, int(cfg.get("keep_days", 90)))

    # 邮件
    if args.no_mail:
        print("  [邮件] 本次只抓取，不发邮件")
        return
    key_n = sum(1 for it in items if it.get("level", 0) == 2)
    plain, edition = build_mail_body(items, cfg, run_time)
    send_mail(
        cfg,
        f"新闻{edition} {run_time.strftime('%m-%d')} · 必看 {key_n} 条 / 共 {len(items)} 条",
        plain,
    )


if __name__ == "__main__":
    main()
