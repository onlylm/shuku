# 系统架构设计（阶段0）

> 状态：待验收  
> 范围：个人运营、单次几十至几百本、先本地测试后部署至 Linux 服务器

## 1. 架构目标

- 用最少的运行组件完成“表格导入—审核—发布—搜索—多渠道跳转—链接维护”闭环。
- 资源核心模型与百度、夸克解耦，未来新增平台无需修改资源表和前台结构。
- 重复链接、跨资源错链和未经审核的链接不得进入前台。
- 第一版不引入 Redis、Celery、微服务或多节点部署。
- 本地 Windows 与服务器 Linux 使用相同应用代码，路径、域名和凭据全部配置化。

## 2. 逻辑架构

```text
访客浏览器
  → Nginx（生产环境 HTTPS、静态资源、反向代理）
  → FastAPI
      ├─ Jinja2 前台页面与管理员页面
      ├─ 资源/分类/搜索服务
      ├─ 表格导入、匹配与审核服务
      ├─ 渠道与分享链接服务
      ├─ /go 跳转与基础统计服务
      └─ 轻量巡检 Worker（独立进程，可选 APScheduler）
  → SQLAlchemy 2.x
  → MySQL 8

本地文件目录
  → 元数据识别
  → 人工确认
  → ProviderAdapter
  → 百度/夸克官方能力（可用时）
```

## 3. 部署单元

阶段1建议保留三个容器：

1. `web`：FastAPI、Jinja2、后台管理与 API。
2. `db`：MySQL 8。
3. `nginx`：生产反向代理；本地开发可不启用。

阶段3按需要增加一个使用相同代码镜像的 `worker`，执行分批链接检测和周期任务，不新增 Redis。

## 4. 模块边界

```text
app/
├── main.py                 应用入口
├── core/                   配置、安全、日志、数据库会话
├── models/                 SQLAlchemy 模型
├── schemas/                Pydantic 输入输出模型
├── repositories/           数据访问
├── services/               业务规则
├── api/                    JSON API 与 /go
├── web/                    前台路由
├── admin/                  管理后台路由与认证
├── importers/              XLSX/CSV 解析、预览和提交
├── providers/              渠道注册表、链接解析器、适配器
├── tasks/                  轻量任务与巡检
├── templates/              Jinja2 模板
└── static/                 构建后的 CSS、图片与脚本
```

路由层不直接写数据库；资源匹配、链接改绑、主备切换等规则统一放在服务层。

## 5. 渠道扩展机制

渠道平台由 `providers` 表配置；代码侧使用注册表查找适配器：

```python
class ProviderAdapter:
    code: str

    def recognize_url(self, url: str) -> bool: ...
    def normalize_url(self, url: str) -> str: ...
    def extract_share_id(self, url: str) -> str | None: ...
    def check_link(self, url: str) -> object: ...
    def upload(self, file_path: str) -> object: ...
    def create_share(self, provider_file_id: str) -> object: ...
```

阶段1实现百度、夸克的 URL 识别、规范化和手工链接导入；上传与创建分享仅保留接口。任何自动能力必须以届时可用的官方接口为准。

## 6. 链接安全与前台显示

- 原始链接只保存在后台；前台使用 `/go/{link_id}`。
- 仅 `active` 且 `is_visible=true` 的链接参与前台选择。
- `unchecked/suspected/invalid/blocked/manual_review/disabled` 均不显示。
- 主链接失效后选择同渠道优先级最高的有效备用链接。
- 一个渠道无可用链接时隐藏该渠道；全部渠道失效时显示“资源修复中”。
- 已失效的 `/go/{link_id}` 不跳转，返回友好状态页并引导回资源详情。

## 7. 导入与异步边界

单次最多几百行：解析、字段校验、链接规范化、数据库匹配和预览可在普通请求内完成。外部网盘检测不与导入预览绑定在同一长请求中，确认导入后按小批次执行并保存进度。

## 8. 安全基线

- 超级管理员密码使用 Argon2id 或同等级安全哈希。
- Session Cookie 使用 `HttpOnly`、`SameSite=Lax`，生产环境开启 `Secure`。
- 写接口具有 CSRF 防护；登录、搜索、反馈和 `/go` 有频率限制。
- 只允许渠道白名单域名，禁止开放重定向。
- Token、Cookie、密码不得写入代码、日志或普通业务字段。
- 管理后台和搜索参数页设置 `noindex`。

## 9. 环境迁移

关键配置：

```env
APP_ENV=development
DATABASE_URL=mysql+pymysql://...
PUBLIC_BASE_URL=http://localhost:8000
LOCAL_STORAGE_ROOT=D:/ebook-transfer
SESSION_SECRET=replace-me
ENABLE_SCHEDULER=false
```

生产服务器只改变环境变量与卷挂载，例如将 `LOCAL_STORAGE_ROOT` 改为 `/data/ebook-transfer`。

## 10. 当前决策与待确认项

已确认：百度与夸克为首批渠道；预留其他渠道；个人运营；表格导入；AI 只辅助；疑似记录人工确认；先本地测试、后部署服务器；只处理公版、开放许可或已授权内容。

阶段0无阻塞问题。进入阶段1前需准备 Python 3.12、Docker Desktop，并最终确定正式域名和管理员初始化方式。
