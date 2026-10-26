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
from concurrent.futures import ThreadPoolExecutor, as_completed
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


# ---------------------------------------------------------------- 正文提取
# RSS 摘要经常只有一句导语（"中新网北京10月26日电(记者 xxx)…"），
# 真正的内容要点拿不到。所以打开原文页面，把正文前几段抽出来放进日报。
_BODY_SKIP = re.compile(
    r"(责任编辑|责编\s*[:：]|编辑\s*[:：]|来源\s*[:：]|供稿|原标题|扫一扫|二维码|分享到|"
    r"点击进入|点击查看|转载请注明|免责声明|版权声明|相关阅读|相关新闻|延伸阅读|"
    r"上一篇|下一篇|热门推荐|猜你喜欢|责任编辑|Copyright|All rights reserved|"
    r"Sign up|Subscribe|Read more|Advertisement|Cookie|JavaScript)", re.I)
_CREDIT_LINE = re.compile(r"^[（(][^）)]{0,40}(记者|编辑|摄影|通讯员|编译)[^）)]{0,60}[）)]$")
_CUT_MARK = re.compile(
    r"(相关阅读|相关新闻|延伸阅读|推荐阅读|猜你喜欢|热门推荐|责任编辑|责编\s*[:：]|"
    r"版权声明|免责声明|扫一扫|class=\"[^\"]*(?:related|recommend|ydtj|hotnews|tuijian)[^\"]*\")", re.I)
_SENT_END = re.compile(r"[。！？…”’][\"'”’]?$|[.!?][\"'”’)]$")


def _clean_para(s):
    s = re.sub(r"<[^>]*>", "", s)
    s = html.unescape(s)
    s = s.replace("\u3000", " ").replace("\xa0", " ").replace("\u200b", "")
    return re.sub(r"\s+", " ", s).strip()


