# Linux 一键部署指南

更新：2026-08-31。对应项目：`D:\网盘拉新`。入口：项目根目录 `deploy.sh`。

## 1. 这次交付的范围

网站按单台 Linux 服务器、单网站实例部署，采用 MySQL 8.4、FastAPI 和 Caddy。域名、网站名称在部署时填写，没有预选正式域名；图片域名也没有写死。

已有后台、资源展示、分类、搜索、分享入口和整理软件同步接口继续保留。本轮不增加桌面整理功能，不自动上传电子书，不创建真实分享链接，不替你上传 GitHub 或购买服务器。

一键入口负责：环境检查、经确认安装 Docker、配置生成、镜像构建、数据库迁移、初始化管理员、HTTPS、健康检查；另提供备份、恢复、升级、查看状态和重置密码。

一键部署不等于无需域名解析。填写的域名需要已指向服务器，80/443 端口需要能从公网访问。自动签发证书依赖这些条件，见 [Caddy 官方说明](https://caddyserver.com/docs/automatic-https)。

## 2. 上传 GitHub 前

上传的是网站源码，不是整个电脑目录的压缩包。建议先用私有仓库。使用 Git 提交可以遵守 `.gitignore`；直接在网页拖拽文件不会替你按 `.gitignore` 排除敏感文件。

应包含：`app/`、`alembic/`、`scripts/`、`deploy/` 中的样例和配置模板、`tests/`、`.github/`、`Dockerfile`、依赖清单、`deploy.sh`、`alembic.ini`、说明文档，以及已有本地工具源码（若保留完整项目）。

不要上传：

- 根目录 `.env`、`deploy/.env`、任何真实 API Key/Token、私钥。
- `local.db`、其他数据库、`runtime/`、`backups/`、本地书库和原始 EPUB。
- `.venv/`、`dist/`、`build/`、用户上传封面文件。
- 截图、整理软件导出目录、电子书和本地交接记录，本次首次发布统一排除，保留在本地。

`.gitignore` 和 `.dockerignore` 已覆盖常见配置、数据库、备份、运行目录及上传封面。已经进入 Git 历史的文件不受忽略规则保护；若曾提交密钥，必须撤销/轮换，不能只删除当前文件。

不要把访问令牌写进 GitHub 仓库 URL。私有仓库使用 GitHub 的正常 SSH/凭据授权。

## 3. Linux 首次部署

### 准备条件

- 自动安装 Docker 支持全新 Ubuntu 22.04/24.04、Debian 12/13，其他 Linux 先自行安装 Docker Engine、Compose 插件、Python 3.10+、Git 和 util-linux。
- 服务器能访问 GitHub、Docker 镜像源、Python 软件源及证书机构。
- 使用 root 或有 sudo 权限的账户；如果已有容器环境，脚本不自动卸载/替换它。
- 80/443 没被另一个网站占用。脚本不会强行关闭已有网站，也不会自动改防火墙。
- 域名 A 记录指向服务器；若配置了 AAAA，IPv6 也必须正确可达。

Docker 官方安装资料：[Ubuntu](https://docs.docker.com/engine/install/ubuntu/)、[Debian](https://docs.docker.com/engine/install/debian/)、[Compose 插件](https://docs.docker.com/compose/install/linux/)。新安装使用官方软件源，不使用 `curl | sh` 安装方式。

### 运行

推荐使用同一个服务器管理账户拉取和维护代码。项目仓库为 `https://github.com/onlylm/shuku`：

```bash
git clone https://github.com/onlylm/shuku.git ebook-site
cd ebook-site
sudo bash deploy.sh
```

已装 Git 时，也可以合为一行：

```bash
git clone https://github.com/onlylm/shuku.git ebook-site && cd ebook-site && sudo bash deploy.sh
```

第一次会询问：

1. 缺 Docker 时是否安装；不同意则退出。
2. 网站正式域名，只填主机名，不加 `https://` 和路径。现在没决定可以退出，以后再填。
3. 网站名称，暂时默认“书库”，会同步到前台标题、页头、页脚、后台。

脚本生成 `deploy/.env`，权限为仅当前账户可读写。会显示一次随机初始管理员密码，请存进密码管理器。初始用户名默认 `admin`，不是本地演示密码。

然后自动构建、迁移数据库并初始化基础分类及百度/夸克渠道；**不会插入演示书籍**。部署只有在 HTTPS、数据库、首页、资源列表、登录页、robots 和 sitemap 检查通过后才显示完成。

访问：你填写的 HTTPS 域名；管理后台路径：`/admin/login`。

如果证书或 DNS 检查失败，容器可能已经启动，但部署尚未验收完成。解决问题后重跑 `sudo bash deploy.sh`。它不会重置已有密码或覆盖配置。

## 4. 配置在哪里改

只修改服务器 `deploy/.env`，不要修改本地开发用的根目录 `.env`。修改后重新运行 `sudo bash deploy.sh`。

| 配置项 | 用途 |
| --- | --- |
| `SITE_DOMAIN` | 正式网站域名，网页绝对地址、canonical、sitemap 和 HTTPS 从这里取得 |
| `APP_NAME` | 网站显示名称 |
| `ORGANIZER_SITE_ID` | 自动生成的站点编号，之后让整理软件填写相同编号 |
| `ORGANIZER_COVER_HOSTS` | 可选的图片域名白名单，只填主机名，多个用逗号分隔 |
| `LINK_CHECK_AUTOMATIC_ENABLED` | 默认 false，完成网盘检测联调后可改 true |
| `INITIAL_ADMIN_PASSWORD` | 仅空数据库初始化管理员时使用；修改它不会重置已有管理员 |

网站域名与图片域名独立。CF/R2 的上传密钥继续由本地工具保管，网站展示远程图片只需要公开图片地址与白名单，不需要 R2 上传密钥。

不要直接修改已有数据库的 `MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD`、`MYSQL_DATABASE`、`MYSQL_USER` 或 `COMPOSE_PROJECT_NAME`。这些不是改个配置就能同步修改数据库账号的字段，误改会导致连接失败或使用另一个数据卷。需要换密码时单独做数据库账号轮换。

网站目前按单进程运行；不要直接扩展为多实例，因为登录限流、上传任务和巡检还不是分布式设计。

## 5. 书库、封面和本地工具

MySQL、上传封面、运行数据、HTTPS 证书分别保存到独立 Docker 数据卷，重新构建网站不删除这些数据。

- 正式站第一次启动是空书库。本地测试库不会打包进镜像，也不会自动发布到公网。
- 初期可以在网站后台创建资源/分类，或用后台 CSV/XLSX 导入资源和已取得的分享链接。
- CF 封面继续由公开图片地址展示；数据卷里的封面用于本地上传的图片。**备份脚本不会下载或备份 CF/R2 中的对象。**
- 如果要完整保留当前 Windows SQLite 中的分类、书籍、历史记录，需要单独安排 SQLite → MySQL 的数据迁移。本轮没有执行，也不要把 `local.db` 当作 MySQL 数据文件直接复制或提交 GitHub。
- Linux 网站无法读取你电脑的 `D:\电子书存放`。之后本地软件负责读取文件、上传、回传元数据；网站负责展示和下载跳转。
- 正式部署默认关闭后台云盘上传队列。夸克官方连接器的 Agent 环境要求不会因为换到 Linux 就消失，未做真实联调前不能承诺服务器全自动上传。

首批先人工验收 10–20 本有权发布的书：书名、分类、封面、简介、分享入口、提取码、发布/下架、失效隐藏和移动端显示。不要直接批量上线整个书库。

## 6. 升级与日常维护

在服务器项目根目录执行：

```bash
sudo bash deploy.sh status
sudo bash deploy.sh check
sudo bash deploy.sh logs
sudo bash deploy.sh backup
sudo bash deploy.sh update
sudo bash deploy.sh password
```

- `status`：显示容器状态。
- `check`：检查数据库就绪及公网 HTTPS 页面。
- `logs`：显示最近100条容器日志。不要未经检查把日志发到公开页面。
- `backup`：短暂停止网站写入，备份完成或失败后尝试恢复原本运行的网站。
- `update`：要求服务器代码没有未提交修改，先备份，再快进拉取 GitHub，构建并迁移；迁移前会再生成快照。不会丢弃你的源码修改。
- `password`：在终端交互输入新管理员密码，不把密码放命令历史。

不建议在服务器直接改源码；在本地修改、提交 GitHub，然后运行 update。用同一个账户维护仓库；如果 Git 提示所有权不匹配，确认项目属于自己后使用该目录的拥有者操作，不设置全局信任所有目录。

更新失败不会自动把数据库迁移倒回去，防止二次破坏。先看日志，必要时使用升级前备份和对应代码恢复。备份清单记录了代码版本；不确定时不要反复强行迁移。

## 7. 备份与恢复

完整备份保存在项目 `backups/时间-随机编号/`，包含：

- `database.sql.gz`：网站 MySQL 数据库；包含管理员、资源、分类、链接、同步授权等。
- `files.tar.gz`：网站运行数据和本地上传封面。
- `deploy.env`：当时的私密部署配置。
- `manifest.json`：文件 SHA-256 校验值和源码版本。

以 `.partial` 结尾的是失败或中断的备份，不能用于自动恢复。恢复前会校验完整性，并拒绝跨目录路径、符号链接和特殊文件。

备份包含账号凭据和私密数据，**不是加密备份**。请复制到服务器之外的安全位置；只留在同一台服务器上无法抵御服务器丢失。脚本不自动删除旧备份，需关注磁盘空间，也不自动设置定时任务。

恢复示例：

```bash
sudo bash deploy.sh restore /完整路径/ebook-site/backups/你选中的备份目录
```

需要输入 `RESTORE` 确认。会先给当前数据再做一个备份，再替换数据库和两个数据卷目录。恢复失败时网站保持停止，避免向只恢复了一半的数据写入；请先解决错误或重新恢复。

恢复不会自动替换当前 `deploy/.env` 或 HTTPS 证书；不要把其他网站备份随意导入。跨服务器灾难恢复时，先用相应源码和原配置重新部署，再恢复可信备份。管理员密码是备份时的密码。需要退回旧版数据库时，也应先选用相应旧版源码，避免启动时立刻执行新版迁移。

不要运行 `docker compose down -v`、清理数据卷、删除数据库目录等命令来“重装”，这会删除数据。

## 8. 常见问题

- **提示安装 Docker，但服务器已有宝塔/其他网站：** 先确认 80/443 和已有 Docker 环境。脚本不会自动卸载或停止其他服务。已有反向代理需人工接入，不能让两个程序同时占用80/443。
- **后台进不去或反复回登录页：** 使用 HTTPS 域名，不使用 IP:8000；生产 Cookie 只通过 HTTPS 发送。
- **证书一直不成功：** 检查 DNS、错误 AAAA、端口、防火墙及 `logs`。若域名由 Cloudflare 代理，排障时可以先改为仅 DNS；启用代理时使用严格的端到端 HTTPS，避免 Flexible 模式循环跳转。
- **数据库无法启动：** 检查可用内存、磁盘和数据卷权限，以及是否改过数据库密码配置，不要删数据卷重试。
- **刚部署没有本地书籍：** 这是防止演示/私人数据意外发布的设计，按第5节录入或迁移。
- **服务器更新后看到空库：** 先检查是否误改了 `COMPOSE_PROJECT_NAME`，或混用了旧 `docker-compose.yml`，不要继续导入覆盖。
- **GitHub Actions 没有全绿：** 先打开失败步骤。工作流只检查网站和服务器同步接口，不包括 Windows 桌面软件测试；容器构建/迁移失败时先处理再上线。

## 9. 当前验证边界

开发机完成96项网站相关自动测试（96通过、0失败），包括空库迁移与重复初始化、部署配置和备份安全测试。正式 Compose 配置通过官方 Compose 工具校验，三个部署 Shell 脚本通过 ShellCheck 检查，Caddy 配置通过官方 Caddy 工具的适配校验；未启动公网服务或申请证书。测试报告在本地 `runtime/server-validation/tests.xml`（不提交 GitHub）。

GitHub 工作流已加入真实 Linux 容器构建、MySQL 迁移、初始化及备份验证，需要上传后实际运行。

本次修复旧初始迁移直接读取当前模型、导致新库重复创建后续表的问题：初始迁移现在使用冻结的结构快照。已有数据库不会重跑已应用的迁移，不需要清库或重新生成版本记录。

尚未以你的真实域名/服务器执行 Docker 部署、证书签发、MySQL 备份恢复演练；不能把源码开发完成表述为已经成功上线。网站数据的正式迁移和 CF 图片、真实网盘下载验收也需要在上线前完成。

全项目回归额外发现旧桌面整理软件的 `test_quark_directory_and_share_retry` 报 `stage_file` 未定义。本轮按“软件随后再说”的范围保留软件源码，不修改该问题；它不属于服务器网站测试。
