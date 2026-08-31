# 路由与同步接口

本页列出当前主要入口，不代表提供了完整的资源 REST CRUD API。普通后台写操作是 HTML 表单；不要将表单路由当作 JSON API 调用。

开发环境可查看 `/api/docs` 和 `/openapi.json`；生产环境关闭这两个入口。准确的请求模型以源码为准。

## 公开页面

以下均为 GET：

| 路径 | 用途 |
| --- | --- |
| `/` | 首页 |
| `/books` | 资源列表 |
| `/search?q=...` | 搜索 |
| `/category/{slug}` | 分类页 |
| `/collections` | 合集入口 |
| `/book/id/{resource_id}` | 固定 ID 图书详情；改书名不改变此地址 |
| `/book/{slug}` | 旧书名地址，已发布图书返回 301 跳转到 ID 地址 |
| `/go/{link_id}` | 验证可用入口、记录跳转并返回 302；不可用返回 410 |
| `/disclaimer` | 资源与免责声明 |
| `/robots.txt`、`/sitemap.xml` | 搜索引擎端点 |

源码：[前台路由](../app/web/routes.py)。没有独立的 `/authors/{slug}`、`/tutorials/{slug}` 或公开用户评论接口。

新旧图书地址同时支持 HEAD（不累计浏览次数）。草稿、归档和不存在的图书均返回 404。旧地址按原 `slug` 精确查找，纯数字书名不会被当作资源 ID。旧别名字段继续保留，不随改书名清空或重生成。

## 后台页面与表单

| 入口 | 用途 |
| --- | --- |
| `/admin/login` | GET 登录页面、POST 登录表单 |
| `/admin/logout` | POST 退出 |
| `/admin` | 仪表盘 |
| `/admin/resources` | 资源列表 |
| `/admin/resources/new` | 新建资源 |
| `/admin/resources/{resource_id}/edit` | 编辑和发布 |
| `/admin/links` | 链接检测 |
| `/admin/import` | 链接表格导入 |
| `/admin/import/meta` | 元数据表格导入 |
| `/admin/categories` | 分类管理 |
| `/admin/providers` | 网盘渠道管理 |
| `/admin/friend-links` | 友情链接 |
| `/admin/analytics` | 访问与搜索统计 |
| `/admin/uploads` | 云盘上传任务（受运行环境与配置限制） |
| `/admin/organizer` | 创建、撤销专用同步授权 |

后台表单写操作需要管理员会话及 `csrf_token`，使用当前页面返回的令牌。资源删除、归档、链接替换等具体 POST 路径见 [后台路由](../app/admin/routes.py)。

## 普通 JSON 接口

| 方法 / 路径 | 返回 |
| --- | --- |
| GET `/api/v1/health` | 进程响应状态，不检查数据库 |
| GET `/api/v1/ready` | 数据库及基础表就绪状态；未就绪返回 503 |
| GET `/api/v1/resources/search?q=...` | 可见资源摘要，最多 20 项 |

搜索响应的 `total` 是本次返回项数，不是全库匹配数。源码：[普通 API](../app/api/routes.py)。

资源摘要增加 `detail_url`，使用站点配置域名及 `/book/id/{id}`；原 `id` 和 `slug` 字段保留兼容。新调用方应直接使用 `detail_url`，不再根据书名或 `slug` 拼接详情页地址。

## 整理工具同步 API

以下接口需要 `Authorization: Bearer <专用同步授权>`，不是管理员密码。授权在后台创建，只显示一次；已撤销授权不能再访问。生产环境必须 HTTPS。

| 方法 / 路径 | 用途 |
| --- | --- |
| GET `/api/v1/organizer/info` | 站点编号、契约版本、单批上限、图片域名白名单及分类 |
| POST `/api/v1/organizer/preview` | 提交资料包，返回逐本预检 |
| POST `/api/v1/organizer/batches/{batch_id}/commit` | 提交明确的逐本决定 |
| GET `/api/v1/organizer/batches/{batch_id}/receipt` | 读取当前授权所属批次的回执 |

调用顺序：读取站点信息 → 预检 → 人工确认 → 提交 → 核对逐本回执。

- 契约版本为 `2.0`；单批 1–500 本，字节上限由 `ORGANIZER_MAX_BYTES` 控制。
- 预检返回的 `export_id` 用作后续路由中的 `batch_id`。
- 每本书包含稳定 `book_id`、版本、EPUB 哈希、书目字段、版权确认记录，以及可选封面和分享链接。
- 图片域名需要在站点白名单内；数据包不能夹带本地绝对路径、API 密钥等额外字段。
- 提交体为 `{"choices": [...]}`。每项动作仅允许 `create`、`update`、`bind`；绑定时需要明确目标，不能直接提交预检中的待选择状态。
- `overwrite` 和 `publish` 默认 false。发布还受链接检测结果和资源状态限制。
- **HTTP 200 不等于整批成功。** 必须检查逐本回执，不要只读请求状态或桌面汇总数字。
- 同一批次重放不会重新执行已完成项；修改资料或发布意图后应重新预检，或在后台操作。

输入字段与校验见 [Python 契约](../app/services/organizer_contract.py) 和 [JSON Schema](schemas/organizer-v2.schema.json)；鉴权和错误响应见 [同步路由](../app/api/organizer.py)，提交规则见 [同步服务](../app/services/organizer_sync.py)。

当前没有通用的 `/api/v1/admin/resources` CRUD、导入回滚或搜索联想接口。桌面端与批处理的限制见 [已知问题与使用限制](已知问题与使用限制.md)。
