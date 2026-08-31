# 阶段0静态视觉原型

## 页面

- `index.html`：首页
- `book.html`：单本资源详情
- `collection.html`：合集详情
- `admin-dashboard.html`：后台仪表盘
- `admin-resource.html`：资源编辑
- `admin-import.html`：批量导入预览

所有页面共享 `assets/styles.css` 和 `assets/app.js`。视觉原型使用Tailwind CDN作为原型期依赖，同时将已确认的设计令牌和核心组件样式集中在共享CSS中；正式阶段不得继续使用CDN。

## 本地预览

在本目录运行：

```powershell
python -m http.server 4173 --bind 127.0.0.1
```

打开 `http://127.0.0.1:4173/index.html`。

## 交互范围

当前可验证移动菜单、搜索提示、渠道按钮提示、合集目录展开、导入状态筛选和后台移动侧栏。所有数据均为视觉原型示例，不连接数据库，不执行真实网盘跳转。

## 截图

`../screenshots/` 包含六个页面各三种视口的最终JPG：

- 390×844
- 440×956
- 1440×1024

文件命名格式为 `{page}-{viewport}.jpg`。
