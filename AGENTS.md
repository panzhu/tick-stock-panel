# AI 开发入口

修改、调试或审查本仓库前，必须完整阅读并遵循根目录的 [`CONTRIBUTING.md`](CONTRIBUTING.md)。其中定义了项目架构、数据契约、数据源插件化、缓存与性能要求、测试矩阵以及 PR 复审和合并标准。

涉及代码二次开发、前端插槽、后端可替换策略、扩展注册或上游升级兼容时，还必须阅读 [`docs/secondary-development.md`](docs/secondary-development.md)。该文档区分当前已实现能力与目标扩展契约；不得根据设计示例虚构尚不存在的 API。

同时遵守以下规则：

- 先理解调用链和现有测试，再进行修改。
- 保持实现简单、改动范围最小，不处理无关问题。
- 不覆盖工作区已有修改，不虚构测试或审查结果。
- 以实际验证结果作为完成标准。

---

## 常用命令

| 目的 | 命令 | 说明 |
| --- | --- | --- |
| 一键起前后端 (Windows) | `.\dev.ps1` | 后端 3018、前端 3011；`-NoReload` 关热重载，`-Force` 结束已有实例。Ctrl-C 关两端，日志在 `data\logs\` |
| 一键起前后端 (macOS/Linux) | `./dev.sh` | `BACKEND_PORT=8000 ./dev.sh` 改端口；首次自动 `uv sync --frozen` 与 `pnpm install` |
| 装后端依赖 | `cd backend && uv sync --extra dev --frozen` | 后端统一用 `uv`；老 CPU 加 `--extra legacy-cpu`，回测可选 `--extra backtest` |
| 跑单个后端测试 | `cd backend && uv run pytest tests/test_api_gateway.py -q` | 单用例追加 `::test_name` 或用 `-k "关键字"`；子目录 `tests/backtest/`、端到端同理 |
| 跑后端全量测试 | `cd backend && uv run pytest tests -q` | CI 就是这个命令；提交前至少跑通受影响模块 |
| 后端静态检查 | `cd backend && uv run ruff check app/xxx.py tests/test_xxx.py` | 行宽 100；不允许为历史告警做全仓格式化 |
| 前端依赖/开发 | `cd frontend && pnpm install` / `pnpm dev` | 包管理器固定 `pnpm@9`，不要用 npm/yarn |
| 前端构建 (必过) | `cd frontend && pnpm build` | 等价 `tsc -b && vite build`；任何 TS/组件/样式/API 类型改动至少跑一次 |
| 前端 lint | `cd frontend && pnpm lint` | ESLint |
| 跑单个前端测试 | `cd frontend && pnpm vitest run src/pages/Indices.test.tsx` | Vitest 无配置文件，环境由测试文件首行 `// @vitest-environment jsdom` 指定 |
| Docker 部署 | `docker compose up -d --build` | 单容器，数据卷挂 `data/`；生产镜像把前端 `dist` 交给 FastAPI 托管 |
| 提交 | `git commit -s -m "fix(scope): ..."` | 必须 DCO 签名，否则 CI 拦截 |

其他入口：`mcp-server/`（MCP 服务）、`packaging/`（PyInstaller 桌面版）、`scripts/`（版本同步与升级检查）、`backend/scripts/`（压测与数据运维一次性脚本，非应用入口）。

---

## 架构总览

### 形态

单进程 FastAPI 后端 + React SPA，无外部数据库：全部行情、因子、回测结果落在本地 Parquet（`data/`，不入库、不提交）。生产态由 FastAPI 直接托管 `frontend/dist` 并做 SPA fallback；开发态 Vite 把 `/api`、`/health`、SSE 代理到后端。前端 `api.ts` 始终用同源相对路径，不要在组件里散落 URL。

### 数据主链路

```
数据源插件 / TickFlow SDK
  → data_providers 归一化（单位、日期、代码）
  → kline_sync / minute_refresh / financial_sync 落 Parquet (DataStore)
  → indicators 富化管线 → kline_daily_enriched（原子发布 + generation 世代）
  → strategy / monitor / backtest / analysis 服务
  → FastAPI / SSE → 前端 api.ts + TanStack Query
```

禁止页面或 API 层绕过 provider、repository、service 直读数据源或本地文件。

### 后端分层（`backend/app/`）

