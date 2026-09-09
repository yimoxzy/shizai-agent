# 仓库结构与发布路径

## 源码在哪里

当前项目的唯一运行时代码目录是：

```text
D:\agent\indeed_mcp\shizai_agent\
```

主要入口：

| 路径 | 作用 |
| --- | --- |
| `shizai_agent/shizai_agent_cli.py` | `shizai-agent` CLI 入口 |
| `shizai_agent/core/mcp_server.py` | `shizai-agent-mcp` stdio MCP 入口 |
| `shizai_agent/core/project.py` | RPA 项目、组件、元素、任务和校验核心 |
| `shizai_agent/core/component_catalog.py` | 实时组件目录和契约解析 |
| `shizai_agent/core/extension_bridge.py` | Chrome DeepCapture 和浏览器会话桥接 |
| `shizai_agent/core/debug_session.py` | 原生 RPA 单步调试会话 |
| `tests/` | 单元、CLI、MCP 和真实环境回归测试 |
| `docs/` | 开发和运行契约文档 |
| `skills/shizai-agent/` | 给模型加载的 Skill |
| `npm/shizai-agent-mcp/` | npm/npx 安装器，不是 MCP 业务代码 |

`build/`、`dist/`、`shizai_agent.egg-info/` 和 `release/` 下的构建目录都是生成物，
不能作为源码入口。保留的业务结果和排错证据放在被 Git 忽略的
`archive/retained-evidence-<date>/`；旧构建包和探针在确认无用后直接清理，不作为运行时依赖。

## 版本来源

Python 包版本以 `pyproject.toml` 为准，运行时/服务版本以 `shizai_agent/__init__.py` 的
`__version__` 为准。Windows 安装脚本、发布说明和 npm launcher 的默认下载版本必须与当前
发布版本同步。设计器内部的 `factoryVersion`、组件版本和 RuntimeHost 版本不是本项目版本，
不要修改它们来表示发布版本。

## 如何出包

在仓库根目录执行：

```powershell
.\scripts\build-release.ps1 -Python C:\path\to\python.exe
```

脚本会调用两个 PyInstaller spec，生成：

```text
release\shizai-agent-v<VERSION>-windows-x64.zip
release\shizai-agent-v<VERSION>-windows-x64.zip.sha256
```

PyInstaller 的 `build/`、`dist/` 和 work 目录写入系统临时目录，不会再次污染仓库根目录。
ZIP 只包含 `shizai-agent.exe`、`shizai-agent-mcp.exe`、安装说明和 Codex 注册脚本。

## 目录约束

- 只在 `shizai_agent/`、`tests/`、`docs/`、`skills/` 和 `npm/` 修改源文件。
- `release/` 根部只保留发布脚本、入口文件、当前最终 ZIP、SHA-256 和发布说明。
- 临时 probe、旧版本 ZIP、PyInstaller 中间目录和旧 smoke 输出移动到 `archive/`，不提交。
- GitHub 只发布最终 Windows ZIP 和 `.sha256`；Gitee 保存源代码和发布脚本。
