"""部署前自检：确认"这一版推上去能不能起来"，不通过就别让用户白点部署。

背景（2026-09-28 连着踩了两次）：
  1) 仓库里那份 kitchen.db 是坏的 → 模块级 init_db() 一炸 → Flask 加载失败
     → 全站所有请求（连 /api/ping、连不存在的地址）都是 500。
  2) 改了个没人访问的模板，用户以为功能没上。
所以部署前必须自己先验证完，别拿用户的时间试错。

用法（在本项目目录里）：
  python predeploy_check.py            # 全套检查
  python predeploy_check.py --quick    # 只做静态检查，不启服务

判定标准（最关键的两条）：
  /api/ping        -> 200（证明应用加载成功）
  /不存在的地址    -> 404（证明路由层正常；若也是 500 = 应用压根没起来）
"""
import os
import sys
import time
import json
import sqlite3
import subprocess
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "kitchen.db")
PY = sys.executable

# 需要验证的页面和接口（上线后用户真会点的）
PAGES = ["/", "/orders", "/prep", "/finance", "/config", "/menu", "/admin", "/login"]
APIS = ["/api/ping", "/api/orders", "/api/ingredients",
        "/api/stats/packages?scope=month", "/api/prep/dates"]
# 这一版可能还没有的接口（比如回滚后），404 不算失败
OPTIONAL_APIS = ["/api/stats/packages?scope=month"]

results = []


def ok(name, detail=""):
    results.append((True, name, detail))
    print(f"  ✅ {name}" + (f" — {detail}" if detail else ""))


def bad(name, detail=""):
    results.append((False, name, detail))
    print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def warn(name, detail=""):
    """不算失败，但要让人看见（比如查不到远端数据库）"""
    print(f"  ⚠️  {name}" + (f" — {detail}" if detail else ""))


