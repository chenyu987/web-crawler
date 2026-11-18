# webcrawler：整站下载工具

给一个网址，把整个网站扒下来：网页（可离线浏览）、正文文字（Markdown）、所有链接、图片、视频、音频、文档（PDF/Word/Excel/PPT…）、压缩包、CSS/JS/字体，以及嵌入的 YouTube / Bilibili / Vimeo / HLS 视频，最后整理成方便二次利用的数据文件（JSONL / CSV）。

## 最简单的用法：双击运行（Mac / Windows 都支持）

1. 安装 Python 3（只需要装一次）：去 <https://www.python.org/downloads/> 下载安装。**Windows 安装时一定要勾选 “Add python.exe to PATH”。**
2. 下载本项目：在 GitHub 页面点 **Code → Download ZIP** 并解压，或者用 `git clone`。
3. 双击启动文件：
   - **Mac**：`start.command`。第一次如果提示“无法验证开发者”，就右键 → 打开；也可以在终端里运行 `bash start.command`。
   - **Windows**：`start.bat`。如果弹出 SmartScreen 拦截，点“更多信息 → 仍要运行”。
4. 第一次运行会自动安装依赖，大约需要 1 分钟。之后它会问你：
   - 网址
   - 要扒整个网站，还是只扒这一页
   - 最多扒多少页

   扒完会自动用浏览器打开结果汇总页。

文件默认保存在项目文件夹里的 `downloads/<域名>/`。

## 命令行安装（进阶）

```bash
git clone https://github.com/chenyu987/web-crawler.git
```

```bash
cd web-crawler && pip install -e ".[video]"
```

- `[video]` 会装上 **yt-dlp**，用来下载 YouTube、B 站等平台的视频。合并音视频需要系统里有 **ffmpeg**（`brew install ffmpeg` 或 `apt install ffmpeg`）。
- 有些网站的内容是 JavaScript 动态加载的。要扒这类网站，再装上 Playwright：

```bash
pip install -e ".[render]" && playwright install chromium
```

## 快速开始

```bash
webcrawler https://example.com
```

结果默认保存在 `downloads/<域名>/`。下载完成后打开里面的 `index.html`，就能看到汇总页（所有页面、文件分类、嵌入视频）。

常用例子：

```bash
# 只扒单个页面和它引用的所有文件
webcrawler https://example.com/article/123 --single-page

# 只要文档和图片，最多 200 个页面
webcrawler https://example.com -t documents,images -n 200

# 只扒 /docs/ 下面的内容
webcrawler https://example.com/docs/ --scope prefix

# 动态网站（React/Vue 等），用无头浏览器渲染
webcrawler https://spa-site.com --render

# 需要登录的网站：从浏览器导出 cookies.txt
webcrawler https://site.com --cookies-file cookies.txt

# 视频限制在 720p，跳过超过 500MB 的单个文件
webcrawler https://site.com --video-quality 720 --max-file-size 500MB

# 不带网址运行 = 交互模式（会提示你输入）
webcrawler

# 也可以用 python -m 运行
python -m webcrawler https://example.com -o my_output
```

## 输出结构

```
downloads/example.com/
├── index.html            # 汇总页：页面列表、按类型分好的文件、嵌入视频
├── manifest.json         # 统计摘要
├── pages/                # 网页 HTML，链接已改写成本地路径，可离线浏览
├── text/                 # 每个页面的正文，Markdown 格式（适合喂给 AI / 做知识库）
├── assets/
│   ├── images/  videos/  audio/  documents/  archives/
│   ├── styles/  scripts/  fonts/  other/
│   └── videos/_streams/  # yt-dlp 下载的 YouTube/B站等视频（含字幕、封面、info.json）
└── data/
    ├── pages.jsonl       # 每页一行：url、标题、描述、meta、标题层级、JSON-LD、全文
    ├── links.csv         # 所有链接：来源页、目标 URL、链接文字、是否站内
    ├── products.csv      # 购物网站：每个商品一行（名称、品牌、价格、库存、SKU、评分、描述、规格、本地图片）
    ├── products.jsonl    # 同上，JSON 格式（含每个尺码/颜色变体和多个报价）
    ├── images.csv        # 每张图片 + 对应的文字介绍（alt、图注、所在卡片的标题和说明、所在页面）
    ├── images.jsonl      # 同上，JSON 格式
    ├── assets.csv        # 所有已下载文件：URL、类型、本地路径、大小、来源页
    ├── embeds.csv        # 发现的嵌入/流媒体视频
    └── errors.csv        # 失败的 URL 和原因
```

### 图片和文字介绍对应起来（适合博物馆、画廊、商品类网站）

`data/images.csv` 里每张图片占一行，可以直接用 Excel 打开：

| 列 | 内容 |
|---|---|
| `local_path` | 本地图片文件（如果网站提供了多种尺寸，会选最大的那张） |
| `alt` / `title` | 图片自带的描述 |
| `caption` | `<figure>` 的图注 |
| `heading` / `context` | 图片所在“卡片/区块”的标题和介绍文字，比如作品名、作者、年代、材质 |
| `page_title` / `page_url` / `page_description` | 所在页面（例如作品详情页） |

