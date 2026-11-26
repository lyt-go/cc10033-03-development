# 分布式采集调度与反封锁平台

面向分布式采集任务调度、代理池管理、反封锁策略与结果质量校验的数据采集平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 采集任务 | `crawl_mission` | 采集任务 | 任务编号、目标URL、优先级 |
| 代理节点 | `proxy_node` | 代理节点 | 节点编号、IP地址、端口号 |
| 频率控制 | `rate_limit` | 限速策略 | 策略编号、目标域名、请求间隔 |
| 设备指纹 | `fingerprint` | 指纹模板 | 模板编号、浏览器类型、UserAgent |
| 验证码处理 | `captcha_solve` | 验证码记录 | 记录编号、验证码类型、图片数据摘要 |
| Cookie管理 | `cookie_jar` | Cookie容器 | 容器编号、所属账号、域名范围 |
| 页面渲染 | `page_render` | 渲染记录 | 记录编号、渲染引擎、页面URL |
| 抽取规则 | `extract_rule` | 抽取规则 | 规则编号、目标页面、抽取方式 |
| 种子URL | `seed_url` | 种子URL | 种子编号、URL地址、所属站点 |
| 内容去重 | `content_dedup` | 去重记录 | 记录编号、内容指纹、原始URL |
| 结果解析 | `result_parse` | 解析记录 | 记录编号、来源页面、解析方式 |
| Schema匹配 | `schema_match` | 匹配配置 | 配置编号、来源站点、目标Schema |
| 调度计划 | `scheduler` | 调度计划 | 计划编号、Cron表达式、关联任务组 |
| 数据导出 | `export_dataset` | 导出任务 | 任务编号、数据来源、导出格式 |
| 质量抽检 | `quality_check` | 抽检任务 | 任务编号、数据批次、抽检比例 |
| 机器人校验 | `robot_verify` | 校验日志 | 日志编号、检测页面、触发规则 |
| 投递管道 | `delivery_pipe` | 投递配置 | 配置编号、目标系统、投递方式 |
| 弹性伸缩 | `scalable_pool` | 伸缩策略 | 策略编号、Worker类型、最小实例数 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA

## 交接放行回执（导出 → 投递核销）

导出批次生成/文件完成后，还必须有交接负责人针对「确定的批次版本 + 接收方」
发起的放行回执，投递服务才能在投递管道的待发送行核销并启动投递。下载预检、
导出完成都不能代替放行。

### 业务规则

- 批次更正产生新版本（`R1 → R2`），旧版本与旧回执、旧签收记录原样保留、可查证；
  但旧许可身份不再是「当前有效放行」，不能再被核销启动新投递。
- 回执作用域唯一指向 `批次(batch_key) + 版本(revision) + 接收方(receiver)`；
  新接收方不能借用旧接收方的许可。
- 领取/签收按 `(回执, 接收方)` 与 `request_id/idempotency_key` 幂等，重复提交原
  领取请求返回同一领取/许可身份。
- 核销与更正/领取之间用进程内锁做判定+落库（CAS），并发下只有当前有效放行能被核销。
- 所有判定响应都带回执「许可身份」(`permit_id`)、`can_send/sendable`、
  `needs_reconfirm`、`code`（如 `VERSION_SUPERSEDED` / `RECEIVER_MISMATCH` /
  `RELEASE_REQUIRED` / `REDEEMED`）以及当前版本/当前回执，调用方据此判断能否发送、
  是否还要请负责人复核确认。
- 签批结果会回写到投递待发送行的「签批结果」列；签批返回时若版本已变，导出页会
  提示需要复核的新版本，同时仍可打开原批次回执（支持 `?receipt=<permit_id>` 定位）。

### 页面

- 数据导出 `/export_dataset`：顶部「交接放行 · 放行回执」面板，选择批次与接收方后
  发起放行、更正批次、领取/签收、签批、查看旧签收与原批次回执。
- 投递管道 `/delivery_pipe`：顶部「放行核销 · 待发送行」面板，按 R1/R2 版本提交、
  在待发送行录入许可身份后核销或启动投递，并用「实际可发送 / 实际不可发送」标记两版。

### 主要接口（均有中英文/单复数别名，详见 `app/routers/release.py`）

```text
POST /api/release/receipts           发起放行（batch/export_id + receiver）
GET  /api/release/receipts[/{id}]    回执列表/详情（含效力判定）
POST /api/release/correct            更正批次生成 R2
POST /api/release/claim              领取/签收（幂等）
POST /api/release/approve            签批通过/驳回（回写投递待办）
POST /api/release/redeem             投递服务核销
POST /api/delivery/start             核销并启动投递（可按 batch+revision+receiver）
GET  /api/release/evaluate           只判定当前能否发送
GET  /api/delivery/todos             投递待发送行（含回执标记与判定）
POST /api/release/precheck           下载预检（始终提示 release_required）
GET  /api/release/overview           批次/回执/签收/待办/投递总览
```

通用动作接口同样支持：对 `export_dataset` / `delivery_pipe` 等模块
`POST /api/{module}/{id}/actions`，`action` 可取
`发起放行 / 更正批次 / 领取 / 签收 / 签批通过 / 驳回 / 核销 / 启动投递`，
附加字段（`receiver/permit_id/request_id/revision/...`）会一并传入状态机。

