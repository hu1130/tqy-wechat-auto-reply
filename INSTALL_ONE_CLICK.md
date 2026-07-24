# 一键安装包使用说明

## 推荐用法

1. 在新电脑上解压安装包。
2. 双击 `install_one_click.bat`。
3. 按提示填写同事 API 地址和 API key；如果暂时不用 API，可以直接留空跳过。
4. 等待脚本自动完成 Python、虚拟环境、Python 依赖、`.env` 和离线验证。
5. 打开并登录 Windows 桌面微信。
6. 先双击 `start_dry_run.bat` 观察日志，确认回复内容没问题后再使用 `start_live.bat`。

如果新电脑还没有 Windows 桌面微信，可以改为双击 `install_one_click_with_wechat.bat`。它会先调用兼容微信安装脚本，仍然需要你手动完成微信安装界面和扫码登录。

如果安装时 API 地址或 key 填错了，或者当时跳过了 API 配置，安装完成后双击 `configure_api.bat`，重新粘贴真实的 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY` 即可。

如果 `start_dry_run.bat` 里出现 `wxauto4 could not attach`，说明当前微信版本或窗口状态不适配。可以双击 `install_wechat_compatible.bat`，输入 `YES` 或 `Y` 后回车。它会下载并启动归档的 Windows 微信 4.1.8.107 安装器。脚本会校验 SHA256，不通过就停止安装；如果安装器提示已有新版本，需要先手动卸载当前微信，再运行该脚本。

## 安装位置

默认安装到：

```text
%USERPROFILE%\Documents\tqy-wechat-auto-reply
```

桌面会创建这些快捷方式：

- `tqy-wechat-configure-api.lnk`
- `tqy-wechat-install-compatible-wechat.lnk`
- `tqy-wechat-dry-run.lnk`
- `tqy-wechat-live.lnk`

## 重要说明

- 安装脚本会自动安装 Python 3.11；如果电脑没有 winget，需要先手动安装 Python 3.11。
- 安装包会优先使用 `vendor\wheels` 里的依赖包；如果包内依赖不完整，pip 会联网下载缺失依赖。
- 如果安装时填写了同事 API，`.env` 会自动写入 `LLM_PROVIDER=openai_compatible`、`OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。
- `OPENAI_BASE_URL=你同事的API地址` 和 `OPENAI_API_KEY=你同事给的key` 是说明文字，不能原样填写；必须换成真实 URL 和真实 key。
- 如果同事接口地址以 `/chat` 结尾，程序会使用自定义 API 请求体：`api_key`、`session_id`、`message`、`preferred_language`。
- `install_wechat_compatible.bat` 只包含下载脚本，不内置微信安装包；它使用微信 Windows 版本归档中的 `v4.1.8.107` 安装器，并校验 SHA256：`b79aa3fdfd208d64f38fcc354684f9af213c05993c6f603521f2813930362b90`。
- 如果自动下载失败，脚本会打开浏览器下载链接。浏览器下载完成后，把下载好的 `.exe` 文件拖进黑窗口，再按回车，脚本会继续校验并安装。
- 不要一开始就用 live 模式。先用 dry-run 确认回复安全、白名单和人工确认策略都正确。
