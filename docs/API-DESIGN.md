# API与路由设计（阶段0）

## 1. 约定

- 前台与后台以Jinja2服务端渲染为主，JSON API用于导入预览、状态更新和后续适配器。
- API前缀为 `/api/v1`；管理员页面为 `/admin`。
- 所有写操作要求管理员Session和CSRF令牌。
- 日期使用ISO 8601；分页参数统一为 `page`、`page_size`。
- 错误响应包含 `code`、`message`、`field_errors` 和 `request_id`，不返回堆栈或秘密。

## 2. 前台页面路由

| 方法 | 路由 | 用途 |
|---|---|---|
| GET | `/` | 首页 |
| GET | `/search?q=` | 搜索；参数页默认noindex |
| GET | `/categories/{slug}` | 分类页 |
| GET | `/authors/{slug}` | 作者页 |
| GET | `/books/{slug}` | 单本详情 |
| GET | `/tutorials/{slug}` | 教程详情，可复用资源模板 |
| GET | `/collections/{slug}` | 合集详情 |
| GET | `/go/{link_id}` | 校验、统计并302跳转 |
| GET/POST | `/feedback/link/{resource_code}` | 链接失效反馈 |
| GET | `/copyright` | 版权与内容范围说明 |
| GET | `/privacy` | 隐私政策 |
| GET | `/sitemap.xml` | Sitemap |
| GET | `/robots.txt` | Robots |

### `/go/{link_id}`

1. 查询链接及所属资源。
2. 要求资源可访问、链接 `active` 且 `is_visible=true`。
3. 校验实际目标域名属于对应渠道白名单。
4. 排除已知爬虫后记录最小化点击数据。
5. 返回302到当前分享URL。
6. 链接失效时返回410友好页；如资源还有其他渠道，提供返回详情页入口。

## 3. 管理页面路由

| 方法 | 路由 | 用途 |
|---|---|---|
| GET/POST | `/admin/login` | 超级管理员登录 |
| POST | `/admin/logout` | 退出 |
| GET | `/admin` | 仪表盘 |
| GET | `/admin/resources` | 资源列表 |
| GET/POST | `/admin/resources/new` | 新建资源 |
| GET/POST | `/admin/resources/{id}` | 资源详情与编辑 |
| GET | `/admin/providers` | 渠道配置 |
| GET | `/admin/link-checks` | 链接检测中心 |
| GET | `/admin/imports` | 导入批次 |
| GET | `/admin/imports/{id}` | 导入预览/结果 |
| GET | `/admin/missing-searches` | 无结果搜索 |
| GET | `/admin/tasks` | 轻量任务中心 |

管理页面统一返回 `X-Robots-Tag: noindex, nofollow`。

## 4. 资源API

```text
GET    /api/v1/admin/resources
POST   /api/v1/admin/resources
GET    /api/v1/admin/resources/{id}
PATCH  /api/v1/admin/resources/{id}
POST   /api/v1/admin/resources/{id}/publish
POST   /api/v1/admin/resources/{id}/hide
POST   /api/v1/admin/resources/{id}/remove
```

发布接口在服务端检查：版权状态、slug、关键字段、至少一个有效可见链接以及不存在未解决的阻断警告。

## 5. 渠道与链接API

```text
GET    /api/v1/admin/providers
POST   /api/v1/admin/providers
PATCH  /api/v1/admin/providers/{id}

POST   /api/v1/admin/resources/{resource_id}/channels
PATCH  /api/v1/admin/channels/{channel_id}

POST   /api/v1/admin/channels/{channel_id}/links
PATCH  /api/v1/admin/links/{link_id}
POST   /api/v1/admin/links/{link_id}/check
POST   /api/v1/admin/links/{link_id}/make-primary
POST   /api/v1/admin/links/{link_id}/disable
POST   /api/v1/admin/links/{link_id}/replace
```

`replace` 请求示例：

```json
{
  "share_url": "https://pan.example.invalid/s/new",
  "extract_code": "1234",
  "check_immediately": true
}
```

响应先返回新链接 `unchecked`。检测成功后状态切换为 `active`，旧链接保留为 `invalid` 或 `disabled`。检测失败时新链接不在前台显示。

## 6. 导入API

```text
POST   /api/v1/admin/imports                  上传XLSX/CSV并创建批次
GET    /api/v1/admin/imports/{id}             批次摘要
GET    /api/v1/admin/imports/{id}/rows        分页预览
PATCH  /api/v1/admin/imports/{id}/rows/{row}  修改单行决策
POST   /api/v1/admin/imports/{id}/bulk-review 批量确认安全项
POST   /api/v1/admin/imports/{id}/commit      幂等提交
POST   /api/v1/admin/imports/{id}/rollback    回退本批新增数据
GET    /api/v1/admin/imports/{id}/errors.csv  下载错误报告
```

创建批次返回统计与预览地址，不直接写正式资源。`commit` 要求客户端提供 `Idempotency-Key`。

单行审核动作：

```text
match_existing
create_draft
ignore_duplicate
manual_bind
reject
```

跨资源链接冲突只能 `manual_bind` 或 `reject`，不能使用普通批量确认。

## 7. 检测与任务API

```text
POST  /api/v1/admin/link-checks/batches
GET   /api/v1/admin/link-checks/batches/{id}
GET   /api/v1/admin/links/{id}/check-logs
GET   /api/v1/admin/tasks
POST  /api/v1/admin/tasks/{id}/retry
```

批量检测立即返回任务ID；前端通过低频轮询查看进度。单次规模小，但不在一个HTTP请求内等待全部外部链接完成。

## 8. 搜索API与记录规则

前台SSR可直接使用服务层；如需要联想可提供：

```text
GET /api/v1/search/suggestions?q=
```

只有实际搜索提交才计数；短时间内相同访客哈希和相同标准词去重。零结果搜索写入 `search_queries`，不保存完整IP。

## 9. 状态码

- `200/201`：成功。
- `302`：有效 `/go` 跳转。
- `400`：格式错误。
- `401/403`：未登录或无权限/CSRF失败。
- `404`：对象不存在。
- `409`：重复链接、分享ID或渠道编码冲突。
- `410`：已失效或已下架的公开对象。
- `422`：字段校验失败。
- `429`：频率限制。

## 10. 非阶段1接口

自动上传、自动创建分享和账号容量查询只定义适配器接口，不在阶段1暴露公开API。待平台官方能力确认后再增加：

```text
POST /api/v1/admin/uploads
GET  /api/v1/admin/uploads/{id}
POST /api/v1/admin/uploads/{id}/retry
```

不得为了实现这些接口使用逆向接口、明文Cookie或绕过验证码。
