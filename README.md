# tqy-wechat-auto-reply

## WeChat 4.x Test Edition

This project now uses the `wxauto4` test adapter for the current Windows `Weixin.exe` client. Do not install the discontinued WeChat 3.9.x client.

- The dependency is pinned to `wxauto4==41.1.2` for Windows x64 and Python 3.9 through 3.13.
- Desktop WeChat updates can change the UI automation surface. Every client version must pass `run --dry-run` before real sending is considered.
- Run `python main.py doctor --skip-llm`. `wxauto4` must report `[OK]`. A WeChat process warning only means the doctor could not inspect the current session; dry-run is the final attachment check.
- If Windows application control blocks `wxauto4`, ask the device administrator to review and approve the package. Do not disable or bypass client login, update, or system security controls.
- Whitelisting, sensitive keyword filtering, cooldowns, send limits, audit logs, and manual confirmation remain enabled. `DRY_RUN=1` remains the default.

一个本地运行的微信 AI 自动回复工具。

它会监听你指定的微信好友或微信群，把最近聊天上下文发给大模型，再把生成的回复发回微信。适合做客服辅助、私域消息初筛、社群答疑、个人微信助理等场景。

## 当前状态

- 真实微信自动回复只支持 Windows 桌面微信。
- 微信接入基于 `wxauto`。
- 大模型支持本地 Ollama，也支持 OpenAI-compatible API。
- 默认先用 dry-run 模式，只生成回复，不真实发送。
- 已内置启动前体检命令，能检查 Python、Windows、`wxauto`、`.env` 和 LLM 配置。

## 工作流程

```text
Windows 微信桌面端
        |
      wxauto
        |
  tqy-wechat-auto-reply
        |
  最近聊天上下文 + 系统提示词
        |
  Ollama / OpenAI-compatible API
        |
  清理 <think>...</think>
        |
  dry-run 打印 或 真实发送到微信
```

## Windows 快速开始

先确保：

- 已安装 Python 3.9+
- 已安装并登录 Windows 桌面微信
- 已准备一个可用的大模型后端

推荐直接运行：

```powershell
.\setup_windows.ps1
```

然后做体检：

```powershell
.\.venv\Scripts\python.exe main.py doctor
```

先 dry-run：

```bat
start_dry_run.bat
```

确认日志里的回复符合预期后，再进入真实发送模式：

```bat
start_live.bat
```

`start_live.bat` 会要求你手动输入 `YES`，避免误触发真实回复。

## 全新电脑一键下载测试

目标电脑要求：

- Windows 10/11
- 能访问 GitHub
- 已安装并登录 Windows 桌面微信
- 如果仓库是 private，需要能登录 GitHub 账号 `hu1130`

### private 仓库推荐流程

在全新电脑打开 PowerShell，先安装并登录 GitHub CLI：

```powershell
winget install --id GitHub.cli -e --source winget
gh auth login
```

然后下载并运行 bootstrap：

```powershell
$token = gh auth token
$headers = @{ Authorization = "Bearer $token" }
irm -Headers $headers https://raw.githubusercontent.com/hu1130/tqy-wechat-auto-reply/main/bootstrap_clean_windows.ps1 -OutFile bootstrap_clean_windows.ps1
powershell -ExecutionPolicy Bypass -File .\bootstrap_clean_windows.ps1
```

脚本会自动做这些事：

- 安装/检查 Python 3.11
- 安装/检查 Git
- 克隆或更新 `https://github.com/hu1130/tqy-wechat-auto-reply.git`
- 创建 `.venv`
- 安装 `requirements.txt`
- 生成安全默认 `.env`
- 跑完整单元测试
- 跑 `doctor --skip-llm`
- 跑 `simulate --reply "ok, received" "hello"`，不登录微信也能验证回复链路

默认安装目录：

```text
%USERPROFILE%\Documents\tqy-wechat-auto-reply
```

如果你已经从 GitHub 下载 zip 并解压，也可以直接双击：

```bat
test_environment.bat
```

