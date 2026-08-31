# 电子书资源索引与多渠道网盘分发系统

> 项目需求、UI规范与分阶段开发任务书  
> 文档版本：v1.0  
> 开发方式：UI先行、分阶段迭代、每阶段验收后继续

---

## 1. 文档用途

本文件放在项目根目录，作为 Codex 开发本项目时的最高级业务需求文档。

Codex 开始任何开发工作前，必须：

1. 完整阅读本文档以及仓库中的 `AGENTS.md`。
2. 检查当前仓库、已有代码、分支和未提交修改。
3. 只执行当前明确授权的开发阶段，不提前开发后续功能。
4. 先提交实施计划、数据库设计和需要确认的问题，再开始编码。
5. 每完成一个小阶段，运行测试并提交可验证结果。
6. 未经确认不得擅自改变产品定位、UI风格、数据模型核心关系或技术栈。

---

## 2. 项目定位

本项目是一个由管理员统一维护的电子书、公开文献和专题合集资源索引系统。

系统通过搜索引擎获取对具体书名、作者、格式或专题合集有明确需求的访问者，在内容详情页向访客提供多个第三方网盘渠道的资源入口。第三方网盘负责存储、下载、拉新统计与收益展示；本站不计算佣金，不处理支付，不提供用户上传。

核心产品定义：

> 电子书内容库 + 多渠道资源管理 + SEO详情页生成 + 分享链接巡检 + 缺失搜索需求发现。

核心业务原则：

- 一个内部资源只生成一个SEO详情页。
- 一个内部资源可以拥有多个文件格式。
- 一个内部资源可以绑定任意数量的渠道平台。
- 同一渠道可以存在多个渠道资源或主备分享链接。
- 内部资源ID、渠道资源编码、平台文件ID和分享链接是不同概念，禁止混用。
- 系统不得把百度、夸克或任何特定网盘字段写死在图书主表中。
- 未来新增渠道时，应只增加平台配置和适配器，不修改资源核心模型和页面结构。

---

## 3. 项目目标

### 3.1 业务目标

1. 管理员能够批量导入书目信息和不同渠道的分享链接。
2. 系统自动识别、清洗、去重并合并同一本书的不同格式和不同渠道。
3. 系统自动生成结构完整、移动端友好、SEO基础合格的资源详情页草稿。
4. 用户可在详情页选择当前有效的网盘渠道。
5. 后台能查看每个资源的全部渠道、链接状态和检测历史。
6. 后台能看到用户搜索但站内没有结果的书名或关键词。
7. 尽可能减少逐本建页、逐条录入和逐页维护的人工工作。

### 3.2 非目标

当前项目不建设：

- 普通用户注册和登录。
- 用户上传。
- 评论、评分、收藏、关注和社交。
- 会员、支付、订单或站内收费。
- 佣金计算、收益报表和第三方财务数据回传。
- App或小程序。
- 未经授权资源的自动获取或分发。
- 绕过网盘安全机制、验证码、平台限制或服务条款的功能。

---

## 4. 合规边界

系统只应处理公版、开放许可、取得授权或运营方有权分发的资源。

必须保留以下信息：

- 内容来源。
- 来源批次。
- 授权或版权状态。
- 封面来源。
- 下架原因。
- 管理员操作记录。

前台必须具有：

- 版权声明。
- 侵权反馈或联系入口。
- 隐私政策。
- 第三方网盘服务说明。
- 资源失效反馈入口。

AI可以生成原创简介草稿和SEO文案，但禁止编造作者、译者、ISBN、出版社、出版年份、版本、页数、文件大小或授权状态。

---

## 5. UI先行与视觉锁定

### 5.1 强制开发顺序

业务编码之前必须先完成UI基线。未经用户确认UI基线，不得大规模开发前台页面。

顺序如下：

1. 建立设计令牌和组件规范。
2. 生成核心页面的移动端高保真稿。
3. 生成对应桌面端稿。
4. 用静态HTML/Tailwind实现可交互视觉原型。
5. 在390×844、440×956和桌面1440px视口截图验收。
6. 用户确认后锁定设计系统。
7. 后续业务页面只能复用既定组件，不允许随意创造新风格。

