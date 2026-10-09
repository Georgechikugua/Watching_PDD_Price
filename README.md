# 价格盯盘

每天自动抓取你想买的商品(拼多多 / 京东 / 其他电商)的价格,存进本地数据库,
生成手机端友好的页面,随时随地查看:价格走势、近期最低价、今日涨跌一目了然。

**线上页面**:https://georgechikugua.github.io/Watching_PDD_Price/(每次抓取后自动更新)

工作方式:拼多多只给"真浏览器"正常页面(自动化浏览器会被风控返回假数据),
所以抓取用的是系统安装的 Chrome 本体挂本地调试端口,登录也在它里面完成;
抓取完成后通过 GitHub API 把页面发布到 GitHub Pages,无需服务器常开。

## 第一次使用(3 步)

```bat
:: 1. 登录拼多多(弹出浏览器窗口,扫码或验证码登录一次,长期有效)
python tracker.py login

:: 2. 添加你想盯的商品(链接用拼多多 App 里"分享-复制链接"拿到的)
python tracker.py add "https://mobile.yangkeduo.com/goods.html?goods_sign=XXXX"

:: 3. 立刻抓一次并生成页面
python tracker.py all
```

想顺便盯京东商品,再运行一次 `python tracker.py login-jd`(可选),
然后 `add` 京东商品链接即可。其他网站(苹果官网等)不用登录,直接 add。

## 手机上怎么看

```bat
python tracker.py serve
```

会打印出类似 `http://192.168.1.5:8788` 的地址,手机连同一个 Wi-Fi,
浏览器打开即可(电脑开机、命令运行着才能访问)。每天抓取的数据都保留,
可以天天看走势。

> 也可以把 `serve` 做成开机自启:任务计划程序里新建"登录时启动"任务,
> 程序填 `pythonw.exe`,参数 `tracker.py serve`。

## 日常命令

| 命令 | 作用 |
| --- | --- |
| `python tracker.py all` | 抓价 + 生成页面(计划任务每天 09:30 自动跑) |
| `python tracker.py fetch` | 只抓价 |
| `python tracker.py dashboard` | 只生成页面 |
| `python tracker.py serve` | 启动手机端查看服务(端口 8788) |
| `python tracker.py list` | 看在盯的商品和状态 |
| `python tracker.py add 链接 [名称]` | 添加商品 |
| `python tracker.py remove 链接或序号` | 移除商品 |
| `python tracker.py login` / `login-jd` | 重新登录拼多多 / 京东 |

商品列表就是 `products.json`,直接编辑也可以(多个商品则 put 多行)。

## 自动抓取

已注册 Windows 计划任务 **PriceTracker**:每天 09:30 运行 `run_tracker.bat`
(= `tracker.py all`)。删除任务:`schtasks /delete /tn PriceTracker`。
改时间:`schtasks /change /tn PriceTracker /st 10:00`。

注意:电脑关机当天就不会抓;开机后手动跑一次 `python tracker.py all` 补上即可。

## 说明与排障

- 价格一天只记一个点(当天最后一次抓取),图表最多回看 120 天。
- 拼多多登录态失效时,页面会提示重新 `login`;抓取失败的商品不影响其他商品。
- 某商品连续解析失败时,`data/debug/<商品>/` 下会留下当时的页面快照和截图,
  排查反爬变化用。
- 数据都在 `data/prices.db`(SQLite),备份/迁移拷这一个文件就行。
- 拼多多会把"自动化浏览器"识别出来并返回假的"售罄"页,所以抓取用的是你电脑
  上的 **Chrome 本体**(挂本地调试端口,不带自动化痕迹),登录也在它里面完成,
  登录和抓取同环境,会话最稳。抓取时窗口在屏幕外一闪而过属正常现象。
- 拼多多登录态失效时,页面会提示重新 `login`;抓取失败的商品不影响其他商品。
- 京东商品不登录容易拿到壳页面,建议 `login-jd` 后再盯京东商品。
- 隐私:登录态和数据库都只存在本机,不经过任何第三方。
