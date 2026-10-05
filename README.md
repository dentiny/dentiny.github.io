# dentiny's blog

[Visit the blog](https://dentiny.github.io)

A minimal English blog built with Jekyll and hosted on GitHub Pages.

## Write a post

1. Open `_posts` on GitHub and choose **Add file → Create new file**.
2. Name the file `YYYY-MM-DD-short-title.md`.
3. Use the template below, updating the date and content.
4. Commit to `main`. GitHub Pages will publish the update automatically; check **Actions** for its status.

```markdown
---
title: My first post
date: 2026-10-05 09:00:00 -0700
description: A short introduction.
tags: [Notes]
---

Write your post here.
```

Use the current publication date. Future-dated posts stay hidden until their date passes and another build runs. The timezone is `America/Los_Angeles`; use `-0700` during daylight saving time and `-0800` in winter. Unfinished posts can live in `_drafts`.

## Images

Upload images to `assets/images/` and link them in Markdown:

```markdown
![Image description](/assets/images/example.png)
```

## Customize

- `index.html`: homepage introduction
- `about.md`: about page
- `_config.yml`: site title and description
- `assets/css/style.css`: appearance
- `_posts/2026-10-05-hello-world.md`: starter post; edit or delete it

## Local preview (optional)

With Ruby and Bundler installed:

```sh
bundle install
bundle exec jekyll serve
```

Open http://localhost:4000.

## Publishing

**Settings → Pages** uses **Deploy from a branch**, with `main` and `/ (root)` as the source. Posts, RSS, and the sitemap are generated automatically. Both the source repository and website are public.