### 5.2 视觉方向

整体风格：极简、安静、可信、清新的数字阅读室，类似现代电子书屋和Kindle阅读环境，不做传统下载站的杂乱广告风格。

基础颜色：

```text
页面背景：#F7F5EF  暖米白
主色：    #234A3C  墨绿色
辅色：    #DCE7DF  鼠尾草浅绿
正文：    #202522  炭黑
次要文字：#6B706D
边框：    #DDDAD1
卡片背景：#FFFFFF
错误：    #B4473A
警告：    #B7832F
成功：    #2F6B4F
```

视觉约束：

- 不使用渐变。
- 不使用玻璃拟态。
- 不使用夸张阴影。
- 不使用大面积高饱和色。
- 不堆积徽标和促销标签。
- 不出现倒计时、虚假下载提示和弹窗广告。
- 卡片使用细边框、轻阴影和充足留白。
- 移动端优先设计。
- 正文必须具有良好阅读性。

### 5.3 必须先确认的前台页面

1. 首页。
2. 搜索结果页。
3. 分类页。
4. 单本资源详情页。
5. 合集详情页。
6. 资源失效状态页。
7. 404页面。

### 5.4 必须先确认的后台页面

1. 管理员登录页。
2. 仪表盘。
3. 资源列表。
4. 资源详情与编辑页。
5. 渠道及分享链接管理页。
6. 批量导入预览页。
7. 链接检测中心。
8. 缺失搜索资源页。
9. 任务中心。

### 5.5 组件锁定

UI确认后，Codex必须建立并复用：

- 页面容器。
- 顶部导航。
- 搜索框。
- 分类标签。
- 图书卡片。
- 合集卡片。
- 渠道按钮。
- 状态徽标。
- 表格。
- 分页。
- 表单字段。
- 弹窗和确认框。
- 空状态。
- 加载骨架。
- 错误状态。
- 底部导航或页脚。

颜色、圆角、间距、阴影、字体层级必须定义成CSS变量或Tailwind主题令牌，禁止在不同页面散落不一致的魔法值。

### 5.6 UI验收

- 390px移动端无横向滚动。
- 440px移动端布局比例自然。
- 768px至1440px平滑响应。
- 主要按钮、标题和卡片与确认稿保持一致。
- 不允许因接入后端而改变已确认的排版结构。
- 每个核心页面开发完成后必须生成截图与参考稿对比。
- 任何新的视觉模式必须先获得确认。

---

## 6. 核心业务概念

### 6.1 内部资源

内部资源代表一本书或一个合集，是站内内容的唯一主体。

示例：

```text
内部资源ID：B000001
资源类型：book
书名：百年孤独
详情页：/books/bai-nian-gu-du
```

### 6.2 渠道平台

渠道平台代表百度网盘、夸克网盘或未来其他平台。

平台只描述能力和展示信息，不代表具体账号或具体文件。

### 6.3 渠道账号

渠道账号代表运营方在某个平台中使用的具体账号。

系统只保存账号别名、状态和加密凭据引用。禁止在代码、日志或普通数据库字段中保存明文密码、Cookie或Token。

### 6.4 渠道资源

渠道资源表示某个内部资源在某个平台和账号中的对应资源。

```text
B000001 百年孤独
├── 百度渠道编码：BD-526810
└── 夸克渠道编码：QK-893721
```

渠道编码可以不同，但关联的内部资源ID相同。

### 6.5 分享链接

一个渠道资源可以生成多条分享链接，包括历史链接、当前主链接和备用链接。

分享链接失效不等于渠道源文件已经删除。

---

## 7. 推荐数据模型

数据库使用 MySQL 8 和 SQLAlchemy 2.x，数据库变更必须通过 Alembic。

### 7.1 `resources`

```text
id
resource_code           全局唯一内部编号
resource_type           book / collection
title
slug                    全局唯一SEO路径
subtitle
author
translator
publisher
isbn
language
publish_year
description
seo_title
seo_description
cover_image
copyright_status
publish_status
view_count
published_at
created_at
updated_at
```

发布状态：

```text
draft
review
published
hidden
invalid
removed
```