另外，`text/` 下的 Markdown 已经把图片链接改成了本地文件，用 Typora、Obsidian 或 VS Code 打开就能图文对照阅读。

```bash
# 例：只扒某个博物馆的藏品栏目，只要图片
webcrawler https://museum.example.org/collection/ --scope prefix -t images -n 2000
```

### 购物网站：商品图片 + 全部商品信息

大部分购物网站（Shopify、WooCommerce、Magento，以及多数品牌官网）为了上 Google 购物，会在商品页里放一段标准格式的商品数据（schema.org Product）。工具会自动读取这段数据，还会读取 microdata 和 `og:product` 标签作为补充，整理成 `data/products.csv`：

| 列 | 内容 |
|---|---|
| `name` / `brand` / `category` | 商品名、品牌、分类 |
| `price` / `high_price` / `currency` | 价格（多个规格价格不同时给出价格区间）和币种 |
| `availability` / `condition` | 是否有货（InStock/OutOfStock…）、全新/二手 |
| `sku` / `mpn` / `gtin` | 货号、型号、条形码 |
| `color` / `size` / `material` / `properties` | 颜色、尺码、材质和其他参数 |
| `rating` / `review_count` | 评分和评论数 |
| `description` | 商品描述（已去掉 HTML 标签） |
| `local_images` | **这件商品的所有图片在本地的路径**（包括页面上没直接显示的图） |
| `variants` | 每个尺码/颜色变体各自的 SKU、价格、库存和图片 |

同一件商品出现在多个页面里也只记一次。

```bash
# 例：扒一个店铺的全部商品
webcrawler https://shop.example.com -n 3000 -t images
```

注意：如果网站没有放这段标准商品数据，商品信息只能退回到 `images.csv` 的“图片 + 周围文字”。淘宝、京东、亚马逊这类大平台有登录墙和反爬机制，一般扒不全，也不建议去扒。

用 Python 读取数据：

```python
import json, pandas as pd
pages = [json.loads(l) for l in open("downloads/example.com/data/pages.jsonl")]
links = pd.read_csv("downloads/example.com/data/links.csv")
```

## 能识别哪些东西

| 类型 | 来源 |
|---|---|
| 页面 | `<a href>`、`<iframe>`、sitemap.xml（自动读取，包括 robots.txt 里声明的） |
| 图片 | `<img src/srcset/data-src>`（懒加载）、`<picture><source>`、`og:image`、favicon、CSS 背景图 `url()` |
| 视频 / 音频 | `<video>`、`<audio>`、`<source>`、`og:video`、直链 `.mp4/.webm/.mp3…`、页面脚本里的媒体地址、`.m3u8/.mpd` 流 |
| 平台视频 | YouTube、Bilibili、Vimeo、抖音、西瓜、腾讯视频、TikTok、Twitter/X 等（需要 yt-dlp） |
| 文档 | pdf、doc(x)、xls(x)、ppt(x)、odt、rtf、txt、csv、md、epub、json、xml… |
| 其他 | 压缩包和安装包、CSS（包括里面引用的字体和图片）、JS、字体 |

## 全部参数

运行 `webcrawler --help` 查看完整说明。

| 参数 | 默认 | 说明 |
|---|---|---|
| `-o, --output` | `downloads/<host>` | 输出目录 |
| `-n, --max-pages` | 500 | 最多抓多少个页面（0 = 不限） |
| `-d, --max-depth` | 10 | 链接深度（-1 = 不限） |
| `--scope` | domain | `host` 同主机 / `domain` 含子域名 / `prefix` 起始路径之下 / `any` 不限 |
| `--include / --exclude` | | 用正则过滤 URL，可以写多个 |
| `-t, --types` | all | `images,videos,audio,documents,archives,styles,scripts,fonts,other` |
| `--max-file-size` | 2GB | 单个文件的大小上限 |
| `--max-files` | 0 | 最多下载多少个文件 |
| `--assets-same-domain` | 关 | 只下载本站域名下的文件（不下载 CDN 上的） |
| `--no-streams` | | 不用 yt-dlp 下载平台视频 |
| `--video-quality` | 最高 | yt-dlp 的最大分辨率，例如 720 |
| `--no-mirror` | | 不改写 HTML 里的链接 |
| `--redownload` | | 已经存在的文件也重新下载（默认跳过，所以中断后可以接着跑） |
| `-w, --workers` | 8 | 并发数 |
| `--delay` | 0.2 | 对同一个主机两次请求之间的间隔（秒） |
| `--render` | | 用无头 Chromium 渲染 JS 页面 |
| `--cookie / --cookies-file / -H` | | 登录态和自定义请求头 |
| `--proxy` | | 代理 |
| `--ignore-robots` | | 忽略 robots.txt（默认遵守） |

## 在代码里调用

```python
from webcrawler import CrawlConfig, Crawler

summary = Crawler(CrawlConfig(
    start_urls=["https://example.com"],
    output_dir="out",
    max_pages=100,
    types={"documents", "images"},
)).run()
print(summary)
```

## 测试

```bash
pip install -e ".[dev]" && pytest
```

## 使用须知

请只抓取你有权使用的内容。默认会遵守 robots.txt，并对同一主机限速。抓取前请留意目标网站的服务条款和版权，以及当地关于个人数据的法律法规。下载的资料未经授权不要再发布。
