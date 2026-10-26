#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把云端抓好的新闻推成一条 Windows 桌面通知。

云端（GitHub Actions）负责抓取，本机只做三件事：
  1. 取回 data/YYYY-MM-DD.json —— 先走 api.github.com（本机实测可通），
     失败再退回网页看板里内嵌的那份数据；
  2. 挑出"必看"条目；
  3. 用系统通知中心弹一条，点击直接打开看板。

只用标准库，不装任何东西，一次运行 1～3 秒。
"""
import argparse
import base64
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import webbrowser

REPO = "dengwen359/daily-news"
SITE = "https://dengwen359.github.io/daily-news/"
AUMID = "Dengwen.NewsDesktop"
APP_NAME = "每日新闻"

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, ".state", "notified.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 news-desktop/1.0"


# ---------------------------------------------------------------- 取数据
def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                              "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def load_day(date_str):
    """读某一天的完整数据；返回 (data, 来源说明)"""
    api_err = ""
    try:
        raw = _get(f"https://api.github.com/repos/{REPO}/contents/data/{date_str}.json?ref=main")
        d = json.loads(raw.decode("utf-8"))
        return json.loads(base64.b64decode(d["content"]).decode("utf-8")), "api"
    except Exception as e:
        api_err = f"接口: {e}"

    # 备用通道：网页看板把数据内嵌在 const DATA 里，去掉 base64 这一步一样能拿到
    try:
        html = _get(SITE, 30).decode("utf-8", "ignore")
        i = html.index("const DATA")
        j = html.index("[", i)
        days, _ = json.JSONDecoder().raw_decode(html[j:])
        for d in days:
            if d.get("date") == date_str:
                items = [{
                    "title": x.get("t", ""), "link": x.get("u", ""),
                    "summary": x.get("s", ""), "source": x.get("src", ""),
                    "level": x.get("lv", 0), "time": x.get("tm", ""),
                    "also": x.get("also", []),
                } for x in d.get("items", [])]
                return {
                    "date": date_str, "items": items, "count": len(items),
                    "sources_ok": d.get("ok", 0), "sources_total": d.get("tot", 0),
                }, "网页"
    except Exception as e:
        raise RuntimeError(f"{api_err}；网页: {e}")


def latest_day(max_back=4, tries=1, wait=90):
    """今天没数据就往回找（云端可能还没跑完），最多返回 max_back 天前"""
    today = dt.date.today()
    err = ""
    for n in range(max_back):
        day = today - dt.timedelta(days=n)
        ds = day.strftime("%Y-%m-%d")
        for k in range(tries):
            try:
                return ds, load_day(ds)[0]
            except Exception as e:
                err = str(e)
                if k + 1 < tries:
                    print(f"  {ds} 取数据失败，{wait} 秒后重试（{e}）")
                    time.sleep(wait)
    raise RuntimeError(err or "取不到数据")


# ---------------------------------------------------------------- 组装通知
def pick_items(data, lines=3):
    """必看优先，不足用要闻补齐"""
    items = [it for it in (data.get("items") or []) if it.get("title")]
    must = [it for it in items if (it.get("level") or 0) >= 2]
    head = [it for it in items if (it.get("level") or 0) == 1]
    key = lambda x: (-(x.get("score") or 0), x.get("time") or "")
    must.sort(key=key)
    head.sort(key=key)

    rows = must[:lines]
    tag = {id(it): "必看" for it in rows}
    if len(rows) < lines:
        for it in head[:lines - len(rows)]:
            rows.append(it)
            tag[id(it)] = "要闻"
    return rows, tag, len(must), len(head), len(items)


def shorten(t, n=30):
    t = re.sub(r"\s+", " ", (t or "").strip())
    return t if len(t) <= n else t[:n - 1] + "…"


def build_xml(date_str, rows, tag, n_must, n_head, total, is_today=True):
    mmdd = date_str[5:].replace("-", "/")
    prefix = "" if is_today else "昨日 · "
    if n_must:
        title = f"{prefix}{mmdd} 新闻 · 必看 {n_must} 条 / 共 {total} 条"
    elif total:
        title = f"{prefix}{mmdd} 新闻 · 今日 {total} 条"
    else:
        title = f"{prefix}{mmdd} 新闻"

    body = []
    for i, it in enumerate(rows, 1):
        label = tag.get(id(it), "要闻")
        body.append(f"{i}. [{label}] {shorten(it.get('title', ''))}")
    if not body:
        body.append("今天还没有抓到内容，稍后再看。")

    def esc(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;"))

    parts = [
        f'<toast activationType="protocol" launch="{SITE}" duration="long">',
        '<visual><binding template="ToastGeneric">',
        f"<text>{esc(title)}</text>",
    ]
    for b in body:
        parts.append(f"<text>{esc(b)}</text>")
    parts.append("</binding></visual>")
    parts.append('<actions><action content="打开看板" activationType="protocol" '
                 f'arguments="{SITE}"/></actions>')
    parts.append('<audio src="ms-winsoundevent:Notification.Default"/>')
    parts.append("</toast>")
    return "".join(parts)


# ---------------------------------------------------------------- 弹通知
PS_SCRIPT = r"""
param([string]$XmlPath, [string]$AppId)
$ErrorActionPreference = "Stop"
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = [System.IO.File]::ReadAllText($XmlPath, [System.Text.Encoding]::UTF8)
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AppId).Show($toast)
"""


def register_app_id():
    """在 HKCU 登记通知来源，通知里就显示"每日新闻"，而不是"Windows PowerShell" """
    try:
        import winreg
        path = r"Software\Classes\AppUserModelId\\" + AUMID
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "ShowInSettings", 0, winreg.REG_DWORD, 1)
        return True
    except Exception:
        return False


def show_toast(xml_text):
    tmp_xml = os.path.join(HERE, "_toast.xml")
    tmp_ps = os.path.join(HERE, "_toast.ps1")
    with open(tmp_xml, "w", encoding="utf-8") as f:
        f.write(xml_text)
    with open(tmp_ps, "w", encoding="utf-8") as f:
        f.write(PS_SCRIPT)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", tmp_ps, "-XmlPath", tmp_xml, "-AppId", AUMID],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout or "").strip()[:300])
    finally:
        for p in (tmp_xml, tmp_ps):
            try:
                os.remove(p)
            except OSError:
                pass


# ---------------------------------------------------------------- 状态
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


SLOT_AM, SLOT_PM = 840, 2040      # 08:40 / 20:40，和计划任务里的时间一致


def slot_key(date_str, now=None):
    """手动模式下算当前属于哪一份：15 点前算早上那份，之后算晚上那份"""
    now = now or dt.datetime.now()
    return f"{date_str}-{'am' if now.hour < 15 else 'pm'}"


def due_slots(now):
    """今天到此刻为止，本该已经推送过的时段"""
    hm = now.hour * 100 + now.minute
    out = []
    if hm >= SLOT_AM:
        out.append("am")
    if hm >= SLOT_PM:
        out.append("pm")
    return out


# ---------------------------------------------------------------- 主流程
def run_auto(args):
    """巡检模式：只在"今天该弹的那份还没弹过"时才真弹，否则一声不响地退出。

    计划任务每 30 分钟跑一次，所以哪怕 08:40 时电脑是关着的，
    只要当天还没收到过，开机后的下一次巡检就会补上——
    不需要赌 Windows 的"错过任务补跑"能不能正常工作。
    """
    now = dt.datetime.now()
    today = now.strftime("%Y-%m-%d")
    st = load_state()
    due = due_slots(now)
    if not due:
        return 0                                  # 还没到今天第一次推送的时间
    latest = due[-1]
    if st.get(f"{today}-{latest}"):
        return 0                                  # 最近这份已经推过，安静退出

    try:
        date_str, data = latest_day(tries=2, wait=60)
    except Exception as e:
        print(f"[巡检] 取不到数据：{e}")
        return 1

    rows, tag, n_must, n_head, total = pick_items(data, args.lines)
    is_today = (date_str == today)
    xml = build_xml(date_str, rows, tag, n_must, n_head, total, is_today)

    if args.dry:
        print(xml)
        return 0

    register_app_id()
    show_toast(xml)

    stamp = now.strftime("%H:%M")
    for s in due:
        st.setdefault(f"{today}-{s}", stamp)
    save_state(st)
    print(f"[巡检] 补弹 {today} 的 {latest} 那份（数据 {date_str}，共 {total} 条）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="把当天新闻推到 Windows 桌面通知")
    ap.add_argument("--date", help="指定日期 YYYY-MM-DD，默认今天（没数据自动往前找）")
    ap.add_argument("--lines", type=int, default=3, help="通知里列几条（默认 3）")
    ap.add_argument("--auto", action="store_true",
                    help="巡检模式：该弹就弹，已弹过或还没到时间就静默退出（计划任务用）")
    ap.add_argument("--open", action="store_true", help="只打开网页看板，不弹通知")
    ap.add_argument("--test", action="store_true", help="弹一条测试通知，确认通道可用")
    ap.add_argument("--force", action="store_true", help="今天已经弹过也再弹一次")
    ap.add_argument("--dry", action="store_true", help="只打印内容，不弹窗")
    args = ap.parse_args()

    if args.open:
        webbrowser.open(SITE)
        print("已在浏览器打开：" + SITE)
        return 0

    if args.auto:
        return run_auto(args)

    if args.test:
        xml = ('<toast activationType="protocol" launch="%s" duration="long">'
               '<visual><binding template="ToastGeneric">'
               '<text>桌面推送测试</text>'
               '<text>能看到这条，说明通知通道已经通了</text>'
               '<text>以后每天 08:40 / 20:40 自动推</text>'
               '<text>点它会打开新闻看板</text>'
               '</binding></visual>'
               '<actions><action content="打开看板" activationType="protocol" '
               'arguments="%s"/></actions>'
               '<audio src="ms-winsoundevent:Notification.Default"/>'
               '</toast>') % (SITE, SITE)
        print("注册通知来源：", "成功" if register_app_id() else "失败（不影响弹出）")
        if args.dry:
            print(xml)
            return 0
        show_toast(xml)
        print("测试通知已发出，看一下屏幕右下角。")
        return 0

    if args.date:
        date_str, data = args.date, load_day(args.date)[0]
        src = "api"
    else:
        # 计划任务里多等几轮：云端可能刚好还在抓
        date_str, data = latest_day(tries=3, wait=100)

    rows, tag, n_must, n_head, total = pick_items(data, args.lines)
    is_today = (date_str == dt.date.today().strftime("%Y-%m-%d"))
    xml = build_xml(date_str, rows, tag, n_must, n_head, total, is_today)

    print(f"数据日期 {date_str}：共 {total} 条（必看 {n_must} / 要闻 {n_head}）")
    for i, it in enumerate(rows, 1):
        print(f"  {i}. [{tag[id(it)]}] {it.get('title', '')}")
    print(f"  看板：{SITE}")

    if args.dry:
        print("\n--- 通知 XML ---\n" + xml)
        return 0

    key = slot_key(date_str)
    st = load_state()
    if st.get(key) and not args.force:
        print(f"今天这个时段（{key}）已经弹过，跳过。要重弹加 --force。")
        return 0

    register_app_id()
    show_toast(xml)
    st[key] = dt.datetime.now().strftime("%H:%M")
    save_state(st)
    print("已弹出桌面通知。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[出错] {e}")
        sys.exit(1)