### 7.2 `resource_files`

```text
id
resource_id
source_resource_id      外部数据集中的资源ID
file_format
file_size
file_hash
local_relative_path
source_type
source_reference
source_batch_id
processing_status
created_at
updated_at
```

### 7.3 `categories`

```text
id
name
slug
parent_id
description
sort_order
status
created_at
updated_at
```

如果一本书需要多个分类，应增加 `resource_categories` 关联表。

### 7.4 `providers`

```text
id
code
name
icon
base_domain
status
sort_order
supports_upload
supports_share
supports_check
capabilities_json
created_at
updated_at
```

### 7.5 `provider_accounts`

```text
id
provider_id
account_alias
credential_reference
status
storage_total
storage_used
last_authenticated_at
last_error
created_at
updated_at
```

### 7.6 `resource_channels`

```text
id
resource_id
provider_id
account_id
channel_resource_code
provider_file_id
metadata_json
status
priority
created_at
updated_at
```

唯一约束：

```text
(provider_id, channel_resource_code)
(provider_id, provider_file_id)  在非空时唯一
```

### 7.7 `channel_share_links`

```text
id
channel_resource_id
provider_share_id
share_url
normalized_url_hash
extract_code
is_primary
is_visible
status
check_result
last_checked_at
last_success_at
consecutive_failures
failure_reason
expires_at
disabled_at
created_at
updated_at
```

状态：

```text
unchecked
active
suspected
invalid
blocked
manual_review
disabled
```

### 7.8 `link_check_logs`

```text
id
link_id
status
http_status
result_code
result_message
checked_at
```

### 7.9 `search_queries`

```text
id
raw_query
normalized_query
result_count
search_count
zero_result_count
unique_visitor_count
status
matched_resource_id
admin_note
first_searched_at
last_searched_at
```

状态：

```text
pending
collecting
added
ignored
prohibited
```

### 7.10 导入与任务表

```text
import_batches
import_raw_rows
import_errors
background_tasks
admin_operation_logs
```

原始导入行必须保留，便于重新清洗、追踪错误和整批回滚。

---

## 8. 数据导入流程

### 8.1 支持的输入

1. XLSX书目文件。
2. CSV渠道链接文件。
3. 后台批量粘贴渠道链接。
4. 管理员手工创建少量资源。

### 8.2 书目XLSX字段映射

系统至少支持：

```text
日期
文件格式
文件大小
书名
作者
出版社
语种
出版年份
页码
来源文件或种子批次
外部资源ID
```

原始值写入暂存表，经过标准化后才能进入正式资源表。

### 8.3 渠道链接CSV格式

推荐一条渠道记录占一行：

```csv
resource_code,title,author,provider,channel_resource_code,share_url,extract_code,account_alias
B000001,示例书名,示例作者,quark,QK-893721,https://example.invalid/share/xxx,,quark-01
B000001,示例书名,示例作者,baidu,BD-526810,https://example.invalid/share/yyy,8k3m,baidu-01
```

### 8.4 导入处理

```text
上传文件
→ 创建导入批次
→ 分块读取
→ 写入原始暂存表
→ 字段校验
→ 标准化书名、作者、语言和格式
→ 重复检测
→ 自动匹配内部资源
→ 显示导入预览
→ 人工确认
→ 写入正式表
→ 检测分享链接
→ 生成详情页草稿
```

### 8.5 匹配顺序

1. 内部资源ID完全匹配。
2. 渠道编码已经绑定。
3. ISBN完全匹配。
4. 标准书名 + 作者匹配。
5. 书名 + 出版社 + 年份匹配。
6. 模糊匹配并给出置信度。
7. 无法确定时进入人工匹配，不得强行合并。

### 8.6 大文件要求

- 分块处理，每批建议2,000至10,000行。
- 不允许在单个HTTP请求中同步完成大批量导入。
- 显示总行数、已处理、新建、更新、重复和错误数量。
- 支持失败重试和断点继续。
- 提供可下载的错误报告。
- 导入完成后默认进入草稿，不自动公开。

---

## 9. 元数据与封面处理

信息优先级：

```text
人工确认
> 文件版权页或规范EPUB元数据
> 合法书目数据源
> 已有内部书库
> 文件名识别
> AI候选
```

