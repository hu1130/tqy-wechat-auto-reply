# Windows Agent 正式打包与安装

本文档描述个人微信 Windows Agent 的交付骨架。它不代表已经在全新 Windows、真实微信客户端或生产签名证书环境完成验收。

## 交付结构

- 程序目录：`%LOCALAPPDATA%\Programs\DeepAwake\WeChatAgent`
- 当前版本：`versions\<MAJOR.MINOR.PATCH>\WeChatAIAgent.exe`
- 稳定启动器：`WeChatAIAgentLauncher.exe`
- 用户数据：`%LOCALAPPDATA%\DeepAwake\WeChatAgent`
- 登录任务：`DeepAwake WeChat Agent`

计划任务使用当前用户 `InteractiveToken`，隐藏运行且禁止并行实例。它不能安装为 Session 0 Windows 服务，因为桌面微信自动化必须访问已登录用户的交互桌面。

## 构建机要求

1. Windows 10/11 x64 和原生 x64 Python 3.10 或更高版本。
2. 项目依赖已安装，且 `wxauto4==41.1.2`、`uiautomation==2.0.29`。
3. 单独安装 PyInstaller：`python -m pip install "pyinstaller>=6,<7"`。
4. 安装 Inno Setup 6，并取得 `ISCC.exe` 的绝对路径。
5. 安装 Windows SDK，并取得 `signtool.exe` 的绝对路径。
6. Windows 代码签名证书已导入证书存储，并取得 40 位 SHA-1 证书指纹。
7. 独立的 Ed25519 发布私钥。它只用于签名更新 manifest，不可放入源码、安装包或 Web 服务器。

Windows Authenticode 证书和 Ed25519 更新密钥用途不同，不能互相替代：前者签名 EXE/安装器，后者让已安装 Agent 验证更新 manifest。

默认 RFC3161 时间戳服务为
`http://timestamp.digicert.com`。时间戳响应自身经过签名并由 SignTool
验证；`-TimestampUrl` 只接受不含凭据和片段的 HTTP/HTTPS 地址。

## 生成 Ed25519 密钥

下面示例只适合在受控构建机首次生成，私钥随后应转移到 CI 密钥库或硬件保护存储：

```powershell
@'
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

key = Ed25519PrivateKey.generate()
Path("agent-release-ed25519.pem").write_bytes(
    key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
)
'@ | python -
```

生产环境应使用加密私钥，并通过环境变量 `AGENT_ED25519_PRIVATE_KEY_PASSWORD` 向构建进程提供密码。

## 构建命令

中心 URL、更新 manifest URL 和更新包 URL 只在构建时传入，安装向导不会让客户填写或覆盖中心地址。

```powershell
cd "C:\Users\HYF05\Documents\公司的项目\微信AI多租户客服"

.\build_agent.ps1 `
  -Version "1.0.0" `
  -CenterUrl "https://wechat-center.example.com" `
  -UpdateManifestUrl "https://downloads.example.com/wechat-agent/stable.json" `
  -PackageUrl "https://downloads.example.com/wechat-agent/WeChatAIAgent-1.0.0-x64.zip" `
  -Ed25519PrivateKeyPath "C:\secure\agent-release-ed25519.pem" `
  -CertificateThumbprint "0123456789ABCDEF0123456789ABCDEF01234567" `
  -ISCCPath "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
  -SignToolPath "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe"
