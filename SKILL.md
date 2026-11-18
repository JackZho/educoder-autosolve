---
name: educoder-autosolve
description: 自动完成头歌（Educoder）实训/作业关卡。当用户给出 www.educoder.net/tasks/... 形式的作业网址，或要求"自动做头歌/自动闯关/自动在网页终端里执行命令并点下一关"时使用。流程为：启动持久化浏览器 → 打开网址 → 用户微信扫码登录 → 读取左侧任务要求 → 在右侧网页终端执行命令 → 确认测试通过 → 点击"下一关" → 循环直到全部通关。
---

# 头歌（Educoder）实训自动通关

## 适用场景

- 用户给出形如 `https://www.educoder.net/tasks/H7X3JLSF/3025442/njg6u3kmp2o4` 的网址；
- 页面左侧是任务要求，右侧是网页终端（xterm），可点击「启动环境」；
- 一个作业包含多个关卡（第 N 关），通过后右下角出现「下一关」。

## 一、环境准备（首次执行一次）

统一使用 conda `py310` 环境：

```powershell
conda activate py310
python -m pip install -r <SKILL_DIR>/scripts/requirements.txt
```

脚本入口固定为：

```powershell
python <SKILL_DIR>/scripts/edc.py <子命令>
```

**安装到 Codex / Claude Code**：把整个 `educoder-autosolve/` 目录复制到
`~/.codex/skills/educoder-autosolve/`（或项目的 `.codex/skills/`、`.claude/skills/`）即可被自动发现。

默认复用**本机已安装的 Chrome / Edge**（支持微信扫码，自动化痕迹少）；如需其他浏览器，用环境变量 `EDC_BROWSER` 指定可执行文件路径。扫码登录态保存在 `~/.educoder-skill/chrome-profile`，**只需扫码一次**。

> Windows 上 `start` 通过 WMI（`Win32_Process.Create`）拉起浏览器，避免被上层工具在命令结束时回收子进程。
> 若某些环境仍然出现"端口 9222 不存在"，请以**非沙箱**方式执行 `start`。

## 二、标准工作流

1. **启动常驻浏览器**（只需一次，重复执行是幂等的）：

   ```powershell
   python <SKILL_DIR>/scripts/edc.py start
   ```

2. **打开作业网址**：

   ```powershell
   python <SKILL_DIR>/scripts/edc.py open "https://www.educoder.net/tasks/.../..."
   ```

   返回 `need_login: true` 表示需要登录。

3. **提示用户扫码，然后等待登录**：
   先用自然语言告诉用户「浏览器已打开，请用微信扫码登录」，*再*执行：

   ```powershell
   python <SKILL_DIR>/scripts/edc.py wait-login --timeout 600
   ```

   头歌扫码成功后会**跳转到平台首页**。`wait-login` 已自动处理：检测到已登录且不在任务页时，
   会用最近一次 `open` 记录的地址（或 `--url` 指定的地址）**重新进入任务页**，成功后才返回 `{"ok": true}`。
   所以这一步返回成功后，不需要再手动 `open`。

   > 任务网址形如 `/tasks/H7X3JLSF/3025442/njg6u3kmp2o4`，最后一段是**关卡标识**（同一任务不同关卡不同）。
   > `next` 之后地址会变，脚本会自动更新记录，因此后续 `wait-login` 会回到当前所在关卡。

4. **启动环境**（右侧终端没出现时执行，已就绪可跳过）：

   ```powershell
   python <SKILL_DIR>/scripts/edc.py start-env
   ```

5. **逐关循环**：

   ```text
   read  →  看懂本关要求  →  run 命令  →  read 校验输出
         →  click 评测（命令行类关卡必须）→ read 看 test_result/result_hint
         →  通过？→ next；不通过 → 修正后重跑
   ```

   真实示例（已实测）：

   ```powershell
   python <SKILL_DIR>/scripts/edc.py run "apt-get install -y vsftpd" --settle 30
   python <SKILL_DIR>/scripts/edc.py click "评测" --settle 8
   python <SKILL_DIR>/scripts/edc.py read --tail 20 --max-body 0 --max-chars 300
   # -> "test_result": ["1/1 全部通过 ..."], "result_hint": {"result": "pass"}
   python <SKILL_DIR>/scripts/edc.py next --settle 6
   python <SKILL_DIR>/scripts/edc.py read      # 确认已切到第 2 关，任务要求已变化
   ```

