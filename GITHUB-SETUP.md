# 部署到 GitHub（关机也能跑）

配好之后：GitHub 的服务器会自动帮你抓新闻、生成日报、更新网页、发邮件。
你的电脑开不开机都不影响。

**节奏是这样安排的：**

| 时间（北京） | 做什么 |
|---|---|
| 08:30 | 抓取 + **发早报邮件** |
| 12:30 | 只抓取，不发邮件 |
| 16:30 | 只抓取，不发邮件 |
| 20:30 | 抓取 + **发晚报邮件** |
| 00:30 | 只抓取，不发邮件 |

为什么抓 5 次却只发 2 封：新闻网站的 RSS 只保留最近几十条，
上午发生的重大新闻，到晚上就被挤出列表了（实测中新网只保留 30 条，约 5 小时就滚动完）。
多抓几次才能保证不漏，但没必要拿 5 封邮件打扰你——每次抓到的内容会累积到当天的日报里，
所以早上那封邮件里能看到前一天晚上到当天的全部内容，晚报则是完整的一天。

**你需要准备：** 一个 GitHub 账号（你已有）。

> 为什么必须上云：纽约时报、BBC、美联储这些源在国内网络访问不了（实测超时）。
> GitHub 的服务器在国外，能正常抓到，这是本地跑做不到的。

---

## 整体流程

```
建仓库 → 传代码 → 开启网页 → 试跑一次 → 完成
```

全程大约 15 分钟。

---

## 第一步：创建仓库

1. 打开 https://github.com/new
2. **Repository name** 填 `daily-news`（或你喜欢的名字）
3. 选择 **Public**（公开）

   > 为什么建议公开：公开仓库的 GitHub Actions 完全不消耗额度；
   > 私有仓库每月有 2000 分钟额度，这个程序每天跑不到 1 分钟，其实也够，
   > 但公开更省心。内容都是公开新闻，唯一会公开的是你的关键词配置。

4. **不要**勾选 "Add a README file"（我们本地已有文件）
5. 点 **Create repository**

创建后会看到一个空仓库页面，先放着。

---

## 第二步：上传代码

### 方式 A：GitHub Desktop（推荐，不用记命令）

1. 下载安装 https://desktop.github.com/
2. 打开后登录你的 GitHub 账号
3. 菜单 **File → Add local repository**
4. 选择这个文件夹：

   ```
   C:\Users\dengwen\WorkBuddy\2026-10-26-20-00-08\news-collector
   ```

5. 它会提示"这不是一个 Git 仓库"，点 **create a repository** 链接
6. 点 **Create repository**
7. 左下角填一句提交说明，比如 `初始化`，点 **Commit to main**
8. 点顶部的 **Publish repository**
   - Name 填 `daily-news`
   - **取消勾选** "Keep this code private"
   - 点 **Publish repository**

完成。

### 方式 B：命令行

打开 Git Bash（或任意终端），依次执行：

```bash
cd "C:/Users/dengwen/WorkBuddy/2026-10-26-20-00-08/news-collector"

git init
git add .
git commit -m "初始化"
git branch -M main
git remote add origin https://github.com/你的用户名/daily-news.git
git push -u origin main
```

> push 时会要求登录。GitHub 已不支持密码，需要用 **Personal Access Token**：
> 打开 https://github.com/settings/tokens → Generate new token (classic)
> → 勾选 `repo` → 生成后复制，粘贴到密码位置。
>
> 嫌麻烦就用方式 A。

---

## 第三步：开启网页

这一步不能漏，否则最后一步会报错。

1. 进入你的仓库页面
2. 点顶部的 **Settings**
3. 左侧菜单找到 **Pages**
4. **Source** 那里选择 **GitHub Actions**（不要选 "Deploy from a branch"）

保存即可，不用填别的东西。

---

## 第四步：试跑一次

1. 点仓库顶部的 **Actions** 标签
2. 左侧看到 **每日新闻**，点它
3. 右侧有 **Run workflow** 按钮，点开 → 再点绿色 **Run workflow**
4. 等大约 30 秒，刷新页面
5. 出现绿色对勾就成功了

**看结果：**

- 网页版：回到 Settings → Pages，顶部会显示你的网址，形如
  `https://你的用户名.github.io/daily-news/`
  手机电脑都能打开，也能分享给别人
- 仓库里：`reports/` 是 Markdown 日报，`docs/index.html` 是网页

**如果失败了：** 点进那次运行，展开红色的步骤看报错。
最常见的原因是第三步没做（Pages 没开）。

---

## 第五步：配置邮件（可选）

不配也能用，只是没有邮件推送。

1. 仓库 → **Settings** → 左侧 **Secrets and variables** → **Actions**
2. 点 **New repository secret**，依次添加这 4 个：

