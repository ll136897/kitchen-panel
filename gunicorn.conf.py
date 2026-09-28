"""gunicorn 配置（gunicorn 会自动加载当前目录下的 gunicorn.conf.py，无需改启动命令）。

两个作用：
1. post_fork 钩子：线程不会被 fork 继承，worker 里要重建保活线程。
2. **worker 启动即向 GitHub 报平安**（2026-09-28 加）：
   线上出 500 时，Render 日志只有用户能看；把"worker 是否启动成功 / app 能否 import"
   写回仓库的 boot_error.txt，我这边拉一下就知道，不用再麻烦用户。
   如果 worker 在崩溃循环，这个文件的更新频率会直接暴露它。
"""

_REPO_API = "https://api.github.com/repos/ll136897/kitchen-panel/contents/boot_error.txt"


def _report(tag, text):
    """最小化上报：不依赖 app，应用坏了也能用"""
    import os, base64, json, urllib.request
    tok = os.environ.get("GITHUB_TOKEN", "")
    if not tok:
        print("[report] 无 GITHUB_TOKEN，跳过上报", flush=True)
        return
    try:
        url = _REPO_API
        sha = None
        try:
            rq = urllib.request.Request(url, headers={"Authorization": "token " + tok,
                                                      "User-Agent": "kitchen-panel"})
            with urllib.request.urlopen(rq, timeout=15) as r:
                sha = json.loads(r.read().decode()).get("sha")
        except Exception:
            pass
        data = {"message": "auto: worker boot report", "branch": "main",
                "content": base64.b64encode(text.encode("utf-8")).decode()}
        if sha:
            data["sha"] = sha
        rq = urllib.request.Request(url, data=json.dumps(data).encode(), method="PUT",
                                    headers={"Authorization": "token " + tok,
                                             "Accept": "application/vnd.github.v3+json",
                                             "User-Agent": "kitchen-panel"})
        urllib.request.urlopen(rq, timeout=25).read()
        print("[report] 已上报：%s" % tag, flush=True)
    except Exception as e:
        print("[report] 上报失败：%r" % (e,), flush=True)


def post_fork(server, worker):
    try:
        from keepalive import reset_after_fork, start_self_keepalive
        reset_after_fork()
        start_self_keepalive()
        print("[gunicorn] worker pid=%s 已重置并启动保活线程" % worker.pid, flush=True)
    except Exception as e:          # 保活失败不能影响正常服务
        print("[gunicorn] 启动保活线程失败：%s" % e, flush=True)

    # 主动 import 一次 app：这里就是 gunicorn 真正加载应用的地方，
    # 如果 import 炸了，报错会原样传回 GitHub —— 不用用户翻日志。
    import traceback
    try:
        import app as _app
        _msg = "worker(pid=%s) 启动成功，app import OK" % worker.pid
        print("[gunicorn] %s" % _msg, flush=True)
        _report("worker-boot OK", _msg)
    except Exception:
        _tb = traceback.format_exc()
        print("[gunicorn] ⚠️ app 导入失败！", flush=True)
        print(_tb, flush=True)
        _report("worker-boot FAILED: app import error", _tb)