或手动运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_test_environment.ps1
```

通过后先测试 dry-run：

```powershell
cd "$env:USERPROFILE\Documents\tqy-wechat-auto-reply"
.\.venv\Scripts\python.exe main.py run --dry-run --chat 文件传输助手
```

## 手动安装

### 无微信/无 Ollama 的快速模拟测试

如果新电脑还没有登录微信，或者大模型还没装好，可以先用固定回复跑完整安全链路：

```powershell
.\.venv\Scripts\python.exe main.py simulate --chat 文件传输助手 --reply "您好，已收到，我稍后确认。" "你好，在吗"
```

这个命令不会启动 wxauto，也不会真实发送微信消息。它会走同一套白名单、黑名单、敏感词、冷却时间、审计日志逻辑，适合先确认环境和配置是否正常。

要测试“生成后进入人工确认队列”，运行：

```powershell
.\.venv\Scripts\python.exe main.py simulate --queue --chat 文件传输助手 --reply "您好，已收到，我稍后确认。" "你好，在吗"
.\.venv\Scripts\python.exe main.py admin pending
```

如果复制中文示例时被终端编码影响，可以使用纯 ASCII 版本；它会自动读取 `.env` 中第一个 `WECHAT_LISTEN_CHATS`：

```powershell
.\.venv\Scripts\python.exe main.py simulate --reply "ok, received" "hello"
.\.venv\Scripts\python.exe main.py simulate --queue --reply "ok, received" "hello"
.\.venv\Scripts\python.exe main.py admin pending
```

`--queue` 只会写入 `logs/pending_replies.jsonl`，不会发送到真实微信。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python main.py init
python main.py doctor
```

如果安装时出现 `No matching distribution found for wxauto`，说明 pip 在 PyPI 查找旧包失败。当前项目的 `requirements.txt` 已改为从 `https://github.com/cluic/wxauto` 的源码包安装，重新执行：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果运行 `run --dry-run` 时出现 `SetWindowPos` / `无效的窗口句柄`，说明 `wxauto` 没找到经典 PC 微信主窗口。处理方式：

- 关闭当前微信，打开并登录经典 Windows 桌面微信 3.9.x。
- 不要使用新版 `Weixin` / WeChat 4.x 窗口；当前 wxauto 目标窗口类是 `WeChatMainWndForPC`。
- 保持微信主窗口可见，不要只停留在托盘或后台。
- PowerShell 和微信使用同一权限级别：都普通运行，或都管理员运行。
- 再执行 `.\.venv\Scripts\python.exe main.py doctor --skip-llm`，确认 `WeChat window` 不再警告。

如果你只想生成默认 `.env`，不走交互向导：

```powershell
python main.py init --yes
```

## 配置

配置文件是 `.env`。可以从 `.env.example` 复制，也可以用 `python main.py init` 生成。

### 使用 Ollama

```env
LLM_PROVIDER=ollama
LLM_MODEL=deepseek-r1:14b
OLLAMA_BASE_URL=http://localhost:11434
```

确保 Ollama 已启动，并且模型已拉取：

```powershell
ollama pull deepseek-r1:14b
```

### 使用 OpenAI-compatible API

```env
LLM_PROVIDER=openai_compatible
LLM_MODEL=gpt-4o-mini
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=你的_key
```

也可以填任何兼容 `/chat/completions` 的服务地址。

### 微信监听配置

```env
WECHAT_LISTEN_CHATS=文件传输助手,测试群
WECHAT_SELF_NAMES=你的微信昵称
DRY_RUN=1
```

- `WECHAT_LISTEN_CHATS`：要监听的好友名或群名，多个用英文逗号分隔。
- `WECHAT_SELF_NAMES`：你的微信昵称，用来减少自己消息触发回复。
- `DRY_RUN=1`：只打印回复，不发送。
- `DRY_RUN=0`：真实发送回复。

建议永远先 dry-run 一段时间，再切到真实发送。

### 安全护栏配置

```env
REPLY_ALLOWED_CHATS=文件传输助手
REPLY_BLOCKED_CHATS=
REPLY_ALLOWED_SENDERS=
REPLY_BLOCKED_SENDERS=
TEXT_MESSAGE_TYPES=text,friend
SENSITIVE_KEYWORDS=转账,付款,收款,验证码,密码,银行卡,身份证,合同,报价,价格,地址,电话,手机号,隐私,发票,退款,投诉
MAX_REPLY_CHARS=120
BLOCKED_REPLY_PATTERNS=我是AI,作为AI,我不能,无法提供,验证码,密码,银行卡,身份证,转账
DEFAULT_CHAT_POLICY=manual
CHAT_POLICIES=
REPLY_COOLDOWN_SECONDS=30
REQUIRE_MANUAL_CONFIRM=1
APPROVAL_MODE=console
PENDING_REPLIES_FILE=logs/pending_replies.jsonl
MAX_SENDS_PER_RUN=20
MAX_SENDS_PER_CHAT=5
MAX_SENDS_PER_DAY=100
MAX_SENDS_PER_CHAT_PER_DAY=20
MAX_ERRORS_BEFORE_PAUSE=3
LOG_FILE=logs/wechat-ai.log
AUDIT_LOG_FILE=logs/audit.jsonl
ALERT_FILE=logs/alerts.log
HEALTH_FILE=logs/health.json
LOG_MAX_BYTES=10485760
LOG_BACKUP_COUNT=5
REPORTS_DIR=reports
STATE_DB_FILE=data/wechat-ai.sqlite3
```