5.1 **代码填空型关卡**（右侧是代码编辑器 + `#***begin***#` / `#***end***#`，没有终端）：

   先 `code-read` 看模板，把完整脚本（含模板注释和 begin/end 标记）写到本地文件，再 `code-write`：

   ```powershell
   python <SKILL_DIR>/scripts/edc.py code-read
   python <SKILL_DIR>/scripts/edc.py code-write --file .\answer.sh   # 返回 verified:true 表示写入一致
   python <SKILL_DIR>/scripts/edc.py click "评测" --settle 10
   python <SKILL_DIR>/scripts/edc.py read --max-body 0
   ```

   代码内容必须**整体覆盖**编辑器（脚本会先 Ctrl+A 全选，再用 paste 事件写入，避免 Monaco 自动缩进把代码搞乱）；题目如果要求 `passwd` 等交互命令，用 heredoc 形式写在同一个 begin/end 区间内。
   注意：这类关卡**不要**用 `run`（会写进不存在的终端），编辑器是 Monaco。
   `verified: true` 表示回读内容一致（回读优先用剪贴板，可拿到滚动区外的行）。

5.2 **选择题型关卡**（题干 + A/B/C… 选项，无终端、无编辑器）：

   ```powershell
   python <SKILL_DIR>/scripts/edc.py quiz-read                      # 输出 12 道题+选项+当前勾选
   python <SKILL_DIR>/scripts/edc.py quiz-pick --answers "1:A;2:ABCD;3:AC;4:B"
   python <SKILL_DIR>/scripts/edc.py click "评测" --settle 8
   python <SKILL_DIR>/scripts/edc.py click "记录" --settle 3
   python <SKILL_DIR>/scripts/edc.py read --max-body 0             # 记录里看“评测通过”
   ```

   单选会自动切换，多选会按目标状态点选/取消。若「测试结果」面板只显示「题目N 已提交」，
   说明答案已提交成功，**评级要在「记录」标签页看最新一条是否为「评测通过」**。

6. 全部关卡完成后执行一次 `read` 做最终确认。若 `buttons` 里出现「完成」，点它收尾：
   `python <SKILL_DIR>/scripts/edc.py click "完成"`。需要关闭浏览器再执行 `stop`。

## 三、命令速查

| 命令 | 作用 |
| --- | --- |
| `start [--headless]` | 启动带调试端口(9222)的浏览器 |
| `open "<url>" [--new-tab]` | 打开网址 |
| `wait-login [--timeout 600]` | 轮询等待扫码登录完成 |
| `start-env [--timeout 90]` | 点「启动环境」并等终端出现 |
| `read [--settle 2] [--tail 80]` | 输出 JSON：任务候选块、终端文本、按钮、测试结果提示 |
| `status` | 精简状态，判断是否在任务页 |
| `run "<cmd>" [--settle 2]` | 在终端输入命令并回车 |
| `run --file path.sh` | 执行多行脚本/heredoc（推荐，避免引号转义问题） |
| `run "<text>" --no-submit` | 只输入不回车 |
| `press "Control+c"` / `press Enter` | 发送按键 |
| `click "<文字>" [--contains]` | 点击任意按钮（如「评测」） |
| `next [--settle 3]` | 点击「下一关」 |
| `wait-text "测试通过" --timeout 90` | 轮询等待终端出现指定正则 |
| `shot [path]` | 截图（排查渲染问题用） |
| `code-read` | 读取代码编辑器内容（Monaco / CodeMirror / textarea） |
| `code-write "<code>"` / `--file ans.sh` | 整体覆盖写入代码编辑器，返回 `verified` |
| `quiz-read` | 读取选择题题干、选项、当前勾选状态 |
| `quiz-pick --answers "1:A;2:BC"` | 按题号填涂（单选自动切换、多选自动补齐） |
| `eval "<js>"` | 兜底：在页面执行 JS |
| `stop` | 关闭浏览器 |

### `read` 返回的关键字段

