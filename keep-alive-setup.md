# 让系统在营业时间不休眠（说明文档）

## 现在的做法（2026-09-28 更新：主力已经换成"系统自己保活"）

系统**自己**会在营业时段（北京时间 08:00-24:00）每 8 分钟请求一次自己的公网地址
（`/api/ping`）。这个请求对 Render 来说就是一次真实的**入站请求**，于是"15 分钟空闲就休眠"
的计时被不断重置 —— **不依赖 GitHub、不依赖手机、不需要任何第三方服务**。

代码在 `keepalive.py`，可用 `GET /api/keepalive/status` 查看状态：

- `uptime_hours`（连续运行小时数）**一直变大 = 这段时间从没休眠过**（休眠会重启进程、归零）。
  库存页底部那行"已连续运行 X 小时"就是它。
- `GET/POST /api/keepalive/config` 可改：`enabled`、`start_hour`、`end_hour`、`interval_min`。

> 为什么不再只依赖 GitHub Actions：GitHub 官方文档明确写着「`schedule` 事件在高负载时
> 可能被延迟，**最坏的情况下可能根本不运行**」，而 `*/5` 恰好全都落在最拥堵的整分钟上。
> 实测该工作流提交后近 2 小时**一次都没跑**。所以它降级为"备用"。

## 下面的 GitHub Actions 文件还有用吗？——有，当"早上叫醒服务"用

凌晨（00:00 之后）系统会正常休眠省额度。**早上 08:00 需要一个外部的请求把它叫醒**，
否则第一个打开系统的人要等 30-60 秒（页面会显示"系统启动中"，并先展示上次的清单，
所以其实也不耽误干活）。

---

## 为什么要你亲手做（当初建这个文件的原因）

我这个自动推送用的是你仓库里的一个 GitHub 令牌（Token），它有 `repo` 权限、
但**没有 `workflow` 权限**，所以 GitHub 禁止我创建 `.github/workflows/` 里的文件。
这是 GitHub 的安全限制，不是我能绕过的。

> ⚠️ 下面这条路**不用动你的令牌**（动令牌有风险：万一改坏，数据备份会失效）。
> 所以推荐用这条。

---

## 做法：在 GitHub 网页上新建一个文件

1. 打开 https://github.com/ll136897/kitchen-panel
2. 点页面上的 **Add file ▾** → **Create new file**
3. 在文件名框里**粘贴这一整行**（斜杠会自动变成文件夹）：

```
.github/workflows/keep-alive.yml
```

4. 把下面这一整段**全部复制**，粘到大输入框里（覆盖掉原来自动生成的空行）：

```yaml
name: keep-alive

on:
  schedule:
    - cron: "*/5 0-15 * * *"
  workflow_dispatch:

concurrency:
  group: keep-alive
  cancel-in-progress: false

jobs:
  ping:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - name: ping
        run: |
          URL="https://kitchen-panel.onrender.com/api/ping"
          for i in 1 2 3; do
            code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 100 "$URL" || echo 000)
            echo "try $i -> HTTP $code"
            if [ "$code" = "200" ]; then exit 0; fi
            sleep 10
          done
          echo "not reachable this round; next run will retry"
          exit 0
```

5. 拉到底点 **Commit changes**（提交到 main 分支即可）
6. 点仓库上方的 **Actions** 标签，左边应出现 **keep-alive** → 说明生效

---

## 请注意：时间只能这么设（关系到免费额度）

- `*/5 0-15 * * *` 的意思是「**每 5 分钟一次，只在 UTC 0 点到 15 点**」，
  换成北京时间就是 **08:00 - 23:59**，正好覆盖你们的营业时间。
- Render 免费额度是 **750 实例小时/月**，而一个月约 720-744 小时。
  - 只保活 16 小时/天 → 约 **480 小时/月**，安全（留了 270 小时余量）。
  - ⚠️ 如果改成全天 24 小时保活 → 约 730 小时/月，**几乎把额度吃光**；
    一旦用满，Render 会把这个月剩下的时间**全部停掉**，反而更糟。所以**不要改**。

---

## 另一条路（可选，需要动令牌）

如果你希望由我以后**自动维护**这个定时任务，可以给令牌加上 `workflow` 权限：

1. GitHub 右上角头像 → **Settings**
2. 左侧拉到底 → **Developer settings**
3. → **Personal access tokens** → **Tokens (classic)**
4. 找到列表里那个令牌（名字通常是你当初建备份时起的），点进去
5. 勾上 **workflow** → 点 **Update token**
6. 弄完跟我说一声，我直接把任务推上去并手动触发一次验证

> 提醒：这条路要动令牌，**万一改坏会导致数据库备份失效**，所以我更推荐上面那条。
