#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""头歌 (Educoder) 实训自动通关辅助 CLI。

原理
----
1. `start` 用一个**独立 profile** 的 Chrome/Edge 以 `--remote-debugging-port` 方式启动；
2. 之后每个子命令都通过 Playwright `connect_over_cdp` 连接到同一个浏览器实例。

因此"微信扫码登录"只需要做一次，登录态保存在 ~/.educoder-skill/chrome-profile，
后续所有命令复用同一份 cookie / session。

子命令
------
start / open / wait-login / read / start-env / run / press / click / next
wait-text / shot / eval / status / stop

所有查询类命令输出 JSON（UTF-8），方便上层 Agent 解析。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

STATE_DIR = Path(os.environ.get("EDC_STATE_DIR", str(Path.home() / ".educoder-skill")))
PROFILE_DIR = STATE_DIR / "chrome-profile"
TASK_STATE_FILE = STATE_DIR / "last_task.json"
DEFAULT_PORT = int(os.environ.get("EDC_CDP_PORT", "9222"))

ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"      # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\-_]"                  # 其他两字节转义
    r"|\x0d"                           # CR
)

TASK_KW_RE = re.compile(
    r"(task|quest|desc|problem|require|instruct|markdown|detail|left|mission|"
    r"step|guanka|exercise|exam|content)",
    re.I,
)

# ----------------------------------------------------------------------------
# 注入到页面的钩子：记录 WebSocket 下行数据（终端输出兜底来源）
# ----------------------------------------------------------------------------
HOOK_JS = r"""
(() => {
  if (window.__edcHook) return;
  window.__edcHook = 1;
  window.__edcLog = window.__edcLog || [];
  function push(x) {
    try {
      window.__edcLog.push(String(x));
      if (window.__edcLog.length > 4000) window.__edcLog.splice(0, 2000);
    } catch (e) {}
  }
  try {
    const OWS = window.WebSocket;
    if (OWS && !OWS.__edcWrapped) {
      function WS(url, protocols) {
        const ws = (protocols === undefined) ? new OWS(url) : new OWS(url, protocols);
        try {
          if (!/webpack|hot-update|sockjs|\/devtools\//i.test(String(url))) {
            push('[WS-OPEN] ' + url);
            ws.addEventListener('message', (ev) => {
              if (typeof ev.data === 'string') push(ev.data);
              else if (ev.data instanceof ArrayBuffer) push(new TextDecoder('utf-8').decode(ev.data));
            });
          }
        } catch (e) {}
        return ws;
      }
      WS.prototype = OWS.prototype;
      ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach((k) => { WS[k] = OWS[k]; });
      WS.__edcWrapped = 1;
      window.WebSocket = WS;
    }
  } catch (e) {}
})();
"""

# ----------------------------------------------------------------------------
# 读取页面快照：终端文本 + 左侧任务候选块 + 可见按钮 + 正文
# ----------------------------------------------------------------------------
READ_JS = r"""
() => {
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ');
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
  };
  const res = { url: location.href, title: document.title, term: '', termSrc: '',
                log: [], body: '', tasks: [], results: [], buttons: [] };

  // 1) 终端文本：xterm DOM 渲染器 -> .xterm-rows 的每一行
  const rowsBox = document.querySelector('.xterm-rows');
  if (rowsBox) {
    const rows = Array.from(rowsBox.children)
      .map((d) => clean(d.innerText).replace(/\s+$/, ''));
    const joined = rows.join('\n').replace(/^\n+|\n+$/g, '');
    if (joined.trim()) { res.term = joined; res.termSrc = 'dom-rows'; }
  }
  if (!res.term) {
    const box = document.querySelector('.xterm-screen, .xterm');
    if (box && box.innerText && box.innerText.trim()) {
      res.term = box.innerText.trim(); res.termSrc = 'dom-text';
    }
  }

  try { res.log = (window.__edcLog || []).slice(-1500); } catch (e) {}
  if (document.body) res.body = clean(document.body.innerText).slice(0, 30000);

  // 2) 左侧任务描述候选块
  const kw = /(task|quest|desc|problem|require|instruct|markdown|detail|left|mission|step|guanka|exercise|exam|content)/i;
  const seen = new Set();
  let cands = [];
  document.querySelectorAll('div,section,article,main,td,li').forEach((el) => {
    if (el.closest('.xterm') || el.querySelector('.xterm')) return;
    const name = ((el.id || '') + ' ' +
      (typeof el.className === 'string' ? el.className : '')).trim();
    if (!name || !kw.test(name)) return;
    if (!vis(el)) return;
    const t = clean(el.innerText).trim();
    if (t.length < 40) return;
    const key = t.slice(0, 80);
    if (seen.has(key)) return;
    seen.add(key);
    cands.push({ cls: name.slice(0, 100), len: t.length, text: t.slice(0, 3500) });
  });
  cands.sort((a, b) => b.len - a.len);
  // 去掉被更长候选包含的嵌套块
  const picked = [];
  for (const c of cands) {
    if (picked.some((p) => p.text.indexOf(c.text.slice(0, 200)) >= 0)) continue;
    picked.push(c);
    if (picked.length >= 2) break;
  }
  res.tasks = picked;

  // 2.1) 评测结果区域：优先按“测试结果/全部通过”等文本特征抓取最小块
  const rblocks = [];
  const rkw = /(全部通过|未通过|闯关成功|测试失败|测试通过|通过测试|恭喜)/;
  document.querySelectorAll('div,section,article').forEach((el) => {
    if (el.closest('.xterm')) return;
    const t = clean(el.innerText || '').trim();
    if (t.length < 4 || t.length > 1200) return;
    if (!rkw.test(t)) return;
    const cls = ((el.id || '') + ' ' +
      (typeof el.className === 'string' ? el.className : '')).trim();
    rblocks.push({ len: t.length, cls: cls.slice(0, 80), text: t });
  });
  rblocks.sort((a, b) => a.len - b.len);
  const rseen = new Set();
  for (const b of rblocks) {
    if (rseen.has(b.text)) continue;
    rseen.add(b.text);
    res.results.push({ cls: b.cls, text: b.text });
    if (res.results.length >= 2) break;
  }

  // 3) 可见按钮
  const labels = ['启动环境', '下一关', '重新开始', '评测', '提交', '测试', '保存',
                  '继续', '进入环境', '开始实训', '完成', '重置'];
  const seenBtn = new Set();
  document.querySelectorAll('button,a,div[role=button],span').forEach((el) => {
    const t = clean(el.innerText).trim();
    if (!t || t.length > 10 || labels.indexOf(t) < 0) return;
    if (!vis(el)) return;
    if (seenBtn.has(t)) return;
    seenBtn.add(t);
    const r = el.getBoundingClientRect();
    res.buttons.push({ text: t, x: Math.round(r.x), y: Math.round(r.y) });
  });
  return res;
}
"""

