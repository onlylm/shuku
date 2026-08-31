# 数据模型

以 [SQLAlchemy 模型](../app/models/entities.py) 和 [Alembic 迁移](../alembic/versions/) 为准。本页是维护索引，不是可直接执行的建表 SQL。

## 核心关系

```text
resources（图书）
  ├─ resource_categories ↔ categories（分类）
  ├─ resource_files（文件记录）
  └─ resource_channels（图书的平台渠道）
       ├─ providers（平台）
       ├─ provider_accounts（可选账号引用）
       └─ channel_share_links（分享入口）
            └─ link_check_logs（检测记录）
```

一本书可关联多个分类、多个网盘平台；同一本书与同一个平台只有一条渠道记录，渠道下可以有多条分享入口。

## 表索引

| 表 | 用途 |
| --- | --- |
| `admin_users` | 管理员、密码哈希、启用状态 |
| `categories` | 分类名称、slug、父分类、排序、可见性 |
| `resources` | 书名、作者、ISBN、简介、封面、版权记录、发布与 SEO 字段 |
| `resource_categories` | 资源与分类的多对多关联 |
| `resource_files` | 文件名、格式、大小、路径及哈希记录 |
| `providers` | 网盘名称、代码、状态和能力描述 |
| `provider_accounts` | 账号标签、凭据引用和状态 |
| `resource_channels` | 资源在一个网盘上的渠道及远端文件编号 |
| `channel_share_links` | 分享地址、提取码、状态、可见性和检测时间 |
| `link_check_logs` | 每次检测的结果、耗时与错误摘要 |
| `link_clicks` | 网盘跳转记录 |
| `search_queries` | 搜索词和结果数量 |
| `import_batches`、`import_raw_rows`、`import_errors` | 表格批次、原始行、预检及错误记录 |
| `background_tasks` | 后台任务的参数、状态、结果与错误 |
| `friend_links` | 友情链接及排序、可见性 |
| `admin_operation_logs` | 后台操作审计记录 |
| `organizer_tokens` | 整理工具同步授权的哈希及启用状态 |
| `organizer_identities` | 本地图书稳定编号与网站资源的对应关系 |
| `organizer_batches` | 同步包、预检和逐本提交回执 |

账号引用字段不代表可以保存明文密码。原始导入行、分享地址、同步数据与备份都应视为运营数据，不公开提交。

## 关键约束与状态

- `resources.resource_code` 与 `resources.slug` 唯一；ISBN 有索引但不是唯一键。
- `categories.slug` 唯一，分类名称本身不保证唯一；维护同名分类时需确认父级。
- `resource_channels` 对 `resource_id + provider_id` 有唯一约束。
- `channel_share_links.normalized_url_hash` 全局唯一；分享编号有索引，不能仅凭索引推断为唯一约束。
- `import_raw_rows` 的 `batch_id + row_number` 唯一。
- `organizer_identities.book_id` 为主键，`resource_id` 唯一；同步依赖稳定编号、版本和内容哈希。

资源默认草稿，分享链接默认待检测且隐藏。**传统链接表格导入会将新资源记录设为已发布，但新链接仍待检测、隐藏**；不能把它与整理工具默认草稿的同步行为混为一谈。

版权字段是操作者填写的记录，不是系统核验结果；部分旧录入路径会默认填“已获授权”，运营时必须主动核对。完整边界见 [已知问题与使用限制](已知问题与使用限制.md)。

## 迁移和备份

本地开发使用 Alembic 升级；生产通过 `sudo bash deploy.sh` 或 `sudo bash deploy.sh update` 执行迁移。不要直接改数据库表结构来替代迁移。

初始迁移使用冻结的结构快照，后续版本逐步增加字段和表。不要修改已执行迁移、清空版本表或删库来处理升级问题。

Windows SQLite 文件不能直接放进 MySQL 数据卷。跨数据库迁移需要单独导出、转换、导入并校验。生产备份/恢复操作见 [Linux 配置与维护](Linux一键部署指南.md)。
