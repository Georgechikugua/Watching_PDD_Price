# 价格盯盘 🏷️

每天自动抓取你关注的商品(**拼多多 / 京东 / 其他电商**)价格,生成手机端友好的页面并发布到你自己的 GitHub Pages:价格走势、近期最低价、今日涨跌一目了然。

> **线上示例(作者自己的监控页)**:<https://georgechikugua.github.io/Watching_PDD_Price/>
> 你按下面步骤跑起来后,会得到一个属于你自己的同样页面。

## 它是怎么工作的

电商平台只给"真浏览器"正常页面——自动化浏览器会被拼多多风控识别并返回**假的"售罄"数据**。所以本项目用你电脑上安装的 **Chrome 本体**挂本地调试端口来抓取(浏览器进程不带任何自动化痕迹),你登录一次拼多多,之后每天定时用同一环境抓取,会话稳定。抓取完成后通过 GitHub API 把页面发布到 GitHub Pages,**不需要服务器**。

```
每天 09:30 计划任务 → 真实 Chrome 打开商品页 → 解析价格 → SQLite 存档
                    → 生成手机端页面 → 自动发布 GitHub Pages
```

## 快速开始(Windows,约 10 分钟)

### 0. 准备

- Windows 电脑 + 已安装 [Python 3.10+](https://www.python.org/downloads/)(安装时勾选 **Add Python to PATH**)
- 已安装 Google Chrome
- 一个 GitHub 账号(可选,用于手机端查看)

### 1. 下载项目

点本页面右上角 **Code → Download ZIP**,解压到任意目录。

### 2. 双击 `install.bat`

它会自动:安装依赖 → 下载浏览器内核(走国内镜像)→ 启动设置向导。

向导里依次完成:

1. **添加第一个商品**:在拼多多 App 里 分享 → 复制链接,直接粘贴(京东/其他网站商品同理)
2. **注册每日计划任务**:输入 `y`,以后每天 09:30 自动抓取
3. **(可选)配置 GitHub 发布**:
   - 新建一个 **Public** 仓库(勾选 Add a README)
   - 按 [GitHub 文档](https://docs.github.com/zh/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens) 生成 **Fine-grained token**:只选这个仓库,权限勾 `Contents`、`Pages`、`Administration` 三项 **Read and write**
   - 在向导里输入用户名 / 仓库名 / token,验证通过即配置完成

### 3. 完成

向导结束后运行一次 `python tracker.py all`(或重跑 `install.bat`),你的页面就上线了:
`https://你的用户名.github.io/仓库名/`,手机收藏即可。

## 日常使用

| 命令 | 作用 |
| --- | --- |
| `python tracker.py all` | 抓取 + 生成页面 + 发布(计划任务每天自动跑) |
| `python tracker.py add "商品链接"` | 添加商品(支持拼多多分享码/短链,自动规范化) |
| `python tracker.py list` | 查看在盯的商品和状态 |
| `python tracker.py remove 序号或链接` | 移除商品 |
| `python tracker.py serve` | 局域网内临时查看(手机连同一 Wi-Fi) |
| `python tracker.py login` | 拼多多登录失效后重新登录(弹出的就是你的 Chrome) |
| `python tracker.py login-jd` | 盯京东商品前登录一次京东 |

## 常见问题

- **提示"拼多多登录已失效"** → 运行 `python tracker.py login`,在弹出的 Chrome 里重新登录一次
- **提示"商品已售罄或已下架"** → 这是页面的真实状态,换一个在售链接
- **某天电脑关机了** → 当天缺一个价格点,开机后跑一次 `python tracker.py all` 补上
- **抓取时屏幕角落闪过 Chrome 窗口** → 正常现象(抓取窗口放在屏幕外)
- **隐私** → 拼多多登录态、GitHub token、价格数据库全部只存在本机 `data/` 目录,不会被上传;仓库里只有页面和代码

## 限制

- 需要 Windows + Chrome(抓取依赖真实浏览器环境)
- 拼多多价格是"当前账号看到的补贴价",会随账号/活动浮动,记录的是你账号的视角
- 仅供个人学习研究使用,请遵守各平台服务条款,勿用于商业用途

## License

MIT