KEY_ALIAS = {
    "enter": "Enter",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "ctrl+c": "Control+c",
    "ctrl+l": "Control+l",
    "ctrl+d": "Control+d",
    "ctrl+u": "Control+u",
    "ctrl+z": "Control+z",
}


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------
def emit(obj, code: int = 0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def log(msg: str):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def save_task_url(url: str):
    """记录当前任务页地址，供登录后被重定向到首页时重新进入。"""
    if "educoder.net/tasks/" not in (url or ""):
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        TASK_STATE_FILE.write_text(
            json.dumps({"url": url, "at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def load_task_url():
    try:
        d = json.loads(TASK_STATE_FILE.read_text(encoding="utf-8"))
        return d.get("url") or None
    except Exception:
        return None


def cdp_alive(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.5) as r:
            return b"webSocketDebuggerUrl" in r.read()
    except Exception:
        return False


def find_browser() -> str | None:
    env = os.environ.get("EDC_BROWSER")
    if env and Path(env).exists():
        return env
    cands: list[Path] = []
    if sys.platform.startswith("win"):
        for base in (os.environ.get("PROGRAMFILES"),
                     os.environ.get("PROGRAMFILES(X86)"),
                     os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            cands += [
                Path(base) / "Google/Chrome/Application/chrome.exe",
                Path(base) / "Microsoft/Edge/Application/msedge.exe",
            ]
    elif sys.platform == "darwin":
        cands = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    else:
        cands = [Path(p) for p in (
            "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium", "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
        )]
    for c in cands:
        if c.exists():
            return str(c)
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def log_to_text(entries) -> str:
    """把 WebSocket 日志还原成终端文本（兜底方案）。"""
    chunks = []
    for raw in entries:
        if not isinstance(raw, str):
            continue
        s = raw
        if s.startswith("[WS-OPEN]"):
            continue
        if raw.startswith("{") and raw.rstrip().endswith("}"):
            try:
                obj = json.loads(raw)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                s = ""
                for k in ("data", "output", "stdout", "msg", "message",
                          "content", "text", "o"):
                    v = obj.get(k)
                    if isinstance(v, str):
                        s = v
                        break
                if not s:
                    continue
        chunks.append(s)
    text = "".join(chunks).replace("\x00", "")
    return ANSI_RE.sub("", text)


def tail_lines(text: str, n: int) -> str:
    lines = (text or "").splitlines()
    return "\n".join(lines[-n:]) if n and n > 0 else (text or "")


def guess_result(lines):
    for line in reversed(lines):
        if re.search(r"(未通过|不通过|测试失败|评测失败|\bFAIL\b|AssertionError|Traceback|"
                     r"SyntaxError|ModuleNotFoundError|Command not found)", line):
            return {"result": "fail", "line": line.strip()[:240]}
        if re.search(r"(测试通过|通过测试|闯关成功|恭喜|全部通过|\bPASS\b|Accepted|评测通过)", line, re.I):
            return {"result": "pass", "line": line.strip()[:240]}
    return {"result": "unknown", "line": None}


def parse_level(text: str) -> dict:
    total = None
    cur = None
    m = re.search(r"共\s*(\d+)\s*关", text or "")
    if m:
        total = int(m.group(1))
    m2 = re.search(r"第\s*(\d+)\s*关", text or "")
    if m2:
        cur = int(m2.group(1))
    return {"current": cur, "total": total}


# ----------------------------------------------------------------------------
# 浏览器会话
# ----------------------------------------------------------------------------
def attach_page(p):
    try:
        p.on("dialog", lambda d: d.accept())
    except Exception:
        pass
    try:
        p.evaluate(HOOK_JS)
    except Exception:
        pass


@contextmanager
def browser_session(args):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        emit({"ok": False, "error": "缺少 playwright，请在 py310 环境执行: "
                                    "pip install -r scripts/requirements.txt"})
    port = getattr(args, "port", DEFAULT_PORT)
    if not cdp_alive(port):
        emit({"ok": False, "error": f"未检测到调试端口 {port}，请先执行: python scripts/edc.py start"})

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=20000)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        try:
            ctx.add_init_script(HOOK_JS)
        except Exception:
            pass
        for p in ctx.pages:
            attach_page(p)
        try:
            ctx.on("page", attach_page)
        except Exception:
            pass
        yield ctx
    except SystemExit:
        raise
    except Exception as e:
        emit({"ok": False, "error": f"连接浏览器失败: {e}"})
    finally:
        try:
            pw.stop()
        except Exception:
            pass


def live_pages(ctx):
    out = []
    for p in ctx.pages:
        try:
            if p.url and not p.url.startswith("about:"):
                out.append(p)
        except Exception:
            continue
    return out


def resolve_page(ctx, hint: str | None = None):
    pages = live_pages(ctx)
    if hint:
        for p in pages:
            if hint in p.url:
                return p
    # 最近一次 open/next 记录的关卡页优先（忽略 query）
    target = load_task_url()
    if target:
        tpath = target.split("?")[0].rstrip("/")
        for p in reversed(pages):
            if p.url.split("?")[0].rstrip("/") == tpath:
                return p
    for p in pages:
        for f in p.frames:
            try:
                if f.locator(".xterm").count() > 0:
                    return p
            except Exception:
                continue
    # 关卡页优先于课堂/作业列表页
    for p in reversed(pages):
        if "/tasks/" in p.url:
            return p
    for p in pages:
        if "educoder.net" in p.url:
            return p
    return pages[-1] if pages else ctx.new_page()


def collect(ctx):
    """遍历所有页面/iframe，汇总最新快照。"""
    infos = []
    for p in live_pages(ctx):
        info = {"page": p, "url": p.url, "title": "", "term": "", "termSrc": "",
                "log": [], "body": "", "tasks": [], "results": [], "buttons": []}
        for f in p.frames:
            try:
                d = f.evaluate(READ_JS)
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            if not info["title"]:
                info["title"] = d.get("title") or ""
            if len(d.get("term") or "") > len(info["term"]):
                info["term"] = d.get("term") or ""
                info["termSrc"] = d.get("termSrc") or ""
                info["log"] = d.get("log") or []
            if len(d.get("body") or "") > len(info["body"]):
                info["body"] = d.get("body") or ""
            if len(d.get("tasks") or []) > len(info["tasks"]):
                info["tasks"] = d.get("tasks") or []
            for r in (d.get("results") or []):
                if r["text"] not in [x["text"] for x in info["results"]]:
                    info["results"].append(r)
            for b in (d.get("buttons") or []):
                if b["text"] not in [x["text"] for x in info["buttons"]]:
                    info["buttons"].append(b)
        infos.append(info)

    if not infos:
        return None, []

    pri = None
    for i in infos:
        if i["term"]:
            pri = i
            break
    if pri is None:
        # 优先真正的关卡页（课堂作业列表页 /tasks/ 之外）
        for i in infos:
            if "/tasks/" in i["url"]:
                pri = i
                break
    if pri is None:
        for i in infos:
            if "educoder.net" in i["url"]:
                pri = i
                break
    if pri is None:
        pri = infos[0]
    return pri, infos


def format_result(pri, infos, tail=80, max_body=12000, max_chars=6000) -> dict:
    term = pri["term"]
    src = pri["termSrc"] or "none"
    if not term and pri["log"]:
        term = log_to_text(pri["log"])
        src = "ws-log"
    lines = (term or "").splitlines()
    body = pri["body"] or ""
    login = bool(re.search(r"login|passport|signin", pri["url"], re.I))
    if not login and ("启动环境" not in body and "下一关" not in body):
        login = ("微信扫码登录" in body) or ("账号登录" in body) or ("扫码" in body)
    return {
        "ok": True,
        "url": pri["url"],
        "title": pri["title"],
        "login_page": login,
        "level": parse_level(body + "\n" + "\n".join(lines[-40:])),
        "buttons": [b["text"] for b in pri["buttons"]],
        "terminal_source": src,
        "terminal_lines": len(lines),
        "terminal_tail": tail_lines(term, tail),
        "terminal": (term[-max_chars:] if max_chars else term),
        "result_hint": guess_result(lines[-60:] + [
            ln for r in pri["results"] for ln in r["text"].splitlines()]),
        "test_result": [r["text"] for r in pri["results"]],
        "task_candidates": pri["tasks"],
        "page_text": body[:max_body] if max_body else body,
        "pages": [{"url": i["url"], "title": i["title"], "has_terminal": bool(i["term"])}
                  for i in infos],
    }


def is_login_page(page) -> bool:
    try:
        if re.search(r"login|passport|signin", page.url, re.I):
            return True
        txt = page.inner_text("body")[:4000]
    except Exception:
        return False
    if "启动环境" in txt or "下一关" in txt:
        return False
    return ("扫码" in txt) or ("微信登录" in txt) or ("手机号登录" in txt and "任务" not in txt)


def focus_terminal(page) -> bool:
    for f in page.frames:
        for sel in (".xterm-screen", ".xterm", "textarea"):
            try:
                loc = f.locator(sel)
                n = loc.count()
            except Exception:
                continue
            for i in range(n):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box or box["width"] < 20 or box["height"] < 10:
                        continue
                    el.click(timeout=3000)
                    return True
                except Exception:
                    continue
    return False


def click_in_page(page, texts, exact=True):
    """只在指定页面内点击，避免误操作其他标签页。"""
    for f in page.frames:
        for t in texts:
            try:
                loc = f.get_by_text(t, exact=exact)
                n = loc.count()
            except Exception:
                continue
            for i in range(n - 1, -1, -1):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    el.click(timeout=4000)
                    return {"ok": True, "clicked": t, "url": page.url}
                except Exception:
                    continue
    return {"ok": False, "error": f"未找到可点击元素: {texts}"}


def click_text_any(ctx, texts, exact=True):
    for p in live_pages(ctx):
        for f in p.frames:
            for t in texts:
                try:
                    loc = f.get_by_text(t, exact=exact)
                    n = loc.count()
                except Exception:
                    continue
                for i in range(n - 1, -1, -1):
                    try:
                        el = loc.nth(i)
                        if not el.is_visible():
                            continue
                        el.click(timeout=4000)
                        return {"ok": True, "clicked": t, "url": p.url}
                    except Exception:
                        continue
    return {"ok": False, "error": f"未找到可点击元素: {texts}"}


# ----------------------------------------------------------------------------
# 子命令实现
# ----------------------------------------------------------------------------
def quote_arg(s: str) -> str:
    return f'"{s}"' if (" " in s or not s) else s


def spawn_windows_detached(cmdline: str) -> bool:
    """通过 WMI 创建进程，父进程是 WmiPrvSE，不会被调用方的进程树回收。"""
    ps = (
        "$ErrorActionPreference='Stop';"
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine=$env:EDC_CMDLINE};"
        "if ($r.ReturnValue -ne 0) { Write-Error ('wmi-error ' + $r.ReturnValue) };"
        "Write-Output $r.ProcessId"
    )
    env = dict(os.environ, EDC_CMDLINE=cmdline)
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            env=env, capture_output=True, text=True, timeout=60,
        )
        return r.returncode == 0 and (r.stdout or "").strip().isdigit()
    except Exception:
        return False


def cmd_start(args):
    if cdp_alive(args.port):
        emit({"ok": True, "already_running": True, "port": args.port})
    exe = find_browser()
    if not exe:
        emit({"ok": False, "error": "未找到 Chrome/Edge，请安装，或用环境变量 EDC_BROWSER "
                                    "指定浏览器可执行文件路径"})
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        exe,
        f"--remote-debugging-port={args.port}",
        f"--user-data-dir={PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=ChromeWhatsNewUI",
        "--start-maximized",
    ]
    if args.headless:
        cmd.append("--headless=new")
    cmd.append("about:blank")

    ok = False
    if os.name == "nt":
        ok = spawn_windows_detached(" ".join(quote_arg(c) for c in cmd))
    if not ok:
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                  "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)

    for _ in range(60):
        if cdp_alive(args.port):
            emit({"ok": True, "browser": exe, "port": args.port,
                  "profile": str(PROFILE_DIR)})
        time.sleep(0.5)
    emit({"ok": False,
          "error": "浏览器启动超时。可能原因: (1) 已有同 profile 的浏览器在运行，请先完全退出；"
                   "(2) 当前执行环境会回收子进程，请用非沙箱方式执行 start"})


def cmd_open(args):
    if not re.match(r"^https?://", args.url or ""):
        emit({"ok": False, "error": "url 必须以 http(s):// 开头"})
    with browser_session(args) as ctx:
        page = None
        for p in live_pages(ctx):
            if p.url.rstrip("/") == args.url.rstrip("/"):
                page = p
                break
        if page is None or args.new_tab:
            page = ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2000)
        try:
            page.evaluate(HOOK_JS)
        except Exception:
            pass
        try:
            page.bring_to_front()
        except Exception:
            pass
        save_task_url(args.url)
        emit({"ok": True, "url": page.url, "title": page.title(),
              "need_login": is_login_page(page)})