def trim_sentence(text, limit):
    """按句号截断，不要把一个句子砍一半"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    ends = list(re.finditer(r"[。！？…]|[.!?](?=\s|$)", cut))
    if ends and ends[-1].end() >= limit * 0.5:
        return cut[:ends[-1].end()]
    return cut.rstrip() + "…"


def extract_body(page, target_chars=280, title=""):
    """从新闻页面 HTML 里抽出正文前几段"""
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)   # 注释里常藏着残留标签
    scope = page
    # 尽量缩小到正文容器，找不到就用整页
    for pat in (r'<article[^>]*>(.*?)</article>',
                r'<div[^>]+(?:id|class)="[^"]*(?:article|content|detail|post|body|txt)[^"]*"[^>]*>(.*)'):
        m = re.search(pat, page, re.S | re.I)
        if m and m.group(1).count("<p") >= 2:
            scope = m.group(1)
            break

    tnorm = norm_title(title)
    paras, blank = [], 0
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", scope, re.S | re.I):
        blk = m.group(1)
        p = _clean_para(blk)
        if len(p) < 10:
            continue
        # 整段被 <a> 包住的，是"相关推荐"里的标题链接，不是正文
        head = scope[:m.start()]
        if head.rfind("<a ") > head.rfind("</a>"):
            blank += 1
            if blank >= 2 and paras:
                break
            continue
        if _BODY_SKIP.search(p) or _CREDIT_LINE.match(p) or "-->" in p:
            continue
        if tnorm and norm_title(p)[:12] == tnorm[:12]:
            continue                      # 正文第一段往往是标题重复
        # 正文里的段落一般不带链接；带链接的短段落基本都是"相关推荐"的标题
        looks_like_headline = ("<a " in blk.lower() and len(p) < 70) or (
            not _SENT_END.search(p) and len(p) < 60)
        if looks_like_headline:
            blank += 1
            if blank >= 2 and paras:
                break                     # 后面是推荐阅读的标题列表
            continue
        blank = 0
        if p not in paras:
            paras.append(p)
        if sum(len(x) for x in paras) >= target_chars:
            break

    if not paras:
        m = re.search(
            r'<meta[^>]+(?:name|property)="(?:description|og:description)"[^>]+content="([^"]{40,})"',
            page, re.I)
        if m:
            paras = [_clean_para(m.group(1))]

    if not paras:
        return ""
    sep = "" if is_cjk_text(paras[0]) else " "
    return trim_sentence(sep.join(paras), target_chars)


def fetch_body(url, target_chars=280, timeout=10, title=""):
    try:
        page = fetch(url, timeout=timeout, retries=0)
    except Exception:
        return ""
    try:
        return extract_body(page, target_chars, title)
    except Exception:
        return ""


def enrich_items(items, cfg):
    """给摘要过短的新闻补抓正文，让每条下面都有内容可读"""
    ec = cfg.get("enrich", {}) or {}
    if ec.get("enabled", True) is False:
        return
    target = int(ec.get("target_chars", 280))
    min_chars = int(ec.get("min_chars", 150))
    max_items = int(ec.get("max_items", 60))
    timeout = int(ec.get("timeout", 10))
    workers = int(ec.get("workers", 6))

    # 必看优先，其次按分数
    order = sorted(items, key=lambda x: (-x.get("level", 0), -x["score"], -(x["ts"] or 0)))
    todo = [it for it in order
            if it.get("link") and len(it.get("summary", "")) < min_chars][:max_items]
    if not todo:
        return
    print(f"  补抓正文：{len(todo)} 条（并发 {workers}）…")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_body, it["link"], target, timeout, it["title"]): it for it in todo}
        for f in as_completed(futs):
            it = futs[f]
            try:
                body = f.result()
            except Exception:
                body = ""
            if body and len(body) > len(it.get("summary", "")):
                it["body"] = body
                it["summary"] = body
                done += 1
    print(f"  补抓正文：{done}/{len(todo)} 条成功")


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
    """标题归一化：去掉栏目前缀和标点空格，便于比较相似度"""
    t = t or ""
    # 去掉"外媒：""快讯丨""【独家】"这类栏目前缀，它们会让同一事件的标题看起来不一样
    t = re.sub(r"^\s*(?:【[^】]{0,10}】\s*)+", "", t)
    t = re.sub(
        r"^\s*(?:外媒|快讯|快讯丨|直击|独家|视频|受权发布|受权发布丨|观察|观察丨|聚焦|关注|"
        r"热点|现场|图集|专访|解读|评论|重磅|最新|突发|视频丨|直播)[：:丨|\s]+\s*", "", t)
    t = re.sub(r"[^\w\u4e00-\u9fff]", "", t.lower())
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
    # 数字对不上的一定不是同一条：公司财报、行情、比分这类标题句式一模一样，
    # 只有数字不同（"紫光国微…10.45 亿元" vs "长川科技…17.65 亿元"）
    na, nb = numbers_in(a), numbers_in(b)
    if na and nb and na != nb:
        return False
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


# ---------------------------------------------------------------- 同一事件聚类
# 标题相似度只能抓住"用词几乎一样"的稿子，抓不住：
#   「卢拉将第四次出任巴西总统」/「外媒：巴西总统卢拉赢得连任」/「外交部：中方祝贺卢拉在巴西总统大选中获胜」
# 这几条是同一件事，但标题写法差得远、相似度只有 0.3 左右。
# 所以换一种判法：把两条标题共有的原文片段找出来（"巴西总统""卢拉"），
# 去掉"外交部""表示""事故"这类官话套话之后，如果共有的实体片段够长、够多，
# 就判定为同一事件——合并成一条，其余来源作为"同题另报"链接列出，一条都不丢。
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")

# 官话套话：出现在共有片段里就丢掉，它们不能证明两条新闻讲的是同一件事
_ENT_BAD = (
    "外交", "交部", "发言", "言人", "记者", "采访", "报道", "消息", "表示", "宣布",
    "发布", "举行", "召开", "会议", "工作", "情况", "有关", "相关", "方面", "问题",
    "进行", "推进", "加强", "推动", "发展", "建设", "管理", "服务", "活动", "内容",
    "时间", "地方", "今年", "去年", "明年", "目前", "近日", "今天", "昨天", "我们",
    "他们", "其中", "此外", "以及", "通过", "实现", "事故", "发生", "致人", "受伤",
    "死亡", "遇难", "救援", "调查", "原因", "一起", "多人", "全国", "国家", "美国",
    "中国", "中方", "美方", "双方", "国际", "全球", "世界", "市场", "公司", "亿元",
    "美元", "同比", "增长", "下降", "上涨", "下跌", "季度", "净利润", "营收",
    "反对", "呼吁", "谴责", "支持", "合作", "携手", "一道", "继续", "深化", "促进",
    # 行业/主题大类词：两条新闻都提到"新能源汽车"不代表讲的是同一件事
    "新能源", "能源", "汽车", "机器人", "智能", "芯片", "手机", "半导体", "电动车",
    "房地产", "银行", "股市", "股价", "医药", "军工", "航天", "人工智能",
)

_EN_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "who", "why", "how",
    "what", "after", "before", "over", "into", "says", "said", "new", "his",
    "her", "its", "are", "was", "were", "has", "have", "had", "will", "would",
    "could", "should", "not", "but", "they", "their", "you", "your", "about",
    "more", "most", "than", "then", "when", "where", "which", "while", "amid",
    "ahead", "against", "between", "during", "under", "also", "just", "one",
    "two", "three", "first", "last", "next", "back", "down", "out", "off",
    "top", "get", "gets", "can", "may", "now", "still", "even", "here",
    "there", "these", "those", "such", "some", "many", "much", "very",
}

_NUM_TOKEN = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(s):
    return set(_NUM_TOKEN.findall(s or ""))


def is_cjk_text(s):
    return bool(re.search(r"[\u4e00-\u9fff]", s or ""))


def common_spans(a, b, min_len=2):
    """两条标题共有的原文片段（重叠的会合并成一段）"""
    if not a or not b:
        return []
    la, lb = len(a), len(b)
    dp = [[0] * (lb + 1) for _ in range(la + 1)]
    raw = []
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            if a[i - 1] == b[j - 1]:
                v = dp[i - 1][j - 1] + 1
                dp[i][j] = v
                if v >= min_len:
                    raw.append((i, v))
    if not raw:
        return []
    # 只保留"自身已是最长"的匹配，再把重叠的合并
    spans = []
    for i, v in raw:
        if any(i2 == i and v2 > v for i2, v2 in raw):
            continue
        spans.append((i - v, v))
    spans.sort()
    merged = []
    for s0, L in spans:
        if merged and s0 <= merged[-1][0] + merged[-1][1]:
            ps, pl = merged[-1]
            merged[-1] = (ps, max(ps + pl, s0 + L) - ps)
        else:
            merged.append((s0, L))
    return [a[s0:s0 + L] for s0, L in merged]


def same_event(a, b, same_source=False):
    """两条归一化标题是否在讲同一件事"""
    if not a or not b:
        return False
    if is_cjk_text(a) != is_cjk_text(b):
        return False           # 中英文稿不做跨语言匹配（没有翻译能力，容易误判）
    # 数字对不上的一定不是同一条：财报、行情类标题句式一模一样，只有数字不同
    na, nb = numbers_in(a), numbers_in(b)
    if na and nb and na != nb:
        return False

    if not is_cjk_text(a):
        wa = {w for w in _LATIN_WORD.findall(a.lower()) if w not in _EN_STOP}
        wb = {w for w in _LATIN_WORD.findall(b.lower()) if w not in _EN_STOP}
        inter = wa & wb
        return len(inter) >= 4 and len(inter) / max(1, min(len(wa), len(wb))) >= 0.5

    if similar(a, b):
        return True

    spans = [sp for sp in common_spans(a, b)
             if not any(bad in sp for bad in _ENT_BAD)]
    if not spans:
        return False
    total = sum(len(sp) for sp in spans)
    longest = max(len(sp) for sp in spans)
    if same_source:
        # 同一家媒体：共享 4 个字以上的实体片段就算（同一编辑部常换角度重写）
        return longest >= 4 and total >= 4
    # 不同来源：要求更硬，避免把同一主题的两条不同新闻合并掉
    return longest >= 6 and total >= 8


def cluster_items(items):
    """把讲同一件事的条目合并成一条，其余来源挂在 also 里，链接一个不丢"""
    norms = [norm_title(it.get("title", "")) for it in items]
    clusters = []
    for idx, it in enumerate(items):
        home = None
        for cl in clusters:
            if len(cl["members"]) >= 5:
                continue
            m = cl["rep"]          # 只跟代表条目比，避免 A→B→C 的连环误合并
            if same_event(norms[idx], norms[m],
                          same_source=(it.get("group") == items[m].get("group"))):
                home = cl
                break
        if home:
            home["members"].append(idx)
        else:
            clusters.append({"rep": idx, "members": [idx]})

    out = []
    for cl in clusters:
        rep = items[cl["rep"]]
        also = []
        for m in cl["members"][1:]:
            other = items[m]
            also.append({
                "src": other.get("source", ""),
                "t": other.get("title", ""),
                "u": other.get("link", ""),
            })
            # 合并时保留更高的重要级别和命中的必收词
            if other.get("level", 0) > rep.get("level", 0):
                rep["level"] = other["level"]
            if other.get("why") and not rep.get("why"):
                rep["why"] = other["why"]
        rep["also"] = also
        out.append(rep)
    return out


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
def _meta_line(r, mark_key=False):
    bits = []
    if r["dt"]:
        bits.append(r["dt"].strftime("%m-%d %H:%M"))
    bits.append(r["source"])
    if mark_key:
        if r.get("why"):
            bits.append(f"**{r['why']}**")
        elif r.get("tier") == "must":
            bits.append("**权威源**")
    elif r["words"]:
        bits.append(f"命中 {'、'.join(r['words'][:4])}")
    return " · ".join(bits)


def _also_line(r, style="md"):
    also = r.get("also") or []
    if not also:
        return ""
    # 老数据里 also 只存了来源名字符串，这里统一兼容一下
    norm = [(a if isinstance(a, dict) else {"src": str(a), "t": "", "u": ""}) for a in also]
    if style == "md":
        parts = []
        for a in norm[:4]:
            if not a.get("u"):
                parts.append(a.get("src", ""))
                continue
            t = a.get("t", "")
            label = f"{a['src']}｜{t[:22] + '…' if len(t) > 22 else t}" if t else a["src"]
            parts.append(f"[{label}]({a['u']})")
        return f"　<sub>同题另报：{' · '.join(parts)}</sub>"
    parts = [f"{a['src']} {a['u']}".strip() for a in norm[:2]]
    return "   同题另报：" + "；".join(parts)


def render_item(r, mark_key=False):
    L = [f"- **[{r['title']}]({r['link']})**",
         f"  <sub>{_meta_line(r, mark_key)}</sub>"]
    body = (r.get("body") or r.get("summary") or "").strip()
    if body:
        L.append(f"  {body}")
    extra = _also_line(r)
    if extra:
        L.append(extra)
    L.append("")
    return L


def split_en(items):
    """英文稿单独放一块：中英文没法比对，混在中文里会显得重复"""
    en = [it for it in items if not is_cjk_text(it.get("title", ""))]
    zh = [it for it in items if is_cjk_text(it.get("title", ""))]
    return zh, en


def cap_by_source(items, per_source):
    """每个源最多留几条，多的折叠起来（避免一个源刷满整屏）"""
    seen, keep, overflow = {}, [], []
    for it in items:
        n = seen.get(it["source"], 0)
        if n < per_source:
            seen[it["source"]] = n + 1
            keep.append(it)
        else:
            overflow.append(it)
    return keep, overflow


def build_report(items, cfg, run_time, feed_stats):
    hw = cfg.get("hours_window", 30)
    topics = cfg.get("topics", {}) or {}
    lim = cfg.get("limits", {}) or {}
    per_src = int(lim.get("max_per_feed", 8))
    per_topic = int(lim.get("max_per_topic", 12))

    lv2 = [it for it in items if it.get("level", 0) == 2]
    lv1 = [it for it in items if it.get("level", 0) == 1]
    key_ids = {it["id"] for it in lv2 + lv1}
    rest = [it for it in items if it["id"] not in key_ids]

    lv2.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))
    lv1.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))

    # 中文/英文分开：英文源没法和中文稿比对，混排会显得同一件事出现两次
    lv1_zh, lv1_en = split_en(lv1)
    rest_zh, rest_en = split_en(rest)

    lv1_show, lv1_more = cap_by_source(lv1_zh, per_src)

    grouped = {name: [] for name in topics}
    grouped["其他"] = []
    for it in rest_zh:
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

    L = []
    L.append(f"# 每日新闻简报 · {run_time.strftime('%Y-%m-%d')}")
    L.append("")
    L.append(f"> 生成时间：{run_time.strftime('%Y-%m-%d %H:%M')}　|　"
             f"必看 **{len(lv2)}** 条 · 要闻 **{len(lv1)}** 条 · 全部 **{total}** 条　|　"
             f"时间窗：最近 {hw} 小时")
    L.append(f"> 信息源：{sources_ok} 个正常" +
             (f"，{sources_fail} 个暂不可用（已自动跳过）" if sources_fail else ""))
    L.append("> 每条都带原文链接和正文摘录；同一事件多家报道只列一条，其余收在“同题另报”。")
    L.append("")

    if lv2:
        L.append(f"## ★★ 必看（{len(lv2)} 条）")
        L.append("")
        L.append("> 命中货币政策、监管政策、宏观数据、重大政治事件等必收关键词。**这部分保证不遗漏。**")
        L.append("")
        for r in lv2:
            L.extend(render_item(r, mark_key=True))

    if lv1_show:
        L.append(f"## ★ 要闻（{len(lv1)} 条，先列 {len(lv1_show)} 条）")
        L.append("")
        L.append("> 来自中国新闻网、国家统计局、美联储等权威源的原发内容。")
        L.append("")
        for r in lv1_show:
            L.extend(render_item(r, mark_key=True))

    L.append("## 今日概览")
    L.append("")
    if lv2:
        L.append(f"- **★★ 必看**：{len(lv2)} 条（见上）")
    if lv1:
        L.append(f"- **★ 要闻**：{len(lv1)} 条（见上）")
    for name in list(topics.keys()) + ["其他"]:
        if grouped[name]:
            L.append(f"- **{name}**：{len(grouped[name])} 条")
    if rest_en or lv1_en:
        L.append(f"- **外媒原文**：{len(lv1_en) + len(rest_en)} 条（英文）")
    L.append("")

    for name in list(topics.keys()) + ["其他"]:
        rows = grouped[name]
        if not rows:
            continue
        rows.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))
        show, more = rows[:per_topic], rows[per_topic:]
        L.append(f"## {name}（{len(rows)} 条）")
        L.append("")
        for r in show:
            L.extend(render_item(r))
        if more:
            L.append("<details><summary>展开该主题其余 "
                     f"{len(more)} 条</summary>")
            L.append("")
            for r in more:
                L.extend(render_item(r))
            L.append("</details>")
            L.append("")

    # 要闻超出的部分折叠，不丢内容但不占版面
    if lv1_more:
        L.append(f"### 要闻 · 其余 {len(lv1_more)} 条")
        L.append("")
        L.append("<details><summary>展开全部</summary>")
        L.append("")
        for r in lv1_more:
            L.extend(render_item(r, mark_key=True))
        L.append("</details>")
        L.append("")

    en_all = lv1_en + rest_en
    if en_all:
        en_all.sort(key=lambda x: (-x.get("level", 0), -x["score"], -(x["ts"] or 0)))
        show, more = cap_by_source(en_all, int(lim.get("max_per_feed_en", 5)))
        L.append(f"## 外媒原文 · 英文（{len(en_all)} 条）")
        L.append("")
        L.append("> BBC、纽约时报等英文源的原始报道，按源限量，方便对照着看外媒口径。")
        L.append("")
        for r in show:
            L.extend(render_item(r, mark_key=(r.get("level", 0) > 0)))
        if more:
            L.append("<details><summary>其余外媒报道</summary>")
            L.append("")
            for r in more:
                L.extend(render_item(r, mark_key=(r.get("level", 0) > 0)))
            L.append("</details>")
            L.append("")

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
    """邮件正文。

    邮件要满足三件事：每条有原文链接、每条有正文摘录、条数不能太多。
    所以必看全给，要闻和兴趣各按分数取前若干条，其余引导去网页看。
    """
    lim = cfg.get("limits", {}) or {}
    n1 = int(lim.get("mail_must", 20))
    n2 = int(lim.get("mail_level1", 12))
    n3 = int(lim.get("mail_interest", 8))
    en_n = int(lim.get("mail_en", 3))
    body_chars = int(lim.get("mail_body_chars", 180))

    lv2 = [it for it in items if it.get("level", 0) == 2]
    lv1 = [it for it in items if it.get("level", 0) == 1]
    lv0 = [it for it in items if it.get("level", 0) == 0]
    for lst in (lv2, lv1, lv0):
        lst.sort(key=lambda x: (-x["score"], -(x["ts"] or 0)))

    lv1_zh, lv1_en = split_en(lv1)
    lv0_zh, lv0_en = split_en(lv0)
    hour = run_time.hour
    edition = "早报" if hour < 15 else "晚报"
    site = cfg.get("site_url", "")

    L = []
    L.append(f"新闻{edition} · {run_time.strftime('%Y年%m月%d日')}")
    L.append("=" * 34)
    L.append(f"共 {len(items)} 条：必看 {len(lv2)} · 要闻 {len(lv1)} · 兴趣 {len(lv0)}")
    L.append(f"生成于 {run_time.strftime('%H:%M')}，覆盖最近 {cfg.get('hours_window', 30)} 小时")

    def block(title, rows, note=""):
        if not rows:
            return
        L.append("")
        L.append(f"【{title}】")
        L.append("-" * 34)
        if note:
            L.append(note)
        for i, r in enumerate(rows, 1):
            L.append(f"{i}. {r['title']}")
            meta = []
            if r["dt"]:
                meta.append(r["dt"].strftime("%m-%d %H:%M"))
            meta.append(r["source"])
            if r.get("why"):
                meta.append(r["why"])
            L.append("   " + " · ".join(meta))
            body = (r.get("body") or r.get("summary") or "").strip()
            if body:
                L.append("   " + trim_sentence(body, body_chars))
            L.append(f"   原文：{r['link']}")
            ext = _also_line(r, style="mail")
            if ext:
                L.append(ext)

    block("必看", lv2[:n1], "命中货币政策、监管、宏观数据、重大政治事件等必收关键词，保证不遗漏。")
    if len(lv2) > n1:
        L.append(f"   还有 {len(lv2) - n1} 条必看内容在网页版")

    block("要闻 · 精选", lv1_zh[:n2], "中国新闻网、国家统计局、美联储等权威源的原发内容。")
    if len(lv1_zh) > n2:
        L.append(f"   还有 {len(lv1_zh) - n2} 条要闻在网页版")

    block("兴趣", lv0_zh[:n3])
    if len(lv0_zh) > n3:
        L.append(f"   还有 {len(lv0_zh) - n3} 条在网页版")

    en_rows = lv1_en + lv0_en
    if en_rows:
        L.append("")
        L.append(f"【外媒原文 · {len(en_rows)} 条，先列 {min(en_n, len(en_rows))} 条】")
        L.append("-" * 34)
        for r in en_rows[:en_n]:
            L.append(f"· {r['title']}")
            L.append(f"  {r['source']}　{r['link']}")

    if site:
        L.append("")
        L.append("=" * 34)
        L.append(f"完整 {len(items)} 条（按主题分类、可翻历史）：")
        L.append(site)
        L.append("")

    L.append("— 新闻收集器自动发送，不要回复本邮件")
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
            "body": it.get("body", ""),
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
            "group": it.get("group", ""),
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
                "summary": it["summary"][:400],
                "body": it.get("body", "")[:400],
                "source": it["source"],
                "also": it.get("also", [])[:5],
                "topics": it["topics"],
                "words": it["words"][:5],
                "time": it["dt"].strftime("%H:%M") if it["dt"] else "",
                "score": round(it["score"], 1),
                "level": it.get("level", 0),
                "why": it.get("why", ""),
                "tier": it.get("tier", ""),
                "group": it.get("group", ""),
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
                    "group": fd.get("group") or name,
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

    # ---- 同一事件合并：巴西大选被六家媒体各写一条，只留一条，其余挂在"同题另报" ----
    items.sort(key=lambda x: (-x["level"], -x["score"], -(x["ts"] or 0)))
    before = len(items)
    items = cluster_items(items)
    if before > len(items):
        print(f"  同题合并：{before} 条合并为 {len(items)} 条（其余来源记在“同题另报”）")

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

    # 摘要太短的去原文页面补抓正文，让每条新闻下面都有实质内容
    enrich_items(items, cfg)

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