系统可提取：

- EPUB标题、作者、出版社、语言、ISBN、简介和内置封面。
- PDF标题、作者、页数、文件大小和首页预览。
- 文件格式、大小与哈希。

封面处理顺序：

1. EPUB内置封面。
2. 文件包中的明确封面文件。
3. PDF首页候选并等待确认。
4. 获准使用的书目封面服务。
5. 站内统一占位封面。

禁止未经确认自动抓取电商平台、搜索引擎或其他电子书网站封面。

所有自动识别字段应记录来源和置信度。低置信度或多版本冲突必须进入人工确认。

---

## 10. 前台功能

### 10.1 首页

- 品牌和搜索框。
- 热门分类。
- 最近更新单本资源。
- 精选超级合集。
- 热门资源。
- 简洁页脚。

### 10.2 搜索

支持搜索：

- 书名。
- 作者。
- 译者。
- ISBN。
- 合集名称。
- 分类。

无结果时向用户显示友好提示，并记录缺失搜索需求。

### 10.3 单本详情页

- 面包屑。
- 封面。
- 书名、作者、译者、出版社。
- 分类、格式、大小和版本信息。
- 动态渠道入口。
- 资源信息。
- 原创内容简介。
- 下载说明。
- 相关推荐。
- 链接失效反馈。
- 版权说明。

### 10.4 合集详情页

- 合集标题和封面。
- 合集介绍。
- 资源数量、格式和总大小。
- 部分书目与展开目录。
- 动态渠道入口。
- 相关合集。

### 10.5 动态渠道显示

- 只显示状态正常且允许前台展示的链接。
- 按优先级排序。
- 主推荐渠道显示主要按钮。
- 其他渠道折叠为“其他获取方式”。
- 主链接失效时使用同渠道备用链接。
- 所有渠道失效时显示“资源修复中”。

### 10.6 跳转

前台不直接输出业务按钮的原始网盘URL，统一使用：

```text
/go/{link_id}
```

流程：

1. 检查链接是否允许跳转。
2. 排除已知爬虫的点击统计。
3. 记录资源、渠道、来源页面和设备类型等基础数据。
4. 302跳转到当前有效分享链接。

本站只统计访问和点击，不计算佣金或收益。

---

## 11. SEO要求

- Jinja2服务端渲染。
- 每页唯一标题和Meta Description。
- 稳定的slug。
- Canonical URL。
- Open Graph基础标签。
- Book或CreativeWork结构化数据。
- 面包屑结构化数据。
- `sitemap.xml`。
- `robots.txt`。
- 分类和详情页分页。
- 下架、失效、404和410策略。
- 图片WebP、尺寸属性和懒加载。
- 搜索参数页默认不批量索引。

建议路由：

```text
/
/search?q=
/categories/{slug}
/authors/{slug}
/books/{slug}
/collections/{slug}
/go/{link_id}
/copyright
/takedown
/privacy
/sitemap.xml
/robots.txt
```

关键词应自然分布在标题、H1、简介、格式信息、图片alt和结构化数据中，禁止关键词堆砌和批量生成高度重复的低质量页面。

---

## 12. 后台功能

### 12.1 管理员认证

- 第一阶段只需超级管理员。
- 密码安全哈希。
- 安全Session Cookie或经过评审的认证方式。
- 登录频率限制。
- 管理写入接口全部受保护。
- 管理页面禁止搜索引擎索引。

### 12.2 仪表盘

- 资源总数。
- 今日新增。
- 待识别。
- 待匹配。
- 待发布。
- 渠道异常链接。
- 热门资源。
- 热门无结果搜索。
- 失败任务。

### 12.3 资源管理

- 单本和合集筛选。
- 创建、编辑、草稿、发布、隐藏和下架。
- 批量分类、发布和停用。
- 查看全部格式、渠道和链接。
- 查看内容来源和识别置信度。

### 12.4 渠道与链接管理

- 动态增加渠道平台。
- 配置平台图标、域名、排序和能力。
- 管理渠道账号别名和状态。
- 绑定渠道资源编码。
- 增加主备分享链接。
- 立即检测。
- 查看检测历史。
- 批量检测和导出异常清单。