- `task_candidates`：左侧任务描述候选块（自动去掉包裹终端的大容器、去重嵌套块），**以第 1 项为主理解要求**；不够时看 `page_text`。
- `terminal_tail`：右侧终端最近若干行（`terminal_source` 为 `dom-rows` 最可靠，`ws-log` 为兜底）。
- `test_result`：评测结果面板文本，例如 `1/1 全部通过 ... 测试集1`。
- `result_hint`：`pass` / `fail` / `unknown` 的粗判，附命中行，**仍需自己读终端确认**。
- `buttons`：当前可见按钮。注意头歌的「下一关」**一直显示**，不代表已通过，务必先确认 `result_hint`。
- `level`：从文本里提取的「第 N 关 / 共 M 关」。

大页面想省 token 可以用 `read --max-body 0 --max-chars 500 --tail 20`。

## 四、解题规范（重要）

- **严格按左侧要求**：目录、文件名、语言、输出格式、变量名都要对齐，不要自创路径。
- **每步都要验证**：`run` 之后必须 `read`（或 `wait-text`）看真实输出，禁止臆测结果。
- **通过标准**：终端出现「测试通过 / 闯关成功 / 恭喜 / PASS」，或页面提示本关已完成。看到 `Traceback`、`未通过`、`FAIL` 必须修复后重跑。
- **多行脚本**：先 Write 到本地临时文件，再 `run --file xxx.sh`。终端里用 heredoc 也行，例如：

  ```text
  cat > /data/workspace/myshixun/step1/main.py <<'EOF'
  print("hello")
  EOF
  ```

- **`next` 之前必须确认本关已通过**；点击后页面会刷新成下一关，**必须重新 `read`**，任务要求会变。
- **同一关连续失败 3 次**：停下来检查 Python 版本（`python -V`、`python3 -V`）、工作目录（`pwd`、`ls`）、依赖是否安装、是否需要 `pip install`，不要盲目重试。
- **禁止**执行危险命令：`rm -rf /`、`mkfs`、关机/重启、与任务无关的大文件下载。
- 需要长时间运行的命令（服务、训练）用 `run "<cmd> &"` 或 `nohup`，否则会阻塞终端。

## 五、异常处理

| 现象 | 处理 |
| --- | --- |
| `read` 的 `terminal` 为空 | 先 `start-env`；仍为空则 `shot` 截图看页面是否报错/需要手动点授权 |
| 终端在新标签页 | `read` 的 `pages` 会列出所有页面；用 `--page <URL子串>` 指定 |
| 登录失效 / 跳到登录页（`login_page: true`） | 重新 `open` 该网址，提示用户再扫码，然后 `wait-login` |
| `next` 找不到按钮 | 当前关未通过，或已是最后一关；先 `read` 看 `result_hint` 和 `buttons` |
| 浏览器启动超时 | 已有同 profile 的 Chrome 在运行，请完全退出后重试 |
| 命令字符输入异常 | 用 `run --mode insert`（走粘贴通道，支持中文/特殊字符） |
| 多行脚本回显错乱/命令被吞 | `run --file` 会**逐行键入+回车**（`--line-delay` 可加大间隔）；避免一次粘贴整段 |
| 命令里的 `^`、`|`、引号被吞或变形 | Windows 上 `conda run` 会转义参数，复杂命令写进文件用 `run --file`，或改用不含 `^` 的等价写法 |
| 点了「评测」但结果没变 / 报错信息还是上一次的 | 确认 `click` 成功且 URL 是当前关卡；`read` 对比 `本次评测耗时` 是否刷新，未刷新就再点一次 |
| 多标签页时命令作用到了别的作业 | 所有命令默认只作用于最近一次 `open`/`next` 的关卡页（`~/.educoder-skill/last_task.json`）；仍不对时用 `--page <URL子串>` 指定 |
| 随机类题目评测偶尔不通过 | 题目本身是随机算法，重复 `click "评测"` 2~3 次；若仍失败，点「参考答案」对齐算法实现 |

## 六、与用户沟通

- 开场明确告知：会打开本机浏览器，需要用户微信扫码；登录只需一次。
- 每完成一关，简要汇报：关卡名、执行的关键命令、测试结果。
- 遇到必须人工介入（图形验证码、环境异常、需要授权）时，明确请用户在浏览器里操作，然后继续。