- `REPLY_ALLOWED_CHATS` / `REPLY_BLOCKED_CHATS`：按聊天名控制是否允许自动回复；白名单不能为空，建议从 `文件传输助手` 开始。
- `REPLY_ALLOWED_SENDERS` / `REPLY_BLOCKED_SENDERS`：按发送人控制是否允许自动回复，适合群聊里排除特定成员。
- `TEXT_MESSAGE_TYPES`：只允许这些 wxauto 消息类型触发回复，默认只处理文本类消息。
- `SENSITIVE_KEYWORDS`：收到包含这些关键词的消息时跳过自动回复，留给人工处理。
- `MAX_REPLY_CHARS`：模型回复超过长度上限时拒绝发送，`0` 表示不限制。
- `BLOCKED_REPLY_PATTERNS`：模型回复包含这些模式时拒绝发送。
- `DEFAULT_CHAT_POLICY`：默认聊天策略，支持 `auto`、`manual`、`dry_run`、`ignore`。
- `CHAT_POLICIES`：给单个聊天覆盖策略，格式如 `客户A=manual,测试群=dry_run,广告群=ignore`。
- `REPLY_COOLDOWN_SECONDS`：同一个聊天两次回复之间的冷却时间，`0` 表示关闭。
- `REQUIRE_MANUAL_CONFIRM=1`：真实发送前逐条要求在控制台输入 `YES`，建议上线初期保持开启。
- `APPROVAL_MODE=queue`：真实发送前只写入待确认队列，配合 `python main.py admin pending` 查看，不会调用 `SendMsg`。
- `MAX_SENDS_PER_RUN` / `MAX_SENDS_PER_CHAT`：限制单次运行和单个聊天的真实发送条数，`0` 表示不限制。
- `MAX_SENDS_PER_DAY` / `MAX_SENDS_PER_CHAT_PER_DAY`：限制每天真实发送总量和单个聊天每天发送量，依赖 `STATE_DB_FILE`。
- `MAX_ERRORS_BEFORE_PAUSE`：连续发生 LLM 或微信发送错误后自动暂停，`0` 表示不暂停。
- `LOG_FILE`：运行日志文件路径，留空则只打印到控制台。
- `AUDIT_LOG_FILE`：JSONL 审计日志，记录每条消息为什么发送、dry-run、跳过或报错。
- `ALERT_FILE`：本地告警日志，机器人暂停或关键错误会写入这里。
- `HEALTH_FILE`：健康状态 JSON 文件，供 watchdog、任务计划或人工巡检查看。
- `LOG_MAX_BYTES` / `LOG_BACKUP_COUNT`：日志、审计日志、告警日志轮转配置。
- `REPORTS_DIR`：`admin report --save` 的日报输出目录。
- `STATE_DB_FILE`：SQLite 状态库，用于持久化聊天暂停、策略覆盖、冷却时间、审计事件和日报。

聊天策略含义：

- `auto`：通过护栏后可直接发送；如果全局 `REQUIRE_MANUAL_CONFIRM=1`，仍需人工确认。
- `manual`：通过护栏后也必须逐条输入 `YES` 才发送。
- `dry_run`：只生成和记录回复，不真实发送。
- `ignore`：完全忽略该聊天。

### 知识库配置

```env
KNOWLEDGE_FILE=knowledge/faq.md
KNOWLEDGE_TOP_K=3
REQUIRE_KNOWLEDGE_MATCH=0
KNOWLEDGE_FALLBACK_REPLY=这个问题我需要确认一下，稍后回复你。
```

- `KNOWLEDGE_FILE`：公司 FAQ/规则文档，使用 UTF-8 Markdown。默认模板在 `knowledge/faq.md`。
- `KNOWLEDGE_TOP_K`：每次最多取几个匹配片段给模型。
- `REQUIRE_KNOWLEDGE_MATCH=1`：没有匹配知识库时不调用模型，直接跳过并记录审计，适合正式客户场景。
- `KNOWLEDGE_FALLBACK_REPLY`：资料不足时要求模型输出的兜底话术。

公司使用时建议把产品 FAQ、售后规则、禁答规则、转人工场景都写进 `knowledge/faq.md`，再逐步打开 `REQUIRE_KNOWLEDGE_MATCH=1`。

## 命令

查看当前配置：

```powershell
python main.py config
```

启动前体检：

```powershell
python main.py doctor
```

