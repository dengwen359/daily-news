#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装桌面通知：注册两个每日计划任务 + 在桌面放一个打开看板的快捷方式。

以后每天 08:40、20:40 自动从云端取当天新闻，弹一条系统通知。
程序用 pythonw.exe 跑，不会闪黑窗口；电脑那会儿关机的话，开机后会补弹一次。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "desktop_notify.py")
SITE = "https://dengwen359.github.io/daily-news/"

TASKS = [
    ("每日新闻·桌面通知（早）", "08:40"),
    ("每日新闻·桌面通知（晚）", "20:40"),
]


def find_pythonw():
    cands = []
    exe = sys.executable or ""
    if exe.lower().endswith("python.exe"):
        cands.append(exe[:-len("python.exe")] + "pythonw.exe")
    cands += [
        os.path.join(os.path.dirname(exe), "pythonw.exe") if exe else "",
        r"C:\Users\dengwen\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    # 最后再问一次 PATH
    try:
        out = subprocess.run(["where", "pythonw"], capture_output=True, text=True,
                             timeout=20).stdout.strip().splitlines()
        for line in out:
            if line.strip() and os.path.isfile(line.strip()):
                return line.strip()
    except Exception:
        pass
    return ""


class _R:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def run(cmd, **kw):
    """跑外部命令；失败也不抛异常，交给调用方判断"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120, **kw)
    except Exception as e:
        return _R(1, "", str(e))


def main():
    pyw = find_pythonw()
    if not pyw:
        print("× 没找到 pythonw.exe，请确认 Python 安装完整。")
        return 1
    print(f"使用解释器：{pyw}")

    ok = 0
    for name, hhmm in TASKS:
        tr = f'"{pyw}" "{SCRIPT}"'
        r = run(["schtasks", "/Create", "/TN", name, "/TR", tr,
                 "/SC", "DAILY", "/ST", hhmm, "/F"])
        if r.returncode != 0:
            print(f"× {name} 创建失败：{(r.stderr or r.stdout).strip()[:200]}")
            continue
        # 错过时间（电脑关机）就开机后补跑，并限制单次最长 10 分钟
        ps = (f"$s=(Get-ScheduledTask -TaskName '{name}').Settings;"
              f"$s.StartWhenAvailable=$true;"
              f"$s.ExecutionTimeLimit='PT10M';"
              f"$s.DisallowStartIfOnBatteries=$false;"
              f"Set-ScheduledTask -TaskName '{name}' -Settings $s | Out-Null")
        run(["powershell", "-NoProfile", "-Command", ps])
        print(f"√ 每天 {hhmm} 自动弹通知（{name}）")
        ok += 1

    # 桌面快捷方式：双击直接打开看板
    desk = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(desk):
        url_path = os.path.join(desk, "今日新闻.url")
        with open(url_path, "w", encoding="ascii", newline="\r\n") as f:
            f.write("[InternetShortcut]\r\nURL=" + SITE + "\r\n")
        print(f"√ 桌面快捷方式：{url_path}")

    if ok:
        print("\n装好了。想立刻试一下，双击 run-notify.bat；")
        print("以后不用管，每天早晚各弹一次。")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
