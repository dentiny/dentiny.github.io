# dentiny 的博客

地址：https://dentiny.github.io

一个用 Jekyll 构建、由 GitHub Pages 托管的中文个人博客。包含首页、文章归档与搜索、关于页、RSS 和深浅色主题。文章使用 Markdown 编写。

## 发布文章（直接在 GitHub 操作）

1. 打开仓库里的 `_posts` 目录，选择 **Add file → Create new file**。
2. 文件名使用 `年-月-日-英文短标题.md`，例如 `2026-10-06-my-first-post.md`。
3. 复制下面的内容，改好标题、日期、摘要、标签与正文。
4. 提交到 `main` 分支；GitHub Pages 会自动构建和发布。一般需要几分钟，可在仓库 **Actions** 中查看结果。

```markdown
---
title: 我的第一篇文章
date: 2026-10-06 09:00:00 -0700
description: 用一句话描述文章内容。
tags: [随笔]
---

这里是文章开头。

<!--more-->

## 一个小标题

在这里写正文。支持 **加粗**、列表、图片、链接与代码块。
```

示例日期需要改成实际发布时间；未来日期的文章在发布时间到达前不会显示，且到时需要触发一次新构建。建议使用当前日期，或暂存在 `_drafts` 中。时区为 `America/Los_Angeles`，夏令时偏移为 `-0700`，冬令时为 `-0800`。

## 放图片

把图片上传到 `assets/images/`，在文章中写：

```markdown
![图片说明](/assets/images/example.png)
```

## 修改博客

- 首页介绍：`index.html`
- 关于页：`about.md`
- 博客标题、说明和时区：`_config.yml`
- 配色、字体、布局：`assets/css/style.css`
- 示例文章：`_posts/2026-10-05-hello-world.md`，可以修改或删除。

`about.md` 与示例文章是起步文案，可替换成自己的介绍和文章。不要把密码、密钥或私人资料提交到公开仓库。

## 本地预览（可选）

在兼容 `github-pages` 的 Ruby 环境中安装 Bundler，然后运行：

```sh
bundle install
bundle exec jekyll serve
```

访问 http://localhost:4000 。如果不想安装开发环境，直接用 GitHub 网页编辑即可。

## 托管设置

仓库 **Settings → Pages → Build and deployment**：

- Source：**Deploy from a branch**
- Branch：**main**，目录 **/ (root)**

Jekyll 自动生成文章页面、RSS 与 sitemap。博客源码和网站均公开。