### 12.5 缺失搜索资源

- 显示无结果关键词、搜索次数和最近搜索时间。
- 归一化常见格式词和书名号。
- 人工合并相似搜索词。
- 一键创建资源草稿。
- 标记待补充、整理中、已补充、忽略或不可提供。
- 过滤爬虫、脚本和短时间重复请求。

### 12.6 操作日志

记录管理员及系统的重要操作，包括资源修改、链接替换、发布、下架、导入、检测和任务失败。

---

## 13. 链接检测

不能仅依赖HTTP状态码，因为失效页面可能返回200。

检测层次：

1. URL和域名格式校验。
2. DNS、TLS和重定向检查。
3. 通用404或异常页面检测。
4. 平台专用失效文本检测。
5. 提取码要求检测。
6. 验证码、登录和访问频繁识别。

处理规则：

```text
首次不确定失败 → suspected
连续多次不确定失败 → manual_review
页面明确提示失效 → invalid
验证码或频率限制 → 保留原状态并延迟重试
```

推荐频率：

- 新链接立即检测。
- 热门链接每1至3天。
- 普通链接每7天。
- 疑似异常数小时后复查。
- 明确失效链接停止高频检测。

巡检必须限速，不能绕过验证码或平台限制。

---

## 14. 自动化与任务系统

需要抽象的任务类型：

- 导入任务。
- 资源匹配任务。
- 元数据处理任务。
- 封面处理任务。
- 链接检测任务。
- SEO草稿任务。
- 发布任务。
- Sitemap更新任务。
- 数据备份任务。

状态：

```text
queued
running
succeeded
retrying
paused
failed
manual_review
```

每个任务必须支持幂等、有限重试、错误记录和人工重新执行。

网盘上传和分享能力使用统一适配器：

```python
class ProviderAdapter:
    def authenticate(self): ...
    def upload(self): ...
    def create_share(self): ...
    def get_share_info(self): ...
    def check_link(self): ...
    def disable_share(self): ...
```

第一阶段只建立接口和手工链接导入能力，不要求实现所有平台自动上传。官方API可用时优先使用官方API；没有稳定接口的平台保持半自动，不得绕过安全限制。

---

## 15. 本地中转模式

电脑只作为临时中转和短期恢复备份，不作为唯一永久仓库。

建议目录：

```text
D:/ebook-transfer/
├── downloading/
├── processing/
├── ready/
├── uploading/
├── retention/
├── failed/
└── metadata-backup/
```

清理条件：

- 文件哈希已保存。
- 元数据和封面已入库。
- 至少一个渠道上传成功。
- 分享链接检测正常。
- 最好存在两个独立可用渠道。
- 超过设定保留期。
- 没有未完成任务引用该文件。

数据库、导入表、资源ID映射、渠道编码、分享链接、提取码、封面和操作日志必须独立备份。

---

## 16. 技术栈

### 16.1 第一阶段

```text
Python 3.12
FastAPI
SQLAlchemy 2.x
MySQL 8
Alembic
Pydantic Settings
Jinja2
Tailwind CSS
Pytest
Docker Compose
Nginx
```

Tailwind CDN只允许视觉原型阶段使用，正式上线前构建压缩后的静态CSS。

### 16.2 后续任务系统

```text
Redis
Celery或经过评审的独立任务Worker
Celery Beat或系统定时任务
```

禁止把大批量导入、文件处理和巡检任务放在普通HTTP请求中长时间执行。

### 16.3 推荐目录

```text
app/
├── main.py
├── core/
├── models/
├── schemas/
├── repositories/
├── services/
├── api/
├── web/
├── admin/
├── importers/
├── providers/
├── tasks/
├── templates/
└── static/
alembic/
tests/
docs/
samples/
docker-compose.yml
.env.example
requirements.txt
```

---

## 17. 分阶段开发计划

### 阶段0：UI与架构确认

交付：

- `docs/UI-DESIGN-SYSTEM.md`
- `docs/ARCHITECTURE.md`
- `docs/DATABASE-SCHEMA.md`
- `docs/IMPORT-WORKFLOW.md`
- `docs/API-DESIGN.md`
- `docs/PHASE-1-PLAN.md`
- 前后台核心页面静态视觉原型。
- 390、440和1440视口截图。

