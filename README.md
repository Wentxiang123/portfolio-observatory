# Portfolio Observatory · 持仓观测站

A local-first investment workspace for tracking portfolios, analyzing returns and monitoring risk across stocks, funds and crypto.

**v0.4.0** · 个人本地部署 · React / TypeScript / ECharts + FastAPI + PostgreSQL + SQLite authentication

## 功能

- 支付宝、moomoo、OKX 多平台来源，统一投资账户，按币种汇总。
- CSV / XLSX 导入、部分 OKX PDF 适配、候选记录核对、去重、原子入账与撤销。图片与未知 PDF 保留原文，需人工补录；尚未实现通用 OCR。
- 净持仓、移动加权成本、浮盈、已实现收益、费用与价格贡献、集中度及情景分析；条件不足时不输出虚构指标。
- OKX 7×24 小时参考行情，股票/ETF 交易所日历与延迟标记，支付宝基金最新已公布日终净值。行情不会改变持仓数量，买卖仍需手动导入。
- 本地用户名 / Argon2id 密码 + 标准 TOTP、二维码绑定、一次性恢复代码、限流、防重放和安全会话。
- Windows DPAPI 保护 TOTP 加密密钥；独立 Local Mode。SMTP 配置与旧实现保留，但当前不启用邮件认证。
- 冻结快照报告和审计记录。新闻采集、自动外部推送、完整日度收益序列尚未实现。

## Windows / VS Code 启动

需要 Python 3.12、Node.js 24、PostgreSQL 17 与 VS Code。PostgreSQL 默认安装目录为 `C:\Program Files\PostgreSQL\17\bin`；其他路径请调整 `scripts/setup.ps1`。

```powershell
git clone --branch release/v0.4.0 https://github.com/Wentxiang123/portfolio-observatory.git
cd portfolio-observatory
py -3.12 -m venv services/api/.venv
.\services\api\.venv\Scripts\python.exe -m pip install -r services/api/requirements-dev.txt
cd apps/web
npm.cmd ci
cd ../..
```

打开 `个性化持仓量化分析平台.code-workspace`，执行 VS Code 任务 **平台：启动全部**，访问 http://127.0.0.1:5173 。初始化创建项目私有 PostgreSQL，默认端口 55432；不会改动系统默认 5432 数据库。首次安装为空工作空间，不包含真实或演示持仓。

首次选择「注册」，设置用户名和至少 12 位密码，使用 Microsoft / Google Authenticator 扫码并验证，再安全保存只展示一次的恢复代码。已有私有安装迁移应选择「原账户升级」；不要将新注册当作旧账户恢复。

完整步骤见 [本地认证与恢复指南](本地认证与恢复指南_v0.4.md)。日常开发可单独运行 API、Worker 和 Vite；具体命令见 `.vscode/tasks.json`。

## 数据与安全

认证保存在本地 SQLite；账本、报价、报告保存在本地 PostgreSQL。原始文件和认证密钥位于 `var/private`，数据库连接配置位于 `.env`。这些文件以及真实账单、个人研究报告、恢复代码均不随仓库分发。

账户模式默认只监听 localhost，持仓等接口需要完成二步认证。Local Mode 是用户主动选择的免网页登录模式，以操作系统访问权限作为信任边界。本版不面向公网或多租户服务。

DPAPI 密钥通常绑定原 Windows 用户及系统环境，复制 SQLite 和密钥文件到另一台机器不保证可恢复。请同时保护系统备份、数据备份与恢复代码；Docker 不替代密钥管理或磁盘加密。

同币种汇总，不将 USDT 自动视为 USD，也不产生未配置汇率的跨币种总资产。公开股票行情可能延迟；本项目不提供交易所授权实时行情承诺。量化结果用于个人复盘，不进行自动交易。

## 验证

启动项目数据库后，在项目根目录运行：

```powershell
.\services\api\.venv\Scripts\python.exe -m pytest tests -q --tb=short
cd apps/web
npm.cmd run build
npm.cmd run lint
```

测试使用随机 PostgreSQL schema 与临时 SQLite，测试凭据均为生成或虚构值。公开版本移除了使用私人账单的黄金样例及真实持仓断言。直接依赖版本记录在 `services/api/requirements.lock.txt`；前端依赖使用 `package-lock.json`。

## 版本与目录

源码位于隔离分支 `release/v0.4.0`；`main` 暂为仓库入口，通过 PR 审阅后合并。见 [VERSIONING.md](VERSIONING.md)。这是私有开发工作空间的脱敏发布副本，未上传私人工作目录或 Git 历史。

- `apps/web`：前端。
- `services/api/app`：认证、行情、账本、API 与 Worker。
- `migrations`：PostgreSQL 迁移。
- `tests`：财务和认证回归测试。
- `scripts`：本地初始化、备份升级与数据库密码维护。

不同版本部署应使用独立配置、密钥和数据库；Git 分支隔离不等于运行数据隔离。