- `main.py`：唯一的装配点。双层 lifespan 先取 `MiningProcessLock`（拿不到则降级启动），再按序初始化并挂到 `app.state`（`repo/datastore/capabilities/quote_service/monitor_engine/strategy_engine/scheduler/...`）。**依赖注入靠 `request.app.state.*` 与 `services.preferences` 的 getter，不是 FastAPI `Depends`**，改路由时要跟着走这条路。启动含 enriched 后台预热、APScheduler 每日流水线、常驻行情线程、watchdog；`auth_middleware` 只拦 `/api/`（白名单含 `/api/auth/*`、`/health`、`/api/events`）。
- `api/`：40+ 个扁平路由模块，各自定义 `APIRouter(prefix="/api/...")`。保持薄：不做重算、全表扫描、数据源专有逻辑。开放 API 走 `services/api_gateway.py` 的 `_RULES` 表（method+前缀 → scope），**规则表即对外契约**，改路由要同步检查它。
- `tickflow/`：`DataStore`（目录布局 + DuckDB 内存视图）与 `KlineRepository`（三级 polars 缓存：最新日 / 历史窗口 / ETF·指数）。写盘后必须处理缓存、视图、generation、SSE 与前端失效。
- `data_providers/`：provider 协议 + 归一化 + `capabilities.py` 能力注册表。能力路由矩阵（`build_capability_matrix` → `/api/settings/capability-matrix`）是唯一真源，页面按 `usable` 判定，前端不得硬编码能力列表。
- `indicators/`：向量化富化管线（指标 → 信号 → 涨跌停/连板 → enriched → 选列），写盘经 `enriched_generation` 原子发布（心跳 + 陈旧回收），世代 drift 会丢弃缓存并触发有界重建。
- `strategy/`：策略热加载（`app/strategy/research` + `data/strategies/{custom,ai,composite}`），内置与自定义共用执行契约；`monitor.py` 负责盘中规则评估与通知。
- `backtest/`：信号生成与成交模拟严格分离（禁止前视偏差）；矩阵/挖掘走 `worker.py` 的 `spawn` 子进程（任务 dict 必须可 pickle），带峰值内存采样，保证确定性退出。
- `plugins/`：数据源插件目录，每个 `plugin.yaml` + `provider.py`，由 `data_providers/custom/loader.py` 动态发现。
- `extensions/`：后端扩展加载器，扩展路由注册在核心路由之后，禁止覆盖核心路径。

### 前端分层（`frontend/src/`）

- `main.tsx` 的 bootstrap 顺序有依赖：`hydrateUiPrefs` → `initializeFrontendExtensions` → `installTspExtBridge` → `loadRuntimePageExtensions` → 才动态 `import('./router')`（路由快照在此时冻结）。路由表在 `router.tsx`（`CORE_ROUTE_PATHS` 为核心白名单），页面全部 `lazy()`。
- `lib/api.ts` 是唯一请求入口（含 `ApiError`、超时、422 展平）；`lib/queryKeys.ts` 集中管理 `QK`，SSE 失效按前缀匹配，新增 key 不必改 SSE 代码；`useSharedQueries.ts` 提供跨页共享查询。
- 刷新以 SSE 为主：`Layout` 全局唯一挂载 `useQuoteStream`，按前缀 `invalidateQueries`；只有非 SSE 场景才轮询。
- 扩展三层：源码扩展 `src/custom/<ns>/extension.tsx`（`import.meta.glob` 自动发现，删目录即卸载）、运行时页面包 `/api/ext-pages/{id}/bundle.js` 动态 import（`createMountComponent` + `ExtensionBoundary` 隔离）、插槽 `ExtensionSlot`。

### 关键不变量（详见 CONTRIBUTING §3）

比率/百分比、复权价 vs 原始价、自然日 vs 交易日、北京时间（分钟线是 naive 墙钟时间，禁止存 UTC）各有固定口径；跨边界新增映射必须补单元测试，禁止「小于 1 就乘 100」这类启发式转换。

### 测试约定

后端 `backend/tests/` 扁平约 350 个 `test_*.py`（`tests/backtest/` 为子进程与矩阵专项），**没有 `conftest.py`**：各测试自建 `tmp_path`、`monkeypatch.setattr(settings, ...)`、`TestClient`。改配置相关代码时注意这一点。

### 已知坑位

- `polars` 锁在 `>=1.44,<1.45`（并发死锁回归），升级必须重跑 `backend/scripts/stress_polars_concurrency.py`。
- Tailwind 暗色用 `html.dark` + CSS 变量；`text-base` 与字号 token 撞名，**禁用**，改写 `text-[16px] leading-6`；图表配色统一走 `useChartTheme()`。
- 页面包自包含 React 且宿主按需编译 Tailwind，页面包需内联样式，不能假设类名存在。
- 每次 `--reload` 重启都会重新做 1GB+ 的 enriched 预热，长时间内测用 `-NoReload`。
