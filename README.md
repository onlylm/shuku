# 书库 · 网站与电子书整理工具

一个面向个人运营者的电子书/教程资源索引站。当前支持百度网盘、夸克网盘，其他网盘通过适配器扩展。系统不负责破解或抓取网盘内容，只管理已取得授权、公版或开放许可资源的元数据和分享入口。

## 阶段一已实现

- 前台：首页、搜索、分类、合集、资源详情、动态网盘按钮。
- 后台：登录、资源管理、分类管理、渠道状态、链接检测、表格导入预检、双网盘上传队列。
- 规模适配：全部资源、分类、搜索、后台资源和链接列表均支持分页；入口数量采用批量统计。
- 前台展示：缺失作者、出版方、简介等字段时不显示整理占位词，语言代码自动显示为中文名称。
- 导入：支持 XLSX/CSV，一次最多 500 行，自动识别百度/夸克链接。
- 防错：同批重复、全库重复和“链接已属于另一本书”的冲突拦截；相似书名只提示人工确认。
- 链接闭环：新链接默认隐藏；检测有效才显示；明确失效立即隐藏；临时网络异常达到阈值后隐藏；后台替换并复检通过后自动恢复。
- 自动巡检：可按时间间隔分批检测到期链接，不依赖 Redis 或独立任务队列。
- 自动上传：扫描本地 EPUB/MOBI/AZW3/PDF/TXT/DOCX，按文件名匹配图书，分别上传百度和夸克并自动创建、去重、检测和回填分享链接。
- 数据：跳转点击记录、零结果搜索记录、操作日志、检测历史。
- SEO：canonical、Book 结构化数据、robots.txt、sitemap.xml、搜索结果 noindex。
- 正式部署：FastAPI + SQLAlchemy + Alembic + MySQL 8.4 + Caddy 自动 HTTPS + Docker Compose。

## 本地运行

项目已包含本地虚拟环境时，在 PowerShell 中执行：

```powershell
cd D:\网盘拉新
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m scripts.seed_demo
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

首次从零安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\scripts\init_local.ps1
```

浏览器打开 `http://127.0.0.1:8000/`。本地演示账号默认为 `admin / ChangeMe123!`，仅用于本地测试，上线前必须修改。

## 双网盘上传工具

后台“云盘上传”适合一次处理几十到几百本：

1. 在 `.env` 的 `CLOUD_UPLOAD_SOURCE_ROOTS` 填写允许扫描的本地目录，多个目录用英文分号分隔。
2. 百度网盘使用开放平台 OAuth 与官方上传/MCP 分享能力，令牌只放在 `BAIDU_NETDISK_ACCESS_TOKEN` 环境变量，不写网页和数据库。
3. 夸克网盘在后台点击“安装官方连接器”，再点击“授权夸克账号”；授权由夸克官方页面完成。
4. 填写本地文件或文件夹路径并扫描，选择百度、夸克或两边同时上传。
5. 每个文件单独生成分享链接。未匹配文件可自动建立草稿；链接通过检测后才允许前台显示。

开发和测试不会自动上传任何已有文件。只有管理员主动把选中的文件加入队列、且对应网盘已授权后，才会读取并上传这些文件。

## 正式录入流程

1. 从后台下载 CSV 模板，填写书名和网盘链接；副标题、作者、ISBN、出版方、出版年份、分类、提取码、格式、语言、简介、版权状态与来源说明均可选填。
2. 上传表格后先看预检页。绿色可导入，黄色需人工确认，重复/冲突/错误行不能导入。
3. 确认导入后，资源可入库，但链接仍是 `pending` 且前台隐藏。
4. 在“链接检测”或资源编辑页执行检测；有效自动显示，无效继续隐藏。
5. 链接失效时在资源编辑页替换，新链接会立即检测并按结果恢复或保持隐藏。

表格模板：[app/static/samples/import-template.csv](app/static/samples/import-template.csv)

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试覆盖链接标准化、提取码变化下的重复识别、同批重复、跨资源错链、导入提交、分页、本地文件扫描、图书匹配、双网盘任务去重、前台空字段处理、失效隐藏、自动巡检、有效恢复、跳转记录、零结果搜索和 SEO 端点。

## 服务器部署

正式域名没有预设。上传 GitHub 后，在 Linux 的项目根目录运行：

```bash
sudo bash deploy.sh
```

按提示填网站域名和名称。脚本支持在全新 Ubuntu 22.04/24.04、Debian 12/13 上经确认安装 Docker，随后生成独立配置、创建随机密码、运行迁移、初始化管理员、申请 HTTPS 并检查页面。已有 Docker 的 Linux 可直接运行；缺少 Python 3.10+ 时需先安装。

完整操作、GitHub 上传注意事项、备份和恢复见 [Linux 一键部署指南](docs/Linux一键部署指南.md)。

- 正式配置：`deploy/.env`（脚本生成，已忽略，不上传 GitHub）。
- 配置样例：`deploy/.env.example`（域名、密钥留空）。
- 升级：`sudo bash deploy.sh update`；备份：`sudo bash deploy.sh backup`。
- 保留本地根目录 `.env` 和 SQLite 数据；正式服务器首次启动为空书库，不携带本地书籍或凭据。
- 旧的根目录 `docker-compose.yml` 保留给既有手动部署使用。新部署只用 `deploy.sh`，两套入口不要混用。
- GitHub Actions 会检查网站测试、Linux 容器构建、MySQL 迁移和备份；只有实际运行成功才算容器验收通过。

## 目录说明

- `app/`：后端、页面模板、样式、导入与网盘适配器。
- `alembic/`：数据库迁移。
- `scripts/`：初始化、演示数据和管理员维护脚本。
- `tests/`：自动化测试。
- `docs/`：阶段零确定的产品、架构、数据库、接口和导入设计。
- `prototype/`：阶段零视觉原型，保留作为设计基准。

## 本地电子书整理工作台（2026-08-31）

- 完整开发计划：`docs/后续开发与上线总计划.md`。
- 操作指南：`docs/本地整理软件使用说明.md`；验收边界：`docs/本地整理软件验收报告.md`。
- 整理软件源码：`ebook_organizer/`；Windows便携版：`dist/EbookOrganizer/EbookOrganizer.exe`。
- 开发启动：`scripts/start_organizer.ps1`；构建：`scripts/build_organizer.ps1`；锁定环境：`requirements-organizer-win.lock`。
- 网站对接后台：`/admin/organizer`；接口契约：`docs/schemas/organizer-v2.schema.json`；新增迁移：`20260831_0004`。

本地读取/分类/封面/导出不需要联网。R2、百度及正式站点仍需配置和真实联调；夸克官方连接器有Agent环境限制，不能将开发环境成功等同于普通桌面全自动可用。

## 阶段一边界

- 适合单人、一次几百本的处理规模，未引入 Redis、Celery 或复杂消息队列。
- 已提前接入阶段四的本地上传任务队列：百度和夸克均只使用官方授权能力，不保存账号密码，不调用网页私有接口；正式使用仍需各平台账号授权和服务器环境配置。
- 自动检测是降低人工负担的辅助机制，网盘反爬或登录页可能导致误判，因此网络异常采用连续失败阈值，后台仍保留检测状态、立即检测和人工替换入口。
