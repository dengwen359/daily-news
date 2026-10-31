#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 data/*.json 渲染成一个单文件网页看板（docs/index.html）
单文件的好处：本地双击能看，丢到 GitHub Pages 也能看，不依赖服务器。
"""

import os
import re
import json
import html
import datetime as dt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
OUT_DIR = os.path.join(BASE_DIR, "docs")
MAX_DAYS = 60          # 只保留最近 60 天，控制页面体积
SUMMARY_LIMIT = 240    # 正文摘录截断字数


def load_all():
    if not os.path.isdir(DATA_DIR):
        return []
    days = []
    for fn in sorted(os.listdir(DATA_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(DATA_DIR, fn), "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("items"):
                days.append(d)
        except Exception:
            continue
    days.sort(key=lambda x: x.get("date", ""), reverse=True)
    return days[:MAX_DAYS]


def esc(s):
    return html.escape(str(s or ""), quote=True)


def short(s, n=SUMMARY_LIMIT):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def fmt_date_cn(ds):
    """2026-10-26 -> 10月26日 周一"""
    try:
        d = dt.datetime.strptime(ds, "%Y-%m-%d")
        wk = "一二三四五六日"[d.weekday()]
        return f"{d.month}月{d.day}日 周{wk}"
    except Exception:
        return ds


def build_html(days):
    if not days:
        return ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>每日新闻</title></head><body style='font-family:system-ui;"
                "padding:40px;color:#444'><h2>还没有数据</h2>"
                "<p>先运行一次 collector.py，再运行 build_site.py。</p></body></html>")

    # 内嵌数据：去掉用不到的字段以压缩体积
    slim = []
    for d in days:
        items = []
        for it in d.get("items", []):
            items.append({
                "t": it.get("title", ""),
                "u": it.get("link", ""),
                "s": short(it.get("body") or it.get("summary", "")),
                "src": it.get("source", ""),
                "also": it.get("also", []),
                "tp": it.get("topics", []),
                "w": (it.get("words") or [])[:4],
                "tm": it.get("time", ""),
                "lv": it.get("level", 1 if it.get("key") else 0),
                "wy": it.get("why", ""),
                "tr": it.get("tier", ""),
            })
        slim.append({
            "date": d.get("date", ""),
            "gen": d.get("generated_at", ""),
            "hw": d.get("hours_window", 30),
            "ok": d.get("sources_ok", 0),
            "tot": d.get("sources_total", 0),
            "order": d.get("topics_order", []),
            "items": items,
        })

    payload = json.dumps(slim, ensure_ascii=False, separators=(",", ":"))
    latest = slim[0]
    total_now = len(latest["items"])

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日新闻简报</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  :root{{
    --bg:#f5f6f8; --card:#ffffff; --line:#e6e8ec;
    --tx:#1a1d21; --tx2:#5c6470; --tx3:#8b939e;
    --accent:#2b5c8a; --accent-soft:#eaf1f8;
  }}
  body{{
    background:var(--bg); color:var(--tx);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    line-height:1.6; -webkit-font-smoothing:antialiased;
  }}
  .wrap{{max-width:880px;margin:0 auto;padding:28px 18px 70px}}

  header{{margin-bottom:22px}}
  h1{{font-size:23px;font-weight:650;letter-spacing:-.3px}}
  .sub{{color:var(--tx3);font-size:13px;margin-top:6px}}

  .stats{{display:flex;gap:10px;margin:18px 0 20px;flex-wrap:wrap}}
  .stat{{
    background:var(--card);border:1px solid var(--line);border-radius:10px;
    padding:11px 15px;flex:1;min-width:100px;
  }}
  .stat b{{display:block;font-size:20px;font-weight:650;color:var(--accent);line-height:1.3}}
  .stat span{{font-size:12px;color:var(--tx3)}}

  .bar{{display:flex;gap:8px;align-items:center;margin-bottom:18px;flex-wrap:wrap}}
  select,input{{
    font-family:inherit;font-size:14px;padding:8px 11px;border-radius:8px;
    border:1px solid var(--line);background:var(--card);color:var(--tx);outline:none;
  }}
  select{{cursor:pointer;font-weight:550}}
  input{{flex:1;min-width:150px}}
  input:focus,select:focus{{border-color:var(--accent)}}

  .tabs{{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:20px}}
  .tab{{
    font-size:13px;padding:6px 13px;border-radius:20px;cursor:pointer;
    background:var(--card);border:1px solid var(--line);color:var(--tx2);
    user-select:none;transition:.15s;
  }}
  .tab:hover{{border-color:#c8ced6}}
  .tab.on{{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:550}}

  .grp{{margin-bottom:26px}}
  .grp-h{{
    font-size:13px;font-weight:650;color:var(--tx2);letter-spacing:.4px;
    margin-bottom:11px;display:flex;align-items:center;gap:8px;
  }}
  .grp-h:after{{content:"";flex:1;height:1px;background:var(--line)}}

  .item{{
    background:var(--card);border:1px solid var(--line);border-radius:11px;
    padding:14px 16px;margin-bottom:9px;transition:.15s;
  }}
  .item:hover{{border-color:#cdd4dd;box-shadow:0 2px 10px rgba(30,40,60,.05)}}
  .item a.title{{
    color:var(--tx);text-decoration:none;font-size:15.5px;font-weight:600;
    display:block;line-height:1.45;
  }}
  .item a.title:hover{{color:var(--accent)}}
  .meta{{
    font-size:12px;color:var(--tx3);margin-top:7px;
    display:flex;gap:9px;flex-wrap:wrap;align-items:center;
  }}
  .meta .dot{{color:#d3d8de}}
  .badge{{
    background:var(--accent-soft);color:var(--accent);
    padding:1px 7px;border-radius:4px;font-size:11.5px;font-weight:550;
  }}
  .sum{{font-size:13.5px;color:var(--tx2);margin-top:9px;line-height:1.65}}
  .ai{{margin-top:9px;padding:8px 10px;background:var(--panel,#f2f6fb);border-left:3px solid #2b5c8a;border-radius:4px;font-size:12.5px;color:#41506b;line-height:1.65}}
  .also{{font-size:12px;color:var(--tx3);margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}}
  .also a{{color:var(--accent);text-decoration:none}}
  .also a:hover{{text-decoration:underline}}

  .empty{{text-align:center;color:var(--tx3);padding:50px 0;font-size:14px}}
  footer{{text-align:center;color:var(--tx3);font-size:12px;margin-top:34px}}
  footer a{{color:var(--accent);text-decoration:none}}

  /* 重点区 */
  .item.key{{border-left:3px solid #c9a227;background:#fffdf6}}
  .item.key:hover{{border-color:#c9a227;box-shadow:0 2px 10px rgba(180,150,40,.12)}}
  .grp-h.keyhead{{color:#a8841c}}
  .grp-h.keyhead:after{{background:#f0e2bb}}
  .tab.keytab{{color:#a8841c;border-color:#e8d9a8}}
  .tab.keytab.on{{background:#c9a227;border-color:#c9a227;color:#fff}}

  @media(max-width:520px){{
    .wrap{{padding:20px 13px 60px}}
    h1{{font-size:20px}}
    .stat b{{font-size:17px}}
  }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>每日新闻简报</h1>
    <div class="sub" id="subbar"></div>
  </header>

  <div class="stats">
    <div class="stat"><b id="s-count">0</b><span>当日收录</span></div>
    <div class="stat"><b id="s-days">0</b><span>累计天数</span></div>
    <div class="stat"><b id="s-src">0</b><span>源在线</span></div>
  </div>

  <div class="bar">
    <select id="dateSel"></select>
    <input id="q" type="search" placeholder="搜索标题、摘要、来源…">
  </div>

  <div class="tabs" id="tabs"></div>
  <div id="list"></div>
  <footer>自动生成 · 数据来自公开 RSS · 共 {len(slim)} 天记录</footer>
</div>

<script>
const DATA = {payload};
const WEEK = ['日','一','二','三','四','五','六'];
let curDate = DATA[0].date;
let curTopic = '__all__';
let curQ = '';

// 支持 ?t=分类 或 #t=分类 直达某个分类（邮件里的类别按钮带这个参数，
// 手机邮箱不支持邮件内锚点跳转，所以按钮直接打开看板并定位）
(function() {{
  try {{
    let t = new URLSearchParams(location.search).get('t');
    if (!t && location.hash.length > 1) {{
      t = decodeURIComponent(location.hash.slice(1));
      if (t.indexOf('t=') === 0) t = t.slice(2);
    }}
    if (t && t !== 'all') curTopic = t;
  }} catch (e) {{}}
}})();

const $ = id => document.getElementById(id);

function fmtCN(ds){{
  const d = new Date(ds + 'T00:00:00');
  if (isNaN(d)) return ds;
  return `${{d.getMonth()+1}}月${{d.getDate()}}日 周${{WEEK[d.getDay()]}}`;
}}

function curDay(){{
  return DATA.find(d => d.date === curDate) || DATA[0];
}}

function initDateSel(){{
  const sel = $('dateSel');
  sel.innerHTML = DATA.map((d,i) =>
    `<option value="${{d.date}}">${{fmtCN(d.date)}}${{i===0?'（最新）':''}}</option>`
  ).join('');
  sel.value = curDate;
  sel.onchange = e => {{
    curDate = e.target.value;
    curTopic = '__all__';   // 换日期时回到"全部"，避免主题在该日不存在
    render();
  }};
}}

function buildTabs(){{
  const d = curDay();
  const set = new Set();
  d.items.forEach(it => (it.tp||[]).forEach(t => set.add(t)));
  const order = (d.order||[]).filter(t => set.has(t));
  const others = [...set].filter(t => !order.includes(t));
  const all = [...order, ...others];
  // 从 URL 直达的分类如果当天不存在，回落到"全部"，避免开着空列表
  if (curTopic !== '__all__' && curTopic !== '__key__' && curTopic !== '__news__'
      && !all.includes(curTopic)) {{
    curTopic = '__all__';
  }}
  const keyN = d.items.filter(it => it.lv === 2).length;
  const newsN = d.items.filter(it => it.lv === 1).length;

  const tabs = $('tabs');
  let h = `<div class="tab ${{curTopic==='__all__'?'on':''}}" data-t="__all__">全部 ${{d.items.length}}</div>`;
  if (keyN) {{
    h += `<div class="tab keytab ${{curTopic==='__key__'?'on':''}}" data-t="__key__">★★ 突发必看 ${{keyN}}</div>`;
  }}
  if (newsN) {{
    h += `<div class="tab ${{curTopic==='__news__'?'on':''}}" data-t="__news__">★ 政策要闻 ${{newsN}}</div>`;
  }}
  all.forEach(t => {{
    const n = d.items.filter(it => (it.tp||[]).includes(t)).length;
    if (n) h += `<div class="tab ${{curTopic===t?'on':''}}" data-t="${{t}}">${{t}} ${{n}}</div>`;
  }});
  tabs.innerHTML = h;
  tabs.querySelectorAll('.tab').forEach(el => {{
    el.onclick = () => {{ curTopic = el.dataset.t; render(); }};
  }});
}}

function itemHTML(it, lv){{
  const isKey = lv >= 2;
  const showWhy = lv > 0;
  const meta = [];
  if (it.tm) meta.push(it.tm);
  if (it.src) meta.push(it.src);
  if ((it.also||[]).length) meta.push(`同题 ${{it.also.length}} 家`);
  if (showWhy) {{
    if (it.wy) meta.push(`<b>${{esc(it.wy)}}</b>`);
    else if (it.tr === 'must') meta.push('<b>权威源</b>');
  }} else if ((it.w||[]).length) {{
    meta.push('命中 ' + it.w.join('、'));
  }}
  let h = `<div class="item${{isKey?' key':''}}">`;
  h += `<a class="title" href="${{esc(it.u)}}" target="_blank" rel="noopener">${{esc(it.t)}}</a>`;
  h += '<div class="meta">' + meta.map((m,i) =>
        (i ? '<span class="dot">·</span>' : '') + `<span>${{m}}</span>`
      ).join('') + '</div>';
  if (it.s) h += `<div class="sum">${{esc(it.s)}}</div>`;
  if (it.ai) h += `<div class="ai">AI分析：${{esc(it.ai)}}</div>`;
  if ((it.also||[]).length) {{
    h += '<div class="also">同题另报：' + it.also.map(a =>
      `<a href="${{esc(a.u)}}" target="_blank" rel="noopener">${{esc(a.src)}}</a>`
    ).join(' · ') + '</div>';
  }}
  h += '</div>';
  return h;
}}

function esc(s){{
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}}

function render(){{
  const d = curDay();
  $('s-count').textContent = d.items.length;
  $('s-days').textContent = DATA.length;
  $('s-src').textContent = d.ok + '/' + d.tot;
  $('subbar').textContent =
    `${{fmtCN(d.date)}} · 生成于 ${{d.gen}} · 时间窗最近 ${{d.hw}} 小时`;

  buildTabs();

  const box = $('list');
  const all = d.items;
  const mustRead = all.filter(it => it.lv === 2);
  const headlines = all.filter(it => it.lv === 1);

  // ---- 搜索：不分主题，全局平铺 ----
  if (curQ) {{
    const q = curQ.toLowerCase();
    const hits = all.filter(it =>
      (it.t||'').toLowerCase().includes(q) ||
      (it.s||'').toLowerCase().includes(q) ||
      (it.src||'').toLowerCase().includes(q)
    );
    if (!hits.length) {{
      box.innerHTML = `<div class="empty">没有匹配「${{esc(curQ)}}」的内容</div>`;
      return;
    }}
    box.innerHTML = `<div class="grp"><div class="grp-h">搜索「${{esc(curQ)}}」 · ${{hits.length}}</div>`
      + hits.map(it => itemHTML(it, it.lv)).join('') + '</div>';
    return;
  }}

  // ---- 只看必看 ----
  if (curTopic === '__key__') {{
    if (!mustRead.length) {{
      box.innerHTML = `<div class="empty">今天没有必看内容</div>`;
      return;
    }}
    box.innerHTML = `<div class="grp"><div class="grp-h keyhead">★★ 突发必看 · ${{mustRead.length}}</div>`
      + mustRead.map(it => itemHTML(it, 2)).join('') + '</div>';
    return;
  }}

  // ---- 只看要闻 ----
  if (curTopic === '__news__') {{
    if (!headlines.length) {{
      box.innerHTML = `<div class="empty">今天没有要闻</div>`;
      return;
    }}
    box.innerHTML = `<div class="grp"><div class="grp-h">★ 政策要闻 · ${{headlines.length}}</div>`
      + headlines.map(it => itemHTML(it, 1)).join('') + '</div>';
    return;
  }}

  // ---- 全部：必看 → 要闻 → 各主题 ----
  if (curTopic === '__all__') {{
    let h = '';
    if (mustRead.length) {{
      h += `<div class="grp"><div class="grp-h keyhead">★★ 突发必看 · ${{mustRead.length}}</div>`;
      h += mustRead.map(it => itemHTML(it, 2)).join('');
      h += '</div>';
    }}
    if (headlines.length) {{
      h += `<div class="grp"><div class="grp-h">★ 政策要闻 · ${{headlines.length}}</div>`;
      h += headlines.map(it => itemHTML(it, 1)).join('');
      h += '</div>';
    }}
    const rest = all.filter(it => it.lv === 0);
    const groups = [];
    const used = new Set();
    (d.order||[]).forEach(t => {{
      const rows = rest.filter(it => (it.tp||[]).includes(t) && !used.has(it.u + it.t));
      if (rows.length) {{
        rows.forEach(it => used.add(it.u + it.t));
        groups.push([t, rows]);
      }}
    }});
    const left = rest.filter(it => !used.has(it.u + it.t));
    if (left.length) groups.push(['其他', left]);

    groups.forEach(([name, rows]) => {{
      h += `<div class="grp"><div class="grp-h">${{esc(name)}} · ${{rows.length}}</div>`;
      h += rows.map(it => itemHTML(it, 0)).join('');
      h += '</div>';
    }});
    box.innerHTML = h || `<div class="empty">今天没有内容</div>`;
    return;
  }}

  // ---- 单个主题 ----
  const rows = all.filter(it => (it.tp||[]).includes(curTopic));
  if (!rows.length) {{
    box.innerHTML = `<div class="empty">这个分类今天没有新闻</div>`;
    return;
  }}
  box.innerHTML = `<div class="grp"><div class="grp-h">${{esc(curTopic)}} · ${{rows.length}}</div>`
    + rows.map(it => itemHTML(it, it.lv)).join('') + '</div>';
}}

$('q').addEventListener('input', e => {{
  curQ = e.target.value.trim();
  render();
}});

initDateSel();
render();
</script>
</body>
</html>"""


def main():
    days = load_all()
    html_text = build_html(days)
    os.makedirs(OUT_DIR, exist_ok=True)
    fp = os.path.join(OUT_DIR, "index.html")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(html_text)
    size = len(html_text.encode("utf-8")) / 1024
    print(f"看板已生成：{fp}（{len(days)} 天，{size:.0f} KB）")


if __name__ == "__main__":
    main()
