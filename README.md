# Turb GPT Register

> 碰到问题欢迎开 Issue 或提 PR。把报错、运行环境和复现步骤带上，通常比一句“不能用”更容易把问题解决。

**Turb GPT Register** 是一套在本机运行的注册流程工具，基于 [gpt-free-register](https://github.com/xiaoguzuiniu/gpt-free-register) 整理和扩展。它把邮箱、代理和注册流程放在同一个地方管理，既能走协议驱动，也能接本地指纹浏览器。你可以从命令行启动，也可以打开 WebUI 配置和管理任务。

这类流程牵涉的服务不少：邮箱提供商会改接口，代理可能抽风，浏览器环境也各有脾气。所以项目把这些部分拆成配置和驱动，方便按自己的环境接起来。听上去很优雅，实际碰到第三方服务变更时，该报错还是会报错；遇到问题先看日志和配置，别指望程序能隔空猜中是哪一环掉链子。

## 怎么跑起来

建议使用 Python 3.11。在仓库根目录打开 PowerShell：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python web.py
```

启动后在本地 WebUI 里填自己的邮箱服务和代理配置。`.env.example` 只是配置样例，照着改成自己的 `.env` 就好，真实凭据别往样例文件或 Git 里塞。

协议注册组件放在 `vendor/gpt-register-tool/协议`。想单独运行它，就按那个目录里的说明装对应依赖。浏览器驱动也可能需要额外的浏览器或供应商客户端；`pip install` 不会替你把这些东西变出来。

## 里面有什么

主程序在 `core/`，配置在 `config/`，本地管理页面在 `webui/`，一些辅助脚本放在 `tools/`。协议注册组件连同它自己的文件放在 `vendor/gpt-register-tool/协议`。入口不多，按目录找通常比满仓库搜索来得快。

项目会把运行配置和结果留在本机。邮箱池、代理凭据、账号数据库、浏览器资料、日志和导出文件都属于自己的运行数据，别提交进仓库。`.gitignore` 已经挡住了常见的数据文件，但提交前扫一眼总没坏处——Git 不会因为文件名看起来无辜就替你保密。

其中两份文件要特别注意：`turb.sqlite3` 里的 `access_token`、`refresh_token`、`totp_secret`、cookie 都是**明文**，`.env` 里的代理、接码、CPA、Sub2API 凭据同样是明文。它们等同于私钥——别放共享盘、别截图发群、别打进压缩包。本项目不做本地加密，安全性靠你自己把这两份文件的权限管住。

## 几个容易踩的点

- **TLS 校验默认开着。** 邮箱取码、flow 触发这些自建网关以前是写死 `verify=False` 的，现在统一走 `config.email.MAIL_TLS_VERIFY`（默认 True）。真碰到只认自签证书的网关，在 `.env` 里设 `MAIL_TLS_VERIFY=False` 显式关掉——那等于把验证码通道摊开给中间人，日志里会打一条 warning 提醒你。
- **「零元提链」是可拆卸组件。** `core/upi_zero_web_service.py` 与 `core/upi_live_feed.py` 不在本源码分发里；缺件时 WebUI 照常启动，相关的 `/api/upi-zero/*` 接口返回 503 并说明缺哪个文件，而不是整个页面起不来。
- **WebUI 只认本机。** 默认绑 `127.0.0.1`，授权码可以用 `.env` 的 `WEBUI_AUTH_CODE` 固定下来；不填就每次启动生成临时码并打印在日志里，重启即失效。用 `--host 0.0.0.0` 暴露到局域网之前先想清楚，这工具手里握着的是账号明文。

## 自检与测试

改完代码提交前，三件事跑一遍就够了：

```powershell
python tools/check_imports.py         # core/config/webui/tools 里的引用是否都能落地
python tools/gen_env_example.py       # 重新生成 .env.example（改过配置白名单就要跑）
python -m pytest -q                   # 单元测试
```

`tools/check_imports.py` 值得单独说一句：清理源码分发时最容易犯的错不是忘了删文件，而是删了模块但调用点还在——`core/upi_live_feed.py`、`core/mailcom_client.py`、`tools/export_session_cookies.py` 都这么悬空过，其中第一个直接让 `python web.py` 起不来。这个脚本被 `tests/test_imports.py` 调用，CI 里也会跑。

`.github/workflows/ci.yml` 在 push / PR 时于 Ubuntu 与 Windows 上执行：语法编译、引用完整性、`.env.example` 同步、pytest。

### 目录约定

| 目录 | 放什么 |
|---|---|
| `core/` | 业务实现：SQLite 持久化、注册/授权驱动、邮箱与短信渠道、流量统计 |
| `config/` | 默认值与 `.env` 覆盖（`config.env_loader`）；WebUI 只写 `.env` |
| `webui/` | Flask 控制台：`app.py` 路由 + `config_editor.py` 配置读写 + `auth.py` 授权 |
| `tools/` | 独立脚本：代理桥、cookie 落盘、环境守卫（`check_imports.py`）等 |
| `sentinel/` | Node 侧 Sentinel 运行时（`sentinel-runner.js` + 环境画像数据） |
| `tests/` | pytest 用例；`conftest.py` 会把 SQLite 指到临时目录，不碰真实库 |
| `vendor/gpt-register-tool/协议` | 上游协议注册组件，保留其原许可证 |

## 来源与许可

主项目基于 [xiaoguzuiniu/gpt-free-register](https://github.com/xiaoguzuiniu/gpt-free-register) 改造，按 MIT License 发布，见 [LICENSE](LICENSE)。`vendor/gpt-register-tool/协议` 里的组件保留各自上游的许可证和版权说明，使用前也记得看一眼。

请只在自己有权使用的环境里运行，并遵守相关服务、邮箱和代理提供方的规则。这个项目负责把流程接起来，不会附送账号、邮箱、代理，也不会替第三方服务承诺可用性。
