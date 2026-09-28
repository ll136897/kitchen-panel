"""gunicorn 配置（gunicorn 会自动加载当前目录下的 gunicorn.conf.py，无需改启动命令）。

只加一个 post_fork 钩子，解决一个很隐蔽的问题：

**线程不会被 fork 继承**（子进程里只剩主线程），而 gunicorn 的 worker 就是 fork 出来的。
如果保活线程在 fork 之前就已经在父进程里启动了，worker 里就**没有**这个线程，
而"已启动"的标记却跟着一起被复制过来 → worker 永远不会再有心跳线程。

线上实测（2026-09-28）：进程连续运行 3.25 小时，`/api/keepalive/status` 里
`count=1`（只有启动那一刻成功心跳过一次），就是被 fork 掉了。

在 post_fork 里重置标记并重新起线程，保证**每个 worker 都有自己的保活线程**。
`app.py` 里的请求钩子（`keepalive.ensure_thread()`）还会再兜一层：线程死了就自动补回来。
"""


def post_fork(server, worker):
    try:
        from keepalive import reset_after_fork, start_self_keepalive
        reset_after_fork()
        start_self_keepalive()
        print("[gunicorn] worker pid=%s 已重置并启动保活线程" % worker.pid, flush=True)
    except Exception as e:          # 保活失败不能影响正常服务
        print("[gunicorn] 启动保活线程失败：%s" % e, flush=True)
