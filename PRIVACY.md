# Privacy / 隐私说明

Codex Request Monitor is a local desktop diagnostic plugin. It opens the local Codex log and thread-index databases in read-only mode. It uses session names and identifiers, timestamps, transport tags, request sizes, and related diagnostic markers to render the dashboard. It extracts these fields from local logs and does not expose full log messages, prompts, request bodies, or credentials through the dashboard.

The plugin does not send analytics or diagnostic data to its maintainer or to any external service. Its desktop integration uses local stdio. The optional browser entry point only listens on the local loopback address. The plugin does not archive conversations or change any Codex setting.

插件只读访问本机 Codex 日志和会话索引。它在本机展示会话名称、会话标识、时间、传输方式、请求大小及相关诊断标记，不会把原始日志、提示词、请求正文或凭据展示在面板中。没有第三方遥测或外部上传。插件不会自动归档会话或修改 Codex 设置。

Network diagnostics runs when the page opens or regains focus, every 15 seconds while visible, and when you click “Check network”. On macOS, it inspects launch proxy variables in Codex-related processes, candidate proxy variable names in the configuration directory’s `.env` file, system proxy flags, and TUN interface names. It does not return raw process environments, command arguments, proxy credentials, PAC URLs, or NO_PROXY addresses. It may open and immediately close TCP connections to up to eight distinct loopback proxy endpoints found in that configuration, without sending application data. Remote endpoints are not probed, PAC scripts are not executed, and network settings remain unchanged. Results stay in the local browser or private MCP widget metadata; no diagnostic history is saved.

「网络诊断」在页面打开、重新获得焦点、保持可见时每 15 秒或点击「检查网络」时执行。macOS 上会检查 Codex 相关进程的启动代理变量、配置目录 `.env` 中的候选代理变量名、系统代理标志和 TUN 接口名称，不返回原始进程环境、命令参数、代理凭据、PAC 地址或 NO_PROXY 例外地址。最多对配置中八个不同的本地回环代理端口建立并立即关闭 TCP 连接，不发送应用数据。不探测远程地址、不执行 PAC 脚本、不改变网络设置。结果仅保留在本机浏览器或 MCP 组件私有元数据中，不保存诊断历史。

Observed-route checks read established Codex TCP socket metadata and use only GET `/connections` and GET `/proxies` on an existing local Clash/Mihomo Unix socket. Controller snapshots are filtered in memory to Codex service connections; only aggregate counts and route status reach the dashboard. Raw IP addresses, connection IDs, other apps' records, node names, process paths, and controller responses are not returned or saved. No credentials, packet contents, or TLS decryption are needed. These checks do not close connections or alter proxy settings.

实际路由检查只读 Codex 已建立的 TCP 连接元数据，并仅向已有的 Clash/Mihomo 本地 Unix 控制接口发送 GET `/connections` 和 GET `/proxies`。控制接口快照在内存中筛选，仅向面板返回 Codex 服务连接的合计数量和路由状态；不返回或保存原始 IP、连接 ID、其他应用记录、节点名、进程路径或控制接口响应。不读取凭据、不抓包、不解密 TLS，不关闭连接或改变代理设置。

Temporary proxy launch is a separate, explicit action. It can start an already-stopped Codex using proxy variables scoped to the child process. It never quits or kills Codex and does not write user configuration files. Running app processes or unconfirmed idleness block automatic restart. Before launch, it sends only an HTTP CONNECT request for the fixed target `chatgpt.com:443` through the identified loopback proxy, without credentials or user content. The loopback-only launch endpoint requires a same-origin POST and a single-use action token. No launch history is persisted. The sidebar action only opens the external browser page; it cannot restart Codex.

临时代理启动是独立、需主动点击的操作，仅为已退出的 Codex 创建带临时代理变量的新进程。工具不会退出或强制结束 Codex，也不写用户配置文件。应用仍运行或空闲无法确认时禁止自动重启。启动前会通过已识别的本地回环代理，仅发送固定目标 `chatgpt.com:443` 的 HTTP CONNECT 请求，不发送凭据或用户内容。仅监听回环地址的启动接口要求同源 POST 和一次性操作令牌，不保存启动历史。侧栏操作只打开外部浏览器网页，不能重启 Codex。

Language and system notification choices are stored in browser local storage for the current monitor address. In-page alerts remain active when system notifications are off.

语言和系统通知开关保存在浏览器当前监控地址的本地存储中；关闭系统通知后，页面内提醒继续显示。

Questions / 联系： [GitHub Issues](https://github.com/wolf3c/codex-request-monitor/issues)。请先移除真实会话信息、原始日志及凭据。
