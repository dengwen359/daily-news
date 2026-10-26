#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装桌面通知：注册计划任务 + 在桌面放一个打开看板的快捷方式。

不采用"每天 08:40 准点弹一次"的写法，因为那样一旦当时没开机就永远错过了。
改成两层保险：
  1. 巡检任务：08:40 起每 30 分钟问一次"今天这份弹过没"，没弹就弹；
  2. 登录任务：每次登录后 2 分钟也问一次。夜里关机、第二天一早开机，也会补上。
两道都跑同一个 --auto 模式，靠 .state/notified.json 记录去重，不会重复打扰。

程序用 pythonw.exe 跑，不闪黑窗口。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "desktop_notify.py")
SITE = "https://dengwen359.github.io/daily-news/"

TASK_REPEAT = "每日新闻·桌面通知"
TASK_LOGON = "每日新闻·开机补弹"
LEGACY = ["每日新闻·桌面通知（早）", "每日新闻·桌面通知（晚）"]


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


def create(name, tr, extra):
    r = run(["schtasks", "/Create", "/TN", name, "/TR", tr, "/F"] + extra)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def tune(name):
    """开机后补跑、单次最多 10 分钟、用电池也照跑"""
    ps = (f"$s=(Get-ScheduledTask -TaskName '{name}') -ErrorAction SilentlyContinue;"
          f"if($s){{$s=$s.Settings;"
          f"$s.StartWhenAvailable=$true;"
          f"$s.ExecutionTimeLimit='PT10M';"
          f"$s.DisallowStartIfOnBatteries=$false;"
          f"$s.StopIfGoingOnBatteries=$false;"
          f"Set-ScheduledTask -TaskName '{name}' -Settings $s | Out-Null}}")
    run(["powershell", "-NoProfile", "-Command", ps])


def main():
    pyw = find_pythonw()
    if not pyw:
        print("× 没找到 pythonw.exe，请确认 Python 安装完整。")
        return 1
    print(f"使用解释器：{pyw}")

    tr = f'"{pyw}" "{SCRIPT}" --auto'

    # 清掉旧版本可能留下的准点任务，避免一天弹好几次
    for name in LEGACY:
        run(["schtasks", "/Delete", "/TN", name, "/F"])

    ok = 0

    # 1) 巡检：08:40 起每 30 分钟一次，持续一整天
    done, err = create(TASK_REPEAT, tr,
                       ["/SC", "DAILY", "/ST", "08:40", "/RI", "30", "/DU", "23:59"])
    if done:
        tune(TASK_REPEAT)
        print("√ 每 30 分钟巡检一次（08:40 起）：该弹才弹，弹过就安静退出")
        ok += 1
    else:
        print(f"· 30 分钟巡检装不上（{err[:80]}），改用两个准点任务")
        for name, hhmm in (("每日新闻·桌面通知（早）", "08:40"),
                           ("每日新闻·桌面通知（晚）", "20:40")):
            done, err = create(name, tr, ["/SC", "DAILY", "/ST", hhmm])
            if done:
                tune(name)
                print(f"√ 每天 {hhmm} 检查一次")
                ok += 1
            else:
                print(f"× {name} 创建失败：{err[:150]}")

    # 2) 登录补弹：关机错过的，一开机就补
    done, err = create(TASK_LOGON, tr, ["/SC", "ONLOGON", "/DELAY", "0002:00"])
    if done:
        tune(TASK_LOGON)
        print("√ 每次登录后 2 分钟再检查一次：关机错过的会补弹")
        ok += 1
    else:
        print(f"· 登录补弹没装上（{err[:80]}），只靠巡检也够用")

    # 桌面快捷方式：双击直接打开看板
    desk = os.path.join(os.path.expanduser("~"), "Desktop")
    if os.path.isdir(desk):
        url_path = os.path.join(desk, "今日新闻.url")
        with open(url_path, "w", encoding="ascii", newline="\r\n") as f:
            f.write("[InternetShortcut]\r\nURL=" + SITE + "\r\n")
        print(f"√ 桌面快捷方式：{url_path}")

    if ok:
        print("\n装好了。想立刻试一下，双击 run-notify.bat。")
        print("不管几点开机，只要当天那份还没收到，开机后都会自动补上。")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