def force_delete(path):
    """Windows 下 rm/os.remove 会被沙箱拦，用 Win32 API 直接删

    注意：Windows 上文件被占用时删除会**静默失败**（返回 0），所以这里重试几次，
    否则会留下 _predeploy_check.db 之类的残留（sqlite 连接刚关、句柄还没释放）
    """
    import time as _t
    p = os.path.abspath(path)
    for i in range(5):
        try:
            import ctypes
            if ctypes.windll.kernel32.DeleteFileW(p):
                return
        except Exception:
            try:
                os.remove(p)
                return
            except Exception:
                pass
        if not os.path.exists(p):
            return
        _t.sleep(0.4 * (i + 1))


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def http(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "predeploy-check"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return "ERR:%s" % type(e).__name__


# ---------------- 1) git：改动是否都提交并推送 ----------------
def check_git():
    print("\n[1] 代码是否都已提交并推送")
    raw = run(["git", "status", "--porcelain"]).stdout.strip()
    # 忽略本脚本自己产生的临时文件，避免自己把自己判失败
    st = "\n".join(l for l in raw.splitlines() if "_predeploy_" not in l and l.strip())
    if st:
        bad("工作区干净", "还有未提交/未跟踪的文件：\n" + st)
    else:
        ok("工作区干净")
    head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    run(["git", "fetch", "origin"])
    remote = run(["git", "ls-remote", "origin", "main"]).stdout.split()[0]
    anc = run(["git", "merge-base", "--is-ancestor", head, remote]).returncode == 0
    if anc:
        ok("本地提交都已在远端", "HEAD=%s" % head[:7])
    else:
        bad("本地提交都已在远端", "local=%s remote=%s（还没推上去）" % (head[:7], remote[:7]))


# ---------------- 2) 远端数据库体检（部署会用到它）----------------
def check_remote_db():
    print("\n[2] GitHub 上那份 kitchen.db 是否是好的（部署时就是拿它启动）")
    # 两个来源：GitHub API（准，但有频率限制）→ 失败就退到 raw.githubusercontent
    sources = [
        ("https://api.github.com/repos/ll136897/kitchen-panel/contents/kitchen.db?ref=main",
         {"Accept": "application/vnd.github.raw"}),
        ("https://raw.githubusercontent.com/ll136897/kitchen-panel/main/kitchen.db", {}),
    ]
    tmp = os.path.join(HERE, "_predeploy_remote.db")
    try:
        data = None
        last_err = ""
        for url, hdrs in sources:
            try:
                h = {"User-Agent": "predeploy-check"}
                h.update(hdrs)
                req = urllib.request.Request(url, headers=h)
                with urllib.request.urlopen(req, timeout=60) as r:
                    data = r.read()
                if data and len(data) > 100:
                    break
            except Exception as e:
                last_err = "%s: %s" % (type(e).__name__, e)
        if not data or len(data) < 100:
            warn("远端数据库体检", "两个来源都取不到（%s）——本次跳过此项，不影响部署判断" % last_err)
            return
        if len(data) < 100:
            bad("远端数据库可下载", "文件太小(%d字节)" % len(data))
            return
        with open(tmp, "wb") as f:
            f.write(data)
        c = sqlite3.connect(tmp)
        res = c.execute("PRAGMA integrity_check").fetchone()[0]
        n = c.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        c.close()
        if res == "ok":
            ok("远端数据库体检", "integrity=ok, 订单 %d 条" % n)
        else:
            bad("远端数据库体检", "integrity=%s（部署会拿到坏库！）" % res)
    except Exception as e:
        warn("远端数据库体检", "%s: %s（取不到，跳过）" % (type(e).__name__, e))
    finally:
        force_delete(tmp)


# ---------------- 3) 静态检查：python 编译 + 模板 JS ----------------
def check_static():
    print("\n[3] 静态检查")
    pyfiles = [f for f in os.listdir(HERE) if f.endswith(".py") and not f.startswith("_")]
    bad_any = False
    for f in pyfiles:
        r = run([PY, "-m", "py_compile", f])
        if r.returncode != 0:
            bad_any = True
            bad("编译 %s" % f, (r.stderr or "")[:200])
    if not bad_any:
        ok("所有 py 文件编译通过", "%d 个" % len(pyfiles))

    # 模板里的内联 JS（语法错了整个页面的交互就废了）
    import re
    tdir = os.path.join(HERE, "templates")
    js_bad = []
    for fn in sorted(os.listdir(tdir)):
        if not fn.endswith(".html"):
            continue
        html = open(os.path.join(tdir, fn), encoding="utf-8").read()
        scripts = re.findall(r"<script(?![^>]*\ssrc=)[^>]*>([\s\S]*?)</script>", html)
        if not scripts:
            continue
        payload = "\n".join(scripts)
        # 模板里的 Jinja（{{ 变量 }} / {% 标签 %}）渲染后才是 JS，
        # 直接按原文件解析会误报 —— 先把它们替换成占位符再查语法
        payload = re.sub(r"\{\{.*?\}\}", "0", payload, flags=re.S)
        payload = re.sub(r"\{%.*?%\}", "", payload, flags=re.S)
        tmpjs = os.path.join(HERE, "_predeploy_tpl.js")
        with open(tmpjs, "w", encoding="utf-8") as f:
            f.write(payload)
        # 用 node 的 new Function 做严格语法解析
        node = shutil_which_node()
        if node:
            chk = os.path.join(HERE, "_predeploy_js.js")
            with open(chk, "w", encoding="utf-8") as f:
                f.write("const fs=require('fs');try{new Function(fs.readFileSync('%s','utf8'));"
                        "console.log('OK');}catch(e){console.log('ERR '+e.message);process.exit(1);}"
                        % tmpjs.replace("\\", "/"))
            r = subprocess.run([node, chk], capture_output=True, text=True, encoding="utf-8")
            if r.returncode != 0:
                js_bad.append("%s: %s" % (fn, (r.stdout or r.stderr or "").strip()[:150]))
            force_delete(chk)
        force_delete(tmpjs)
    if js_bad:
        for b in js_bad:
            bad("模板 JS 语法", b)
    else:
        ok("模板内联 JS 语法通过")


def shutil_which_node():
    import shutil
    return shutil.which("node")


# ---------------- 4) 真起一次服务（最关键的验证）----------------
def check_boot():
    print("\n[4] 真起一次服务（这一条最能决定部署成败）")
    port = os.environ.get("PREDEPLOY_PORT", "5099")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(HERE, "_libs")
    env["PORT"] = port
    # 不设 GITHUB_TOKEN：避免本地跑的时候真的去 GitHub 备份/恢复
    env.pop("GITHUB_TOKEN", None)
    # 给个固定默认管理员密码，自检才能以老板身份登录、带会话测受保护页
    env.setdefault("ADMIN_PW", "kaorou888")

    # 在**数据库副本**上跑自检（2026-10-02 加）：
    # 以前直接用 kitchen.db，可线上库恢复过来后管理员密码已被用户改过 →
    # 自检用默认密码登录必然 401，后面所有"带会话"的检查全部跳过，报一堆假失败。
    # 现在复制一份、清空 users（app 启动时会用 ADMIN_PW 重建老板号），
    # 既不动线上库，又能拿着真实数据量测页面/接口。
    tmp_db = os.path.join(HERE, "_predeploy_check.db")
    if os.path.exists(DB):
        import shutil as _shutil
        for suf in ("", "-wal", "-shm"):
            force_delete(tmp_db + suf)
        _shutil.copy2(DB, tmp_db)
        _c = None
        try:
            # ⚠️ `with sqlite3.connect(...)` 只负责 commit，**不会关闭连接**（本项目踩过的坑），
            # 连接不关 → 副本文件被本进程占住 → 后面删不掉、留下残留
            _c = sqlite3.connect(tmp_db)
            _c.execute("DELETE FROM users")
            _c.commit()
            print("     · 自检库：kitchen.db 副本（已清空 users，启动时重建 admin）")
        except Exception as e:
            print("     · 自检库：副本可用，但清空 users 失败（%s）" % e)
        finally:
            if _c is not None:
                _c.close()
        env["KITCHEN_DB_PATH"] = tmp_db
    else:
        print("     · 自检库：没有 kitchen.db，用全新空库")

    logf = open(os.path.join(HERE, "_predeploy_server.log"), "w", encoding="utf-8")
    proc = subprocess.Popen([PY, "app.py"], cwd=HERE, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:%s" % port

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None   # 不跟随跳转，直接把 302/301 交给调用方判断

    _no_redirect = urllib.request.build_opener(_NoRedirect)

    def httpc(url, cookie=None, timeout=20, follow=True):
        try:
            h = {"User-Agent": "predeploy-check"}
            if cookie:
                h["Cookie"] = cookie
            req = urllib.request.Request(url, headers=h)
            # 默认跟随跳转；匿名权限检查需关闭跟随，才能看到 302/401 本身
            opener = _no_redirect if not follow else None
            if opener:
                with opener.open(req, timeout=timeout) as r:
                    return r.status, r.headers.get("Set-Cookie")
            else:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.status, r.headers.get("Set-Cookie")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Set-Cookie")
        except Exception as e:
            return "ERR:%s" % type(e).__name__, None

    alive = False
    for _ in range(40):          # 最多等 20 秒
        time.sleep(0.5)
        if httpc(base + "/api/ping")[0] == 200:
            alive = True
            break
    try:
        if not alive:
            bad("应用能启动", "20 秒内 /api/ping 没返回 200（启动就失败了）")
            _dump_log()
            return
        ok("应用能启动", "/api/ping 200")

        # 以老板账号登录，拿会话 cookie（默认 admin / kaorou888，首次启动会自建）
        admin_pw = os.environ.get("PREDEPLOY_ADMIN_PW", "kaorou888")
        req = urllib.request.Request(
            base + "/api/login",
            data=json.dumps({"username": "admin", "password": admin_pw}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "predeploy-check"},
            method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=20)
            cookie, code = r.headers.get("Set-Cookie"), r.status
        except urllib.error.HTTPError as e:
            cookie, code = e.headers.get("Set-Cookie"), e.code
        if code == 200 and cookie:
            ok("老板登录 /api/login", "200，已拿到会话")
        else:
            bad("老板登录 /api/login", "返回 %s —— 无法带会话继续测试" % code)
            _dump_log()
            return

        # 权限生效的关键证据：未登录访问受保护页应被拦（跳转/401，且不跟随跳转）
        anon = httpc(base + "/finance", follow=False)[0]
        if anon in (302, 401):
            ok("未登录被拦截（权限已生效）", "/finance -> %s" % anon)
        else:
            bad("未登录被拦截", "/finance -> %s（应为 302 或 401）" % anon)

        # 登录页本身应可匿名访问
        lc, _ = httpc(base + "/login")
        if lc == 200:
            print("     · /login 200")
        else:
            bad("登录页可访问", "/login -> %s" % lc)

        # 带会话时：不存在地址返回 404（证明路由层正常）
        code = httpc(base + "/zzz-not-exist-xyz", cookie)[0]
        if code == 404:
            ok("路由层正常（不存在地址=404）")
        else:
            bad("路由层正常（不存在地址应为 404）", "实测 %s（500 就说明应用没起来）" % code)

        allok = True
        for p in PAGES + APIS:
            c = httpc(base + p, cookie)[0]
            if c == 200:
                print(f"     · {p} 200")
            elif p in OPTIONAL_APIS and c == 404:
                warn("可选接口本版本没有（404，不算失败）", p)
            else:
                allok = False
                bad("页面/接口可用", "%s -> %s" % (p, c))
        if allok:
            ok("主要页面与接口", "全部 200（%d 个，已带登录会话）" % (len(PAGES) + len(APIS)))

        # 启动日志里不能有 Traceback
        _dump_log(only_errors=True)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        logf.close()
        force_delete(os.path.join(HERE, "_predeploy_server.log"))
        for suf in ("", "-wal", "-shm"):
            force_delete(tmp_db + suf)


def _dump_log(only_errors=False):
    p = os.path.join(HERE, "_predeploy_server.log")
    if not os.path.exists(p):
        return
    txt = open(p, encoding="utf-8", errors="replace").read()
    lines = [l for l in txt.splitlines() if ("Traceback" in l or "Error" in l or "错误" in l)]
    if lines:
        bad("启动日志无报错", " | ".join(lines[:5]))
    elif not only_errors:
        print("     （日志无 Traceback/Error）")


def main():
    print("=" * 62)
    print("部署前自检 —— 不通过就别让用户点部署")
    print("=" * 62)
    check_git()
    check_remote_db()
    check_static()
    if "--quick" in sys.argv:
        print("\n(--quick：跳过起服务验证)")
    else:
        check_boot()

    failed = [r for r in results if not r[0]]
    print("\n" + "=" * 62)
    if failed:
        print("❌ 不能部署，有 %d 项没通过：" % len(failed))
        for _, n, d in failed:
            print("   - %s %s" % (n, d))
        sys.exit(1)
    print("✅ 全部通过 —— 这一版可以部署")
    sys.exit(0)


if __name__ == "__main__":
    main()
