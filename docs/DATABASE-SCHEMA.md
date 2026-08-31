# 数据库设计（阶段0）

> MySQL 8、SQLAlchemy 2.x、Alembic；本文定义阶段1所需核心关系。

## 1. 设计原则

- 一本书或一个合集只对应一条内部资源和一个 SEO 详情页。
- 文件格式、渠道资源、平台文件和分享链接分别建模，禁止复用同一编号。
- 百度、夸克只作为 `providers` 数据，不在 `resources` 增加固定字段。
- 原始导入行永久可追溯，只有确认后的数据进入正式表。
- 重复和错链同时由业务校验与数据库唯一约束兜底。
- 表中时间统一存储 UTC，页面按 Asia/Shanghai 展示。

## 2. 核心关系

```text
resources 1 ── N resource_files
resources N ── N categories
resources 1 ── N resource_channels
providers 1 ── N provider_accounts
providers 1 ── N resource_channels
resource_channels 1 ── N channel_share_links
channel_share_links 1 ── N link_check_logs
channel_share_links 1 ── N link_clicks
import_batches 1 ── N import_raw_rows
import_raw_rows 1 ── N import_errors
```

## 3. 资源表

### `resources`

| 字段 | 类型建议 | 说明 |
|---|---|---|
| id | BIGINT PK | 内部主键 |
| resource_code | VARCHAR(32) UNIQUE | 例如 B000001/C000001 |
| resource_type | VARCHAR(20) | book/tutorial/collection |
| title | VARCHAR(255) | 展示标题 |
| normalized_title | VARCHAR(255) INDEX | 匹配用标准标题 |
| slug | VARCHAR(255) UNIQUE | SEO路径 |
| subtitle | VARCHAR(255) NULL | 副标题 |
| author/translator/publisher | VARCHAR(255) NULL | 事实字段，不允许AI猜测 |
| isbn | VARCHAR(32) NULL INDEX | 规范化ISBN |
| language | VARCHAR(32) NULL | 语言 |
| publish_year | SMALLINT NULL | 出版年份 |
| description | TEXT NULL | 审核后的简介 |
| seo_title | VARCHAR(255) NULL | SEO标题 |
| seo_description | VARCHAR(320) NULL | SEO描述 |
| cover_image | VARCHAR(500) NULL | 站内相对路径或允许的资源 |
| copyright_status | VARCHAR(32) | public_domain/open_license/authorized |
| source_reference | VARCHAR(500) NULL | 内容或授权来源引用 |
| publish_status | VARCHAR(20) INDEX | draft/review/published/hidden/invalid/removed |
| view_count | BIGINT | 默认0 |
| published_at | DATETIME NULL | 发布时间 |
| created_at/updated_at | DATETIME | 审计时间 |

发布前必须具有非空标题、唯一 slug、已确认的版权状态和至少一条可用渠道链接。

### `resource_files`

保存一本书的 PDF、EPUB、MOBI 等格式。`file_hash` 在非空时唯一，用于避免相同文件重复上传。

关键字段：`resource_id`、`source_resource_id`、`file_format`、`file_size`、`file_hash`、`local_relative_path`、`source_type`、`source_reference`、`source_batch_id`、`processing_status`、时间字段。

### `categories` 与 `resource_categories`

分类支持父子结构。`categories.slug` 唯一；关联表使用 `(resource_id, category_id)` 联合主键，避免重复分类。

## 4. 渠道与链接

### `providers`

| 字段 | 说明 |
|---|---|
| code | 全局唯一；首批 `baidu`、`quark` |
| name/icon/base_domain | 展示配置 |
| status/sort_order | 启停与排序 |
| supports_upload/share/check | 平台能力开关 |
| capabilities_json | 未来能力扩展，不保存秘密 |

### `provider_accounts`

保存账号别名和凭据引用，不保存明文密码、Cookie或Token。唯一约束建议为 `(provider_id, account_alias)`。

### `resource_channels`

表示内部资源在某个平台和账号下的对应对象。关键字段为 `resource_id`、`provider_id`、`account_id`、`channel_resource_code`、`provider_file_id`、`metadata_json`、`status`、`priority`。

