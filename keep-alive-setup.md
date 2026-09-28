# 让系统在营业时间不休眠（照这个做，2 分钟）

## 为什么要做这件事

Render 免费实例**15 分钟收不到任何请求就会休眠**，下次访问要等 **30-60 秒**才醒。
后厨备餐时点一下别的套餐就得干等，关键时刻用不上。

解决办法：用 GitHub 的定时任务，**每 5 分钟自动戳一次我们的服务**，让它一直醒着。
这个任务跑在 GitHub 的服务器上，**不依赖任何人的手机或电脑开着**。

## 为什么需要你亲手做

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