| Name | Value |
|---|---|
| `NEWS_MAIL_SENDER` | 你的发件邮箱，如 `xxx@qq.com` |
| `NEWS_MAIL_PASSWORD` | 邮箱**授权码**（不是登录密码） |
| `NEWS_MAIL_RECEIVER` | 收件邮箱，**多个用英文逗号分隔** |
| `NEWS_MAIL_HOST` | `smtp.qq.com` |

**手机和电脑同时收到**，就把两个邮箱都填进 `NEWS_MAIL_RECEIVER`：

```
手机号@qq.com,你的电脑邮箱@outlook.com
```

用逗号或分号分隔都行，程序会自动拆开。发一封邮件给所有地址，不额外收费。

**QQ 邮箱授权码怎么拿：** 登录 QQ 邮箱 → 设置 → 账户 →
找到「IMAP/SMTP 服务」→ 开启 → 按提示生成 16 位授权码。

用 Gmail 的话 `NEWS_MAIL_HOST` 填 `smtp.gmail.com`。

3. 到 `config.yaml` 里把 `email.enabled` 改成 `true`，提交一次

---

## 日常怎么用

**什么都不用做。** 每天自动跑 5 次（抓取），早晚各发一封邮件，网页自动更新。
早上那封标题是「新闻早报」，晚上是「新闻晚报」。

**想改关注的内容：**
在 GitHub 网页上打开 `config.yaml` → 点右上角铅笔图标 → 改 → 提交。
下次运行就生效，不用碰本地文件。

**想立刻跑一次：**
Actions → 每日新闻 → Run workflow

**想改运行时间：**
编辑 `.github/workflows/daily.yml` 里的 cron 行。

⚠️ 这里用的是 **UTC 时间**，换算：北京时间 = UTC + 8。

| 你想的时间（北京） | cron 写法 |
|---|---|
| 07:00 | `0 23 * * *` |
| 08:30（早报，发邮件） | `30 0 * * *` |
| 12:00 | `0 4 * * *` |
| 20:30（晚报，发邮件） | `30 12 * * *` |
| 21:00 | `0 13 * * *` |

**改发邮件的时间点：** 除了改 cron，还要同步改「抓取新闻」那一步里的两处判断：

```bash
if [ -z "$SCHEDULE" ] || [ "$SCHEDULE" = "30 0 * * *" ] || [ "$SCHEDULE" = "30 12 * * *" ]; then
```

把 `30 0 * * *` 和 `30 12 * * *` 换成你想发邮件的那个时间点。

**只想要一次？** 删掉多余的 cron 行，并把发邮件判断里只留一个时间点。

---

## 两种跑法怎么选

现在你有了两套方案，可以同时用，互不干扰：

| | 本地（双击 run-now.bat） | GitHub（云端） |
|---|---|---|
| 电脑关机 | 不跑 | 照跑 |
| 速度 | 快 | 稍慢 |
| 网页看板 | 有 | 有，且能手机看 |
| 适合 | 临时补抓 | 日常自动 |

**建议：** 本地那个定时任务可以卸载掉（双击 `uninstall.bat`），
只留 GitHub 云端跑，避免两边重复。

---

## 常见问题

**Q：会花 GitHub 的钱吗？**
不会。公开仓库的 Actions 完全免费，没有额度限制。
这个任务每天跑不到 1 分钟，就算私有仓库也只占 30 分钟/月（额度 2000 分钟）。

**Q：GitHub 会不会哪天停掉这个服务？**
Actions 和 Pages 都是 GitHub 的核心免费功能，已经稳定提供十几年。
即使真变了，你的代码和数据都在自己手里，随时能搬走。

**Q：云端抓不到国内的源怎么办？**
GitHub 的服务器在国外，个别国内站点可能拒绝海外访问。
程序会自动跳过失败的源、继续抓其他的，日报末尾会列出哪个源挂了。
如果发现某个源长期失败，把 `config.yaml` 里它的 `enabled` 改成 `false` 即可。

**Q：别人能看到我的网页吗？**
公开仓库的 Pages 网址任何人拿到都能打开。
如果不想公开，可以把仓库设为 Private，或在 Pages 设置里限制访问。

**Q：手机上怎么看？**
直接打开那个 `github.io` 网址，页面是响应式的，手机上显示正常。
可以「添加到主屏幕」，当个 App 用。

**Q：想停止怎么办？**
Actions → 每日新闻 → 右上角 `...` → Disable workflow。
或者直接删掉仓库。

---

## 部署后你的仓库长这样

```
daily-news/
├── .github/workflows/daily.yml   ← 定时任务配置
├── config.yaml                   ← 你的兴趣设置（可直接在网页上改）
├── collector.py                  ← 抓取程序
├── build_site.py                 ← 网页生成器
├── data/                         ← 每天的原始数据（JSON）
├── reports/                      ← 每天的日报（Markdown）
├── docs/index.html               ← 网页看板（自动更新）
└── README.md
```

`data/`、`reports/`、`docs/` 会每天自动新增内容，不需要你管。