唯一约束：

```text
(provider_id, channel_resource_code)
(provider_id, provider_file_id)  -- provider_file_id 非空时
```

### `channel_share_links`

| 字段 | 说明 |
|---|---|
| channel_resource_id | 所属渠道资源 |
| provider_share_id | 平台分享ID |
| share_url | 原始分享URL，仅后台使用 |
| normalized_url | 规范化URL |
| normalized_url_hash | SHA-256，全局唯一 |
| extract_code | 可空提取码，后台按权限展示 |
| is_primary/is_visible | 主链接及前台显示许可 |
| status | unchecked/active/suspected/invalid/blocked/manual_review/disabled |
| check_result | 最近检测摘要 |
| last_checked_at/last_success_at | 检测时间 |
| consecutive_failures/failure_reason | 失败信息 |
| expires_at/disabled_at | 生命周期 |
| created_at/updated_at | 审计时间 |

唯一约束：

```text
UNIQUE(normalized_url_hash)
UNIQUE(provider_id, provider_share_id)  -- 可通过冗余provider_id或关联校验实现
```

同一渠道资源最多一条 `is_primary=true` 的可见链接，由服务层在事务中维护。链接改绑不得直接更新外键，必须走“确认改绑”服务并记录操作日志。

### `link_check_logs`

每次检测保存 `link_id`、`status`、`http_status`、`result_code`、`result_message`、`checked_at`。历史日志不随当前状态覆盖。

### `link_clicks`

保存最小化统计：`link_id`、`resource_id`、`provider_id`、`referer_path`、`device_type`、`visitor_hash`、`is_known_bot`、`clicked_at`。不保存完整User-Agent或可长期识别用户的原始IP；访客哈希按周期轮换盐值。

## 5. 导入表

### `import_batches`

保存文件名、文件类型、状态、总行数、已处理数、新建数、更新数、重复数、错误数、创建人、确认时间和时间字段。

### `import_raw_rows`

保存 `batch_id`、`row_number`、`raw_data_json`、标准化字段、识别平台、规范化链接哈希、匹配资源、匹配置信度、审核状态和处理结果。

### `import_errors`

保存 `raw_row_id`、`field_name`、`error_code`、`error_message`、`severity`。`blocking` 错误阻止提交，`warning` 允许管理员确认后继续。

## 6. 搜索与审计

### `search_queries`

按 `normalized_query` 聚合，记录原始示例、结果数、搜索次数、无结果次数、最近时间、状态、匹配资源和管理员备注。短时间重复请求、明显机器人和禁止关键词不计入运营需求。

### `admin_operation_logs`

保存管理员、操作类型、对象类型、对象ID、变更摘要、请求ID和时间。敏感值仅记录“已改变”，不得记录密码、Token、Cookie和完整提取码。

### `background_tasks`

阶段1用于分批链接检测和错误重试。字段包括任务类型、状态、进度、幂等键、尝试次数、错误摘要、计划时间、开始/完成时间。

## 7. 核心事务规则

1. 导入提交：写资源、渠道、链接和审核日志必须在同一事务中完成。
2. 替换链接：新链接先保存为 `unchecked`，检测成功后切换为 `active`；旧链接改为 `invalid/disabled` 并保留。
3. 主备切换：同一渠道内原主链接与新主链接在一个事务中更新。
4. 删除策略：业务资源默认软删除；原始导入、检测日志和操作日志不级联物理删除。
5. 发布校验：没有有效可见链接的资源不能首次发布。

## 8. 索引建议

- `resources(normalized_title, publish_status)`
- `resources(isbn)`
- `resources(resource_type, published_at)`
- `resource_channels(resource_id, provider_id, status)`
- `channel_share_links(channel_resource_id, status, is_visible, priority)`
- `link_check_logs(link_id, checked_at)`
- `search_queries(zero_result_count, last_searched_at)`
- `import_raw_rows(batch_id, review_status)`

所有数据库变更只通过Alembic迁移，测试必须验证唯一约束、跨资源错链拦截和主备切换事务。
