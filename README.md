# educoder-autosolve

> 给 AI 装一只「看得懂头歌、点得动浏览器、敲得进终端」的手。
> 不是 Selenium 模拟点击脚本，是一个让 Agent 自己读题、自己解题、自己验证的 Skill。

```
+-----------------------------------------------------------+
|                       你的 Agent (Codex / Claude / ...)   |
|                          │  读 JSON 结果                   |
|                          ▼                                |
|  edc.py  ── CDP(9222) ──>  Edge/Chrome(独立 profile)      |
|    │                        │                             |
|    │  read                  ├── 左：任务描述 / 选择题       |
|    ├─ run / code-write      ├── 右：xterm / Monaco 编辑器   |
|    └─ click 评测/下一关      └── 底：测试结果 / 评测记录     |
+-----------------------------------------------------------+
```

## 为什么不是又一个 RPA 脚本

- **一次扫码，长期有效**：浏览器用独立 profile 常驻，macOS/Windows 关掉终端也不掉登录态。
- **不用点击坐标**：全部走 DOM 与键盘事件，页面改版不会立刻失效。
- **终端是「读」出来的**：优先 `xterm.js` 的 DOM 行，失败时回落到注入的 WebSocket 抓包还原（自动去 ANSI）。
- **编辑器是「粘」进去的**：Monaco 逐行输入会被自动缩进搞乱，这里改用 paste 事件 + 剪贴板回读校验。
- **机器可读**：每条命令输出一行 JSON，Agent 不需要解析人类可读日志。

## 它覆盖的头歌题型

| 题型 | 特征 | 命令 |
| --- | --- | --- |
| 终端型 | 右侧 xterm，先点「启动环境」 | `start-env` / `run` / `read` |
| 代码填空 | `#***begin***#` + Monaco | `code-read` / `code-write --file` |
| 选择题 | `ul.choose-container` + radio/checkbox | `quiz-read` / `quiz-pick` |

## 快速开始

```bash
conda create -n py310 python=3.10 -y
conda activate py310
pip install -r scripts/requirements.txt
```

```bash
# 1. 拉起常驻浏览器（Windows 用 WMI 启动，避免被终端回收）
python scripts/edc.py start

# 2. 打开作业，扫码登录（只需一次）
python scripts/edc.py open "https://www.educoder.net/tasks/<id>/<id>/<level>"
python scripts/edc.py wait-login          # 登录后会跳首页，这里自动回任务页

# 3. 循环：读题 -> 操作 -> 评测 -> 下一关
python scripts/edc.py read
python scripts/edc.py run "service vsftpd start" --settle 3
python scripts/edc.py click "评测" --settle 10
python scripts/edc.py next
```

## 命令速查

```
start                    启动调试浏览器（--headless 可选，扫码请用有头）
open <url>               打开作业
wait-login               等待扫码，登录后自动重进任务页
start-env                点「启动环境」并等终端就绪

read                     任务描述 + 终端尾部 + 按钮 + 测试结果 hint
status                   精简状态
run "<cmd>" | --file f   终端执行（多行自动逐行键入，heredoc 可用）
press Control+c          发送按键
click "<文字>"            点按钮（只在当前任务页内点击）
next                     下一关
wait-text "<regex>"      轮询终端输出
code-read / code-write   读写 Monaco / CodeMirror
quiz-read / quiz-pick    读题 / 按 "1:A;2:BC" 填涂
shot / eval              截图 / 页面执行 JS（兜底）
stop                     关闭浏览器
```

## 值得一看的实现细节

- **浏览器为什么要常驻**：`connect_over_cdp` 每次命令重新连接，进程退出不影响浏览器；Windows 下用 `Invoke-CimMethod Win32_Process Create` 拉起，父进程是 WmiPrvSE，不会被调用方进程树回收。
- **任务页优先级**：所有命令只作用于「最近一次 open/next 记录的关卡页」（`~/.educoder-skill/last_task.json`），避免多标签页时点到别的作业。
- **评测结果从哪来**：优先抓「测试结果」面板里的 `全部通过/未通过`；选择题这类只显示「已提交」的，去「记录」标签页看最新一条是否为「评测通过」。
- **随机算法题**：评测本身有随机性，失败先重试 2~3 次；仍不过再对齐「参考答案」的算法结构。

## 目录

```
educoder-autosolve/
├── SKILL.md              # 给 Agent 的作业手册（工作流 / 规范 / 异常处理）
└── scripts/
    ├── edc.py            # 全部能力都在这里，单文件无依赖（除 playwright）
    └── requirements.txt
```

## 风险与边界

- 仅供学习与自测，**请遵守头歌平台的使用条款**；用它刷分、代考产生的后果由使用者自负。
- 平台改版会让选择器失效，届时改 `edc.py` 里顶部的 `READ_JS / QUIZ_READ_JS` 即可，逻辑层不用动。
- 登录态 = 你的微信账号，profile 目录 `~/.educoder-skill/` 不要分享给别人。

---

<p align="center"><sub>talk is cheap, show me the JSON.</sub></p>
