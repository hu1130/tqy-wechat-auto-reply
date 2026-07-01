# tqy-wechat-auto-reply

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

## 手动安装

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python main.py init
python main.py doctor
```

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

## 项目结构

```text
tqy-wechat-auto-reply/
├── main.py                 # CLI 入口
├── demo.py                 # LLM-only 快速测试
├── wxdemo.py               # 兼容旧入口
├── setup_windows.ps1       # Windows 安装脚本
├── start_dry_run.bat       # Windows dry-run 启动脚本
├── start_live.bat          # Windows 真实发送启动脚本
├── wechat_ai/
│   ├── bot.py              # 微信监听和回复主逻辑
│   ├── config.py           # 配置加载和 .env 生成
│   ├── doctor.py           # 启动前体检
│   ├── llm.py              # Ollama / OpenAI-compatible 调用
│   ├── utils.py            # 上下文、去重、清理 think 标签
│   └── wizard.py           # init 交互向导
└── tests/
```

## 安全使用建议

- 第一次只监听 `文件传输助手`。
- 第一次只用 `DRY_RUN=1`。
- 不要一上来监听客户群或大群。
- 系统提示词尽量要求“短、自然、不要编造”。
- 真实发送前，先观察 dry-run 日志里的回复质量。

## 测试

```powershell
python -m unittest discover -s tests -v
```