只测试大模型回复：

```powershell
python main.py demo "帮我回一句：今天晚点到"
```

监听指定聊天，但不真实发送：

```powershell
python main.py run --dry-run --chat 文件传输助手
```

监听多个聊天：

```powershell
python main.py run --dry-run --chat 文件传输助手 --chat 测试群
```

真实发送：

```powershell
python main.py run --live
```

运维管理：

```powershell
python main.py admin status
python main.py admin pending
python main.py admin pause 客户A --reason handoff
python main.py admin resume 客户A
python main.py admin policy 客户A manual
python main.py admin policy 测试群 dry_run
python main.py admin report
python main.py admin report --date 2026-07-11
python main.py admin report --save
```

这些命令读写 `STATE_DB_FILE`，可以在机器人运行前后查看状态、暂停单个聊天、恢复聊天、临时覆盖聊天策略、生成日报。

生产脚本：

```powershell
start_prod.bat
.\run_watchdog.ps1 -Live
save_daily_report.bat
```

- `start_prod.bat`：交互确认后启动生产 watchdog。
- `run_watchdog.ps1`：启动前运行 `doctor --skip-llm`，机器人异常退出后自动重启，并限制每小时重启次数。
- `save_daily_report.bat`：保存当天日报到 `REPORTS_DIR`，适合放进 Windows 任务计划每天执行。

日志和审计会自动脱敏手机号、身份证、银行卡、邮箱和验证码；完整敏感信息不应落盘。

## 公司稳定使用清单

- 用专用 Windows 机器或云桌面运行，固定 Python、微信、依赖和模型版本。
- 关闭系统休眠，避免微信自动升级，保持桌面微信已登录。
- 第一周只使用 `DRY_RUN=1`，并监听 `文件传输助手`、测试群、内部员工群。
- 正式客户场景保持 `REQUIRE_MANUAL_CONFIRM=1`，逐条确认后再发。
- 所有客户聊天必须进入 `REPLY_ALLOWED_CHATS`，不允许空白放行。
- 高风险问题通过 `SENSITIVE_KEYWORDS` 和 `REQUIRE_KNOWLEDGE_MATCH=1` 转人工。
- 每天检查 `python main.py admin report` 和 `logs/audit.jsonl`。
- 每天用 `save_daily_report.bat` 保存日报。
- 关注 `logs/alerts.log` 和 `logs/health.json`。
- 使用 `admin pause` 暂停异常聊天，人工处理后再 `admin resume`。

## 项目结构

```text
tqy-wechat-auto-reply/
├── main.py                 # CLI 入口
├── demo.py                 # LLM-only 快速测试
├── wxdemo.py               # 兼容旧入口
├── bootstrap_clean_windows.ps1 # 全新电脑下载和测试入口
├── setup_test_environment.ps1  # 本地环境创建和测试脚本
├── test_environment.bat    # zip 解压后的双击测试入口
├── setup_windows.ps1       # Windows 安装脚本
├── start_dry_run.bat       # Windows dry-run 启动脚本
├── start_live.bat          # Windows 真实发送启动脚本
├── start_prod.bat          # 生产 watchdog 启动脚本
├── run_watchdog.ps1        # 自动重启 watchdog
├── save_daily_report.bat   # 保存每日报告
├── knowledge/
│   └── faq.md              # 公司知识库模板
├── wechat_ai/
│   ├── admin.py            # 运维管理命令
│   ├── bot.py              # 微信监听和回复主逻辑
│   ├── config.py           # 配置加载和 .env 生成
│   ├── doctor.py           # 启动前体检
│   ├── knowledge.py        # 本地知识库检索
│   ├── llm.py              # Ollama / OpenAI-compatible 调用
│   ├── ops.py              # 脱敏、日志轮转、健康文件、日报保存
│   ├── store.py            # SQLite 状态和审计存储
│   ├── utils.py            # 上下文、去重、清理 think 标签
│   └── wizard.py           # init 交互向导
└── tests/
```

## 安全使用建议

- 第一次只监听 `文件传输助手`。
- 第一次只用 `DRY_RUN=1`。
- 不要一上来监听客户群或大群。
- 刚切到 `--live` 时保持 `REQUIRE_MANUAL_CONFIRM=1`，确认每条回复后再发送。
- 给 `MAX_SENDS_PER_RUN` 和 `MAX_SENDS_PER_CHAT` 设置较低上限，确认稳定后再放宽。
- 系统提示词尽量要求“短、自然、不要编造”。
- 真实发送前，先观察 dry-run 日志里的回复质量。

## 测试

```powershell
python -m unittest discover -s tests -v
```