def cmd_wait_login(args):
    target = args.url or load_task_url()
    if not target:
        emit({"ok": False,
              "error": "没有待登录的任务网址，请先 open <url>，或用 wait-login --url <url> 指定"})
    log(f"请在浏览器中用微信扫码登录。登录后头歌会跳转到首页，本命令会自动回到任务页：{target}")
    with browser_session(args) as ctx:
        deadline = time.time() + args.timeout
        last = ""
        page = None
        last_nav = 0.0
        while time.time() < deadline:
            try:
                page = resolve_page(ctx)
                if page.url != last:
                    log(f"[wait-login] {page.url}")
                    last = page.url

                if is_login_page(page):
                    time.sleep(3)
                    continue

                # 已登录，但被重定向到首页/其他页面 -> 重新进入任务页
                if "/tasks/" not in page.url:
                    if time.time() - last_nav > 5:
                        log(f"[wait-login] 已登录，重新进入任务页 ...")
                        page.goto(target, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(2500)
                        try:
                            page.evaluate(HOOK_JS)
                        except Exception:
                            pass
                        save_task_url(target)
                        last_nav = time.time()
                    time.sleep(2)
                    continue

                body = page.inner_text("body")
                if ("启动环境" in body) or ("任务描述" in body) or ("编程要求" in body) \
                        or page.locator(".xterm").count() > 0:
                    save_task_url(page.url)
                    emit({"ok": True, "logged_in": True, "url": page.url,
                          "title": page.title()})
            except Exception as e:
                log(f"[wait-login] 轮询异常: {type(e).__name__}: {e}")
            time.sleep(3)
        emit({"ok": False, "error": "等待超时，可能尚未扫码登录",
              "url": page.url if page else None})


def cmd_read(args):
    with browser_session(args) as ctx:
        if args.settle:
            time.sleep(args.settle)
        pri, infos = collect(ctx)
        if pri is None:
            emit({"ok": False, "error": "没有打开的页面，请先 open <url>"})
        emit(format_result(pri, infos, tail=args.tail, max_body=args.max_body,
                           max_chars=args.max_chars))


def cmd_status(args):
    with browser_session(args) as ctx:
        pri, infos = collect(ctx)
        if pri is None:
            emit({"ok": False, "error": "没有打开的页面"})
        res = format_result(pri, infos, tail=20, max_body=1500, max_chars=1500)
        res["task_candidates"] = []
        emit(res)


def cmd_start_env(args):
    with browser_session(args) as ctx:
        r = click_text_any(ctx, ["启动环境", "开始实训", "进入环境", "启动"])
        if not r["ok"]:
            emit(r)
        deadline = time.time() + args.timeout
        term = ""
        while time.time() < deadline:
            pri, _ = collect(ctx)
            term = (pri["term"] if pri else "") or log_to_text(pri["log"] if pri else [])
            if term.strip():
                emit({"ok": True, "terminal_ready": True, "url": pri["url"],
                      "terminal_tail": tail_lines(term, 40)})
            time.sleep(2)
        emit({"ok": True, "terminal_ready": False,
              "note": "已点击启动环境，但未检测到终端文本（可能仍在加载）"})


def read_text_arg(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8")
    if args.use_stdin:
        return sys.stdin.read()
    if args.text is not None:
        return args.text
    emit({"ok": False, "error": "请提供命令文本（位置参数 / --file / --stdin）"})
    return ""


def cmd_run(args):
    text = read_text_arg(args)
    if text == "":
        emit({"ok": False, "error": "命令为空"})
    with browser_session(args) as ctx:
        page = resolve_page(ctx, args.page)
        if not focus_terminal(page):
            emit({"ok": False,
                  "error": "未找到终端输入区。请先点击“启动环境”并等待右侧终端出现",
                  "hint": "python scripts/edc.py start-env"})
        page.wait_for_timeout(200)
        if args.clear:
            page.keyboard.press("Control+u")
            page.wait_for_timeout(100)
        mode = args.mode
        if mode == "auto":
            if "\n" in text:
                mode = "lines"
            elif all(ord(c) < 128 for c in text):
                mode = "type"
            else:
                mode = "insert"

        if mode == "lines":
            # 逐行键入 + 回车：heredoc / 多行脚本在真实 shell 里语义正确
            for line in text.splitlines():
                page.keyboard.type(line)
                page.keyboard.press("Enter")
                page.wait_for_timeout(args.line_delay)
        elif mode == "insert":
            page.keyboard.insert_text(text)
        else:
            page.keyboard.type(text)

        if mode != "lines" and not args.no_submit:
            page.wait_for_timeout(120)
            page.keyboard.press("Enter")
        if args.settle:
            time.sleep(args.settle)
        emit({"ok": True, "sent": text, "mode": mode,
              "submitted": (mode == "lines") or (not args.no_submit),
              "url": page.url})


def cmd_press(args):
    key = KEY_ALIAS.get(args.key.strip().lower(), args.key)
    with browser_session(args) as ctx:
        page = resolve_page(ctx, args.page)
        if not args.no_focus and not focus_terminal(page):
            emit({"ok": False, "error": "未找到终端输入区"})
        page.keyboard.press(key)
        if args.settle:
            time.sleep(args.settle)
        emit({"ok": True, "pressed": key})


def cmd_click(args):
    with browser_session(args) as ctx:
        page = resolve_page(ctx, args.page)
        r = click_in_page(page, [args.text], exact=not args.contains)
        if not r["ok"] and not args.contains:
            r = click_in_page(page, [args.text], exact=False)
        if r["ok"] and args.settle:
            time.sleep(args.settle)
        emit(r)


def cmd_next(args):
    with browser_session(args) as ctx:
        page = resolve_page(ctx, args.page)
        r = click_in_page(page, ["下一关", "下一节", "下一步"])
        if not r["ok"]:
            r = click_in_page(page, ["下一关", "下一节", "下一步"], exact=False)
        if r["ok"]:
            time.sleep(args.settle)
            try:
                # 页面可能已跳转到新关卡：等 URL 变化后再记录
                for _ in range(10):
                    if "/tasks/" in page.url:
                        break
                    time.sleep(0.5)
                save_task_url(page.url)
                r["url"] = page.url
            except Exception:
                pass
        emit(r if r["ok"] else {"ok": False,
                                "error": "未找到“下一关”按钮。可能已是最后一关，"
                                         "或当前关卡尚未通过（请 read 检查测试结果）"})


def cmd_wait_text(args):
    pat = re.compile(args.pattern, re.I)
    with browser_session(args) as ctx:
        deadline = time.time() + args.timeout
        term = ""
        while True:
            pri, infos = collect(ctx)
            if pri is not None:
                term = pri["term"] or log_to_text(pri["log"])
                m = pat.search(term or "")
                if m:
                    emit({"ok": True, "matched": m.group(0), "url": pri["url"],
                          "terminal_tail": tail_lines(term, args.tail)})
            if time.time() > deadline:
                emit({"ok": False, "error": f"等待超时: {args.pattern}",
                      "terminal_tail": tail_lines(term, args.tail)})
            time.sleep(args.interval)


def cmd_shot(args):
    path = Path(args.path) if args.path else (STATE_DIR / "shots" /
                                              f"shot-{int(time.time())}.png")
    path.parent.mkdir(parents=True, exist_ok=True)
    with browser_session(args) as ctx:
        page = resolve_page(ctx, args.page)
        page.screenshot(path=str(path), full_page=args.full)
        emit({"ok": True, "path": str(path)})


CODE_READ_JS = r"""
() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 10 && r.height > 10 &&
      getComputedStyle(el).visibility !== 'hidden' &&
      getComputedStyle(el).display !== 'none';
  };
  // 1) Monaco：可见编辑器的 .view-line
  const mon = [...document.querySelectorAll('.monaco-editor')].filter(vis);
  if (mon.length) {
    const box = mon[0];
    const lines = [...box.querySelectorAll('.view-line')].map((l) => l.innerText);
    if (lines.length) return { text: lines.join('\n'), src: 'monaco-dom' };
    const ta = box.querySelector('textarea.inputarea');
    if (ta && ta.value) return { text: ta.value, src: 'monaco-textarea' };
  }
  // 2) CodeMirror 5
  const cm = [...document.querySelectorAll('.CodeMirror')].filter(vis);
  if (cm.length && cm[0].CodeMirror) {
    return { text: cm[0].CodeMirror.getValue(), src: 'codemirror' };
  }
  // 3) 可见的 textarea
  const ta = [...document.querySelectorAll('textarea')].filter(vis);
  if (ta.length) return { text: ta[0].value || '', src: 'textarea' };
  return { text: '', found: false, src: 'none' };
}
"""


def editor_target(page):
    """返回 (frame, 可点击的编辑器 locator) ，找不到返回 (None, None)。"""
    sels = [".monaco-editor", ".CodeMirror", "textarea"]
    for f in page.frames:
        for sel in sels:
            try:
                loc = f.locator(sel)
                n = loc.count()
            except Exception:
                continue
            for i in range(n):
                try:
                    el = loc.nth(i)
                    if not el.is_visible():
                        continue
                    box = el.bounding_box()
                    if not box or box["width"] < 50 or box["height"] < 40:
                        continue
                    return f, el
                except Exception:
                    continue
    return None, None


def read_code(page) -> dict:
    for f in page.frames:
        try:
            d = f.evaluate(CODE_READ_JS)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("src") != "none" and d.get("text"):
            d["text"] = d["text"].replace("\xa0", " ")
            return d
    return {"text": "", "src": "none"}


def read_code_full(page) -> dict:
    """全量读取编辑器内容：优先剪贴板（可拿到滚动区域外的行），失败则退回 DOM。"""
    try:
        page.context.grant_permissions(["clipboard-read", "clipboard-write"],
                                       origin="https://www.educoder.net")
    except Exception:
        pass
    try:
        _, el = editor_target(page)
        if el is not None:
            el.click(timeout=4000)
            page.wait_for_timeout(150)
            page.keyboard.press("Control+a")
            page.wait_for_timeout(100)
            page.keyboard.press("Control+c")
            page.wait_for_timeout(250)
            txt = page.evaluate("() => navigator.clipboard.readText()")
            try:
                page.keyboard.press("ArrowRight")
            except Exception:
                pass
            if isinstance(txt, str) and txt.strip():
                return {"text": txt.replace("\xa0", " "), "src": "clipboard"}
    except Exception:
        pass
    return read_code(page)


def page_with_editor(ctx, page):
    """优先用指定页面；若无编辑器则退回到其他带编辑器的标签页。"""
    _, el = editor_target(page)
    if el is not None:
        return page
    for p in live_pages(ctx):
        if p is page:
            continue
        _, el2 = editor_target(p)
        if el2 is not None:
            return p
    return page


def cmd_code_read(args):
    with browser_session(args) as ctx:
        page = page_with_editor(ctx, resolve_page(ctx, args.page))
        d = read_code_full(page) if not args.dom_only else read_code(page)
        emit({"ok": True, "editor": d.get("src", "none"), "text": d.get("text", ""),
              "url": page.url})


def norm_code(s: str) -> str:
    s = (s or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    return "\n".join(line.rstrip() for line in s.split("\n")).strip()


CODE_PASTE_JS = r"""
(text) => {
  const tas = [...document.querySelectorAll('.monaco-editor textarea.inputarea, .monaco-editor textarea, textarea')];
  const ta = tas.find((t) => t.offsetParent !== null) || tas[0];
  if (!ta) return 'no-textarea';
  try {
    ta.focus();
    const dt = new DataTransfer();
    dt.setData('text/plain', text);
    const ev = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });
    ta.dispatchEvent(ev);
    return 'dispatched';
  } catch (e) {
    return 'error:' + e;
  }
}
"""


def cmd_code_write(args):
    text = read_text_arg(args)
    want = norm_code(text)
    with browser_session(args) as ctx:
        page = page_with_editor(ctx, resolve_page(ctx, args.page))
        frame, el = editor_target(page)
        if frame is None:
            emit({"ok": False, "error": "未找到代码编辑器（页面可能是终端型或尚未加载）"})

        def clear():
            el.click(timeout=5000)
            page.wait_for_timeout(200)
            page.keyboard.press("Control+a")
            page.wait_for_timeout(120)
            page.keyboard.press("Delete")
            page.wait_for_timeout(150)

        def check():
            d = read_code_full(page)
            got = norm_code(d.get("text", ""))
            # 逐行包含判断，兼容编辑器只渲染可视区域的情况
            ok = (got == want) or (bool(got) and got in want)
            return d, ok

        # 方案 1：构造 paste 事件（Monaco 粘贴不会重新缩进）
        clear()
        try:
            page.evaluate(CODE_PASTE_JS, text)
        except Exception:
            pass
        page.wait_for_timeout(500)
        after, ok = check()

        # 方案 2：系统剪贴板 + Ctrl+V
        if not ok:
            try:
                page.context.grant_permissions(["clipboard-read", "clipboard-write"],
                                               origin="https://www.educoder.net")
            except Exception:
                pass
            clear()
            pasted = False
            try:
                page.evaluate("(t) => navigator.clipboard.writeText(t)", text)
                page.wait_for_timeout(200)
                page.keyboard.press("Control+v")
                page.wait_for_timeout(500)
                pasted = True
            except Exception:
                pasted = False
            after, ok = check()
            if not pasted and not ok:
                # 方案 3：CDP insertText（可能被自动缩进影响）
                clear()
                page.keyboard.insert_text(text)
                page.wait_for_timeout(500)
                after, ok = check()

        emit({"ok": True, "verified": ok, "editor": after.get("src"),
              "written": len(text), "read_back": after.get("text", "")[:2000],
              "url": page.url})


QUIZ_READ_JS = r"""
() => {
  const items = [...document.querySelectorAll('ul.choose-container > li')];
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').trim();
  return items.map((li, i) => {
    const order = clean(li.querySelector('.order') ? li.querySelector('.order').innerText : '');
    const qEl = li.querySelector('.subject-body');
    const question = qEl ? clean(qEl.innerText) : '';
    const rows = [...li.querySelectorAll('a.flex-container')];
    const opts = rows.map((r) => {
      const labEl = r.querySelector('.checkTitle');
      const label = labEl ? clean(labEl.innerText).replace(/[、.．\s]/g, '') : '';
      const bodies = [...r.querySelectorAll('.subject-body')];
      const text = bodies.length ? clean(bodies[bodies.length - 1].innerText)
                                 : clean(r.innerText).replace(label, '');
      const inp = r.querySelector('input');
      return { label, text, type: inp ? inp.type : '',
               checked: !!(inp && inp.checked) };
    }).filter((o) => o.label);
    return { n: i + 1, order, question,
             type: opts.length ? (opts[0].type || 'radio') : 'none',
             multiple: opts.length ? (opts[0].type === 'checkbox') : false,
             options: opts };
  });
}
"""

QUIZ_PICK_JS = r"""
(answers) => {
  const clean = (s) => (s || '').replace(/\u00a0/g, ' ').trim();
  const items = [...document.querySelectorAll('ul.choose-container > li')];
  const report = [];
  for (const key of Object.keys(answers)) {
    const letters = answers[key].map((x) => String(x).toUpperCase());
    const want = new Set(letters);
    let li = null;
    for (const it of items) {
      const o = clean(it.querySelector('.order') ? it.querySelector('.order').innerText : '');
      if (parseInt(o, 10) === Number(key)) { li = it; break; }
    }
    if (!li) li = items[Number(key) - 1];
    if (!li) { report.push({ n: key, error: 'question not found' }); continue; }
    const done = [];
    for (const r of [...li.querySelectorAll('a.flex-container')]) {
      const labEl = r.querySelector('.checkTitle');
      const lab = labEl ? clean(labEl.innerText).replace(/[、.．\s]/g, '') : '';
      const inp = r.querySelector('input');
      if (!inp || !lab) continue;
      const isWant = want.has(lab);
      const target = r.querySelector('label') || r;
      if (inp.type === 'radio') {
        if (isWant && !inp.checked) { target.click(); done.push(lab); }
      } else if (isWant !== inp.checked) {
        target.click(); done.push(lab);
      }
    }
    report.push({ n: key, clicked: done });
  }
  return report;
}
"""


def parse_answers(s: str) -> dict:
    """'1:A;2:BC;3:A,C' -> {'1': ['A'], '2': ['B','C'], '3': ['A','C']}"""
    out = {}
    for part in re.split(r"[;；]", s or ""):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(\d+)\s*[:：]\s*([A-Za-z\s,，、]+)$", part)
        if not m:
            continue
        letters = [c.upper() for c in re.findall(r"[A-Za-z]", m.group(2))]
        if letters:
            out[m.group(1)] = letters
    return out


def cmd_quiz_read(args):
    with browser_session(args) as ctx:
        page = resolve_page(ctx, args.page)
        data = page.evaluate(QUIZ_READ_JS)
        emit({"ok": True, "count": len(data), "questions": data, "url": page.url})


def cmd_quiz_pick(args):
    answers = parse_answers(args.answers) if args.answers else {}
    if not answers and args.file:
        answers = parse_answers(Path(args.file).read_text(encoding="utf-8").strip())
    if not answers:
        emit({"ok": False, "error": '请提供答案，格式: --answers "1:A;2:BC;3:D"'})
    with browser_session(args) as ctx:
        page = resolve_page(ctx, args.page)
        report = page.evaluate(QUIZ_PICK_JS, answers)
        if args.settle:
            time.sleep(args.settle)
        after = page.evaluate(QUIZ_READ_JS)
        state = {}
        for q in after:
            picked = [o["label"] for o in q["options"] if o["checked"]]
            state[q["n"]] = picked
        emit({"ok": True, "applied": report, "selected": state, "url": page.url})


def cmd_eval(args):
    with browser_session(args) as ctx:
        page = resolve_page(ctx, args.page)
        try:
            val = page.evaluate(args.js)
        except Exception as e:
            emit({"ok": False, "error": f"{e}"})
        emit({"ok": True, "value": val})


def cmd_stop(args):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        emit({"ok": False, "error": "缺少 playwright"})
    if not cdp_alive(args.port):
        emit({"ok": True, "already_stopped": True})
    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{args.port}", timeout=15000)
        browser.close()
    except Exception as e:
        log(f"close 失败: {e}")
    finally:
        try:
            pw.stop()
        except Exception:
            pass
    emit({"ok": True, "stopped": True})


# ----------------------------------------------------------------------------
# 参数解析
# ----------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="edc.py",
        description="头歌(Educoder)实训自动通关辅助 CLI（Playwright + Chrome CDP）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--port", type=int, default=DEFAULT_PORT)
        return sp

    sp = add("start", "启动带调试端口的浏览器（持久化 profile，扫码登录只需一次）")
    sp.add_argument("--headless", action="store_true")
    sp.set_defaults(func=cmd_start)

    sp = add("open", "打开作业/实训网址")
    sp.add_argument("url")
    sp.add_argument("--new-tab", action="store_true")
    sp.set_defaults(func=cmd_open)

    sp = add("wait-login", "等待扫码登录完成（登录后自动跳回任务页）")
    sp.add_argument("--timeout", type=int, default=600)
    sp.add_argument("--url", default=None, help="任务页地址，默认使用最近一次 open 的地址")
    sp.set_defaults(func=cmd_wait_login)

    sp = add("read", "读取任务要求 + 终端输出 + 测试结果（JSON）")
    sp.add_argument("--settle", type=float, default=0)
    sp.add_argument("--tail", type=int, default=60)
    sp.add_argument("--max-body", type=int, default=6000)
    sp.add_argument("--max-chars", type=int, default=4000)
    sp.set_defaults(func=cmd_read)

    sp = add("status", "精简状态（先判断在不在任务页/终端是否就绪）")
    sp.set_defaults(func=cmd_status)

    sp = add("start-env", "点击“启动环境”并等待终端就绪")
    sp.add_argument("--timeout", type=int, default=90)
    sp.set_defaults(func=cmd_start_env)

    sp = add("run", "在右侧终端输入命令（默认回车执行）")
    sp.add_argument("text", nargs="?", default=None)
    sp.add_argument("--file", help="从文件读取要执行的命令")
    sp.add_argument("--stdin", dest="use_stdin", action="store_true")
    sp.add_argument("--no-submit", action="store_true", help="只输入不回车")
    sp.add_argument("--clear", action="store_true", help="先 Ctrl+U 清空当前输入行")
    sp.add_argument("--mode", choices=["auto", "type", "insert", "lines"], default="auto")
    sp.add_argument("--line-delay", type=float, default=0.08,
                    help="lines 模式下每行之间的间隔秒数")
    sp.add_argument("--settle", type=float, default=0)
    sp.add_argument("--page", default=None, help="按 URL 子串指定页面")
    sp.set_defaults(func=cmd_run)

    sp = add("press", "发送按键（Enter / Control+c / Tab / Up ...）")
    sp.add_argument("key")
    sp.add_argument("--no-focus", action="store_true")
    sp.add_argument("--settle", type=float, default=0)
    sp.add_argument("--page", default=None)
    sp.set_defaults(func=cmd_press)

    sp = add("click", "点击包含指定文字的元素")
    sp.add_argument("text")
    sp.add_argument("--contains", action="store_true", help="模糊包含匹配")
    sp.add_argument("--settle", type=float, default=1)
    sp.add_argument("--page", default=None)
    sp.set_defaults(func=cmd_click)

    sp = add("next", "点击“下一关”")
    sp.add_argument("--settle", type=float, default=2)
    sp.add_argument("--page", default=None)
    sp.set_defaults(func=cmd_next)

    sp = add("wait-text", "轮询等待终端出现指定正则")
    sp.add_argument("pattern")
    sp.add_argument("--timeout", type=float, default=60)
    sp.add_argument("--interval", type=float, default=2)
    sp.add_argument("--tail", type=int, default=60)
    sp.set_defaults(func=cmd_wait_text)

    sp = add("shot", "截图")
    sp.add_argument("path", nargs="?", default=None)
    sp.add_argument("--full", action="store_true")
    sp.add_argument("--page", default=None)
    sp.set_defaults(func=cmd_shot)

    sp = add("eval", "在页面执行 JS 并返回结果")
    sp.add_argument("js")
    sp.add_argument("--page", default=None)
    sp.set_defaults(func=cmd_eval)

    sp = add("code-read", "读取代码编辑器（Monaco/CodeMirror）当前内容")
    sp.add_argument("--dom-only", action="store_true", help="只用 DOM 读取（不碰剪贴板）")
    sp.add_argument("--page", default=None)
    sp.set_defaults(func=cmd_code_read)

    sp = add("quiz-read", "读取选择题题干与选项")
    sp.add_argument("--page", default=None)
    sp.set_defaults(func=cmd_quiz_read)

    sp = add("quiz-pick", "按题号填涂选择题，如 --answers \"1:A;2:BC;3:D\"")
    sp.add_argument("--answers", default=None)
    sp.add_argument("--file", default=None, help="答案文件（同样 1:A;2:BC 格式）")
    sp.add_argument("--settle", type=float, default=0.5)
    sp.add_argument("--page", default=None)
    sp.set_defaults(func=cmd_quiz_pick)

    sp = add("code-write", "覆盖写入代码编辑器（代码填空型关卡用）")
    sp.add_argument("text", nargs="?", default=None)
    sp.add_argument("--file", help="从文件读取代码")
    sp.add_argument("--stdin", dest="use_stdin", action="store_true")
    sp.add_argument("--page", default=None)
    sp.set_defaults(func=cmd_code_write)

    sp = add("stop", "关闭浏览器")
    sp.set_defaults(func=cmd_stop)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        emit({"ok": False, "error": "用户中断"})
    except Exception as e:
        emit({"ok": False, "error": f"{type(e).__name__}: {e}"}, code=1)


if __name__ == "__main__":
    main()
