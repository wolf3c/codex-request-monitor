# Privacy / 隐私说明

Codex Request Monitor is a local desktop diagnostic plugin. It opens the local Codex log and thread-index databases in read-only mode. It uses session names and identifiers, timestamps, transport tags, request sizes, and related diagnostic markers to render the dashboard. It extracts these fields from local logs and does not expose full log messages, prompts, request bodies, or credentials through the dashboard.

The plugin does not send analytics or diagnostic data to its maintainer or to any external service. Its desktop integration uses local stdio. The optional browser entry point only listens on the local loopback address. The plugin does not archive conversations or change any Codex setting.

插件只读访问本机 Codex 日志和会话索引。它在本机展示会话名称、会话标识、时间、传输方式、请求大小及相关诊断标记，不会把原始日志、提示词、请求正文或凭据展示在面板中。没有第三方遥测或外部上传。插件不会自动归档会话或修改 Codex 设置。

Questions / 联系： [GitHub Issues](https://github.com/wolf3c/codex-request-monitor/issues)。请先移除真实会话信息、原始日志及凭据。