验收后才能进入阶段1。

### 阶段1：最小可运行闭环

交付：

- 项目骨架、环境配置和Docker Compose。
- 数据模型与Alembic迁移。
- 管理员登录。
- 分类、资源、渠道、渠道资源和分享链接管理。
- XLSX/CSV小批量导入、预览、去重和错误报告。
- 首页、搜索、分类、单本详情和合集详情。
- 动态多渠道按钮。
- `/go/{link_id}`跳转和基础点击记录。
- 手动链接检测。
- 无结果搜索记录。
- Sitemap、Robots、Canonical和基础结构化数据。
- 演示数据和自动测试。

### 阶段2：大批量导入与内容加工

- 异步分块导入。
- 断点继续。
- 文件元数据提取。
- 封面处理。
- 识别来源和置信度。
- SEO草稿。
- 批量确认和发布。
- 导入批次回滚。

### 阶段3：巡检与运营自动化

- 定时链接巡检。
- 平台专用检测器。
- 主备链接切换。
- 异常通知。
- 自动Sitemap更新。
- 自动备份。
- 缺失资源聚合与任务创建。

### 阶段4：渠道适配器

- 在遵守平台规则和权限的前提下接入官方API。
- 上传任务队列。
- 创建分享和回写链接。
- 渠道账号状态与容量监控。
- 没有官方稳定能力的平台继续使用手工或半自动导入。

---

## 18. 第一阶段验收标准

### 数据

- 可创建一本书并绑定三个以上动态渠道。
- 不修改数据库结构即可新增渠道平台。
- 同一资源的不同渠道编码正确绑定到同一内部资源ID。
- 同一渠道可保存主链接和备用链接。
- 重复链接和重复渠道编码被数据库约束阻止。

### 导入

- 可导入书目XLSX和链接CSV。
- 导入前有预览。
- 错误数据不会污染正式表。
- 同一本书不同格式可以合并。
- 无法确定的记录进入人工匹配。

### 前台

- 核心页面移动端无横向滚动。
- 视觉与已确认UI基线一致。
- 只展示正常渠道。
- 跳转路径有效。
- 详情页具有唯一SEO信息。

### 后台

- 管理员可查看每个资源的全部渠道和链接。
- 可立即检测链接并查看历史。
- 可更换主链接。
- 可查看无结果搜索词并创建资源草稿。

### 工程质量

- `docker compose up`可以启动开发环境。
- `.env.example`完整且不包含秘密。
- Alembic可以从空数据库迁移。
- 核心测试通过。
- 没有把密码、Token、Cookie写进代码或日志。
- 提供README运行、测试和部署说明。

---

## 19. Codex执行规则

1. 先计划，后编码。
2. 阶段0未验收不得进入阶段1。
3. 不得一次性生成未经验证的大量页面。
4. 不得擅自增加用户系统、支付系统和佣金系统。
5. 不得写死特定网盘字段。
6. 不得用AI猜测事实字段。
7. 不得通过关闭测试或删除校验来掩盖错误。
8. 每个功能必须同时考虑移动端、错误状态、空状态和权限。
9. 每完成一个小阶段运行相关测试并报告结果。
10. 保留用户已有代码和未提交修改，不得破坏性重置仓库。
11. UI变化必须提供截图，不得只用文字声称一致。
12. 发现需求冲突、外部API限制或安全风险时，停止相关实现并提出具体问题。

---

## 20. Codex首次任务

Codex收到本文档后的第一次任务仅执行阶段0：

1. 检查仓库和开发环境。
2. 输出业务理解和风险清单。
3. 创建架构、数据库、导入、API和第一阶段计划文档。
4. 建立UI设计令牌和组件规范。
5. 用静态HTML/Tailwind实现首页、单本详情页、合集详情页、后台仪表盘、资源编辑页和批量导入页的视觉原型。
6. 输出390×844、440×956和1440px页面截图。
7. 等待用户确认UI和架构。

本次不得开始完整业务后端开发。

