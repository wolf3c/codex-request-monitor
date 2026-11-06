# Codex Request Monitor

[简体中文](README.md)

![See HTTP or WebSocket by session, inspect compressed HTTP request sizes, and spot explicitly logged non-incremental events. Uses synthetic data.](docs/overview.en.svg)

Codex Request Monitor is a local, read-only dashboard for diagnosing slow requests and unexpected uploads. It shows the transport observed for each Codex session, logged HTTP request body sizes, and explicitly logged non-incremental WebSocket events.

## Install and use

The macOS beta requires Python 3.9 or later. The current source opens the dashboard in Chinese for Chinese locales and in English otherwise. Use the language selector to switch at any time. Session titles stay in their original language. The pinned `v0.1.0-beta` release below predates the language selector.

### 1. Terminal installation (recommended)

Requires Codex desktop and a Codex CLI with the `plugin` command.

```sh
codex plugin marketplace add wolf3c/codex-request-monitor --ref v0.1.0-beta
codex plugin add codex-request-monitor@codex-request-monitor-marketplace
```

### 2. Install from the desktop app

1. Open **Plugins → Add plugin marketplace**.
2. Set **Source** to `wolf3c/codex-request-monitor` and **Git ref** to `v0.1.0-beta`.
3. Click **Add marketplace**, find the monitor, and click **Install**. The original beta lists it as **Codex 请求监控**.

### Open the installed dashboard

Open the monitor from the **⋯** menu in the desktop sidebar. Use the pin to keep it there. If the entry is missing, reopen the desktop app after your running tasks finish.

### Run in a browser

Run this command from the repository root. If a browser tab does not open, use the local address printed in the terminal.

```sh
python3 plugins/codex-request-monitor/monitor.py
```

## Read the dashboard

- The overview shows large HTTP requests and active non-incremental WebSocket alerts from the last five minutes. Expand the 24-hour summary for historical counts.
- Expand a session to see up to ten recorded events and the 24-hour sum of logged compressed HTTP request bodies. This sum reflects prepared request bodies. A failed request may have sent fewer bytes.
- A WebSocket connection alone does not establish that a request was incremental. Missing WebSocket or download sizes appear as a dash.
- The HTTP session details include a manual WebSocket retry guide. Archiving may interrupt the session and its subtasks.

## Privacy and compatibility

The monitor reads local Codex SQLite diagnostics and sends no monitoring data to an external service. See the [privacy policy](PRIVACY.md). Log availability depends on the Codex version. Include the OS, Codex version, and a redacted description when filing an issue.

## Run the tests

```sh
cd plugins/codex-request-monitor
python3 -B -m unittest
```