```

脚本在构建前会 fail fast 检查：

- Python 和进程必须是 AMD64/x64。
- wxauto4 可定位，uiautomation 和构建依赖可导入，固定依赖版本匹配。
- 版本必须是数字形式 `MAJOR.MINOR.PATCH`。
- 中心、manifest 和更新包 URL 必须使用 HTTPS。
- Ed25519 私钥、ISCC 和 SignTool 必须存在。
- Agent 目录内的 EXE、DLL、PYD、稳定 launcher 和最终安装器必须通过 Authenticode 验证。
- Agent 目录内的全部原生文件和 launcher 必须先通过 SignTool 验证，再实际运行 Agent 的 `version` 和 `self-test`；构建脚本最多等待 180 秒并检查真实退出码，失败或超时都禁止出包。
- 最终安装器也必须通过 SignTool 验证。
- 正式发布证书环境还必须在全新 Windows 上实际运行签名后的 Agent 和安装器；自签测试证书可能被 Smart App Control 拒绝，不能作为客户交付证书。

输出位于 `build\windows-agent`：

- `installer\微信客服Agent安装程序-<version>-x64.exe`
- `WeChatAIAgent-<version>-x64.zip`
- `agent-update-<version>.json`

上传更新时必须先上传 ZIP，确认 HTTPS 可下载后，最后原子发布 manifest。不要发布指向尚未上传包的 manifest。

## 客户安装流程

1. 客户登录自己的 Windows 用户和桌面微信。
2. 运行签名后的安装程序。
3. 输入平台生成的一次性激活码。
4. 安装器调用打包 EXE 的 `agent activate`，由 `main.py` 的激活命令兑换凭据并使用当前用户 DPAPI 保存。
5. 只有激活成功后，安装器才创建登录计划任务并立即尝试启动。

激活失败会终止安装流程，并且不会留下自启动任务。安装器不接受中心 URL 输入。

## 自动升级与回滚

Agent 启动时及运行期间每 6 小时检查一次 HTTPS manifest：

1. 用内置 Ed25519 公钥验证 manifest 签名。
2. 拒绝低于或等于当前版本的包。
3. 下载到暂存目录并核对签名中的长度和 SHA-256。
4. 解压时拒绝绝对路径、`..`、Windows 盘符和符号链接。
5. 将完整版本放入新的 `versions\<version>` 目录，再原子切换 `update-state.json`。
6. Agent 以退出码 75 通知稳定 launcher 启动新版本。
7. 新版本必须在 90 秒内写入新的 `running` 健康状态，否则 launcher 终止它、切回保留的上一版本并重新启动。

缺少 HTTPS、签名、哈希或上一版本时一律拒绝继续，不会使用未验证包替代。

## 升级与卸载数据规则

- 覆盖安装和自动升级保留 profile、spool、日志、报表及上一版本。
- 卸载始终删除登录任务、程序文件、加密 `profile.json` 和运行标记。
- 卸载界面提供“同时删除本地消息队列、日志和报表”选项，默认不勾选。
- 勾选删除数据后会删除整个 `%LOCALAPPDATA%\DeepAwake\WeChatAgent`，不可恢复。

## 干净 Windows 验收表

- [ ] Windows 10/11 x64，全新普通用户，无 Python、pip 和源码目录。
- [ ] 安装器、Agent EXE 和 launcher 的 Authenticode 签名有效且证书链受信任。
- [ ] 安装向导为中文，只要求一次性激活码，不显示中心 URL 输入。
- [ ] 无效、过期、已使用激活码会阻止安装完成，任务计划程序中无残留任务。
- [ ] 有效激活后 profile 中没有明文激活码或 Agent Token。
- [ ] 任务 XML 包含 `InteractiveToken`、`Hidden=true`、`IgnoreNew`，不引用 Python、`.env`、`main.py` 或 watchdog。
- [ ] 当前用户登录后 Agent 隐藏启动；未登录或 Session 0 中不启动微信自动化。
- [ ] 重启 Windows 并登录后，中心后台显示正确租户、Agent 和微信渠道在线。
- [ ] 收消息、启动旧消息基线、策略切换、Outbox 发送和断网恢复使用真实微信完成验证。
- [ ] 正常签名更新能够切换到新版本，并保留 profile、spool 和日志。
- [ ] HTTP manifest、错误签名、错误 SHA-256、低版本和路径穿越包全部被拒绝。
- [ ] 新版本不写健康状态时，90 秒后自动回滚并启动上一版本。
- [ ] 卸载后任务和 profile 已删除；分别验证“保留数据”和“删除数据”两个选项。

当前代码只完成了可测试的打包、安装、自启、更新和回滚骨架。上述清单必须在干净 Windows 和真实微信环境执行后，才能标记为正式验收。
