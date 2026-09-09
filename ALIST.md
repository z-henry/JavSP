# AList 原生刮削

JavSP 可以直接扫描 AList 目录，在本地生成 NFO、封面、海报和可选剧照后上传到 A，再在网盘内改名、移动原视频到 B。视频内容不会经过运行 JavSP 的电脑。

## 启动

在项目根目录运行一次，处理完成后退出：

```powershell
cd D:\code\JavSP
$env:JAVSP_ALIST_TOKEN = '你的 AList 管理令牌'
.\.venv\Scripts\python.exe -m javsp -c config.alist.yml
```

已有 Python 环境安装项目依赖时，也可使用 `python -m javsp -c config.alist.yml`。不需要启动 `script/main.py`。打包的程序使用 `JavSP.exe -c config.alist.yml`。

当前 `config.yml` 已按提供的旧版 `config.ini` 迁移，可直接运行 `python -m javsp`。`config.alist.yml` 同步了相同偏好，但 AList 令牌及百度翻译凭据留空；使用示例前需填写。`JAVSP_ALIST_TOKEN` 优先于配置中的 `alist.token`。需要保存个人配置时可复制为已被 Git 忽略的 `config.alist.local.yml`。修改 INI 不会自动改变 YAML。

| 配置 | 示例 / 含义 |
|---|---|
| `scanner.source` | `alist`；省略时仍使用原有 `local` 模式 |
| `alist.base_url` | `http://192.168.100.10:5244` |
| `alist.source_dir` | `/115/bigsister` |
| `alist.metadata_dir` | A：`/local-emby/bigsister` |
| `alist.video_dir` | B：`/115/emby/bigsister` |
| `alist.work_dir` | `.javsp-alist`，本地元数据临时文件、SQLite 进度和进程锁 |
| `alist.http_timeout` | 单个请求超时秒数，默认 300 |
| `alist.task_timeout` | 每次等待后台任务的超时秒数，默认 3600 |
| `alist.poll_interval` | 后台任务查询间隔秒数，默认 2 |

相对本地路径以启动时的工作目录为基准。首次运行后保持工作目录和 `work_dir` 一致。AList 模式要求 `summarizer.move_files: true`、`hard_link: false`，自动处理而不询问番号。更新开关沿用 INI；从未安装为发行包的源码运行时会跳过更新检查。

迁移后的偏好包括 `http://127.0.0.1:7897` 代理、1 次重试、5 秒请求超时、150 的路径长度上限、百度标题及简介翻译，以及 AirAV 中文标题优先。已有中文标题直接使用，并在可用时保存其他站点原题；简介仍按配置翻译。旧版路径长度 `auto` 在 AList 模式按 UTF-8 字节计算。INI 未配置的剧照下载与已有 NFO 目录跳过均关闭。

旧 INI 的本地 `scan_dir` 由 AList 来源替代；旧版结束等待选项不迁入远程模式，仍按约定单次执行并退出。其他刮削、命名、标签和代理设置以 INI 为准。

## 目录与处理规则

来源、A、B 必须是互不包含的独立路径。首版要求来源和 B 使用同一个 `115 Open` 挂载；会通过管理接口核对真实挂载，因此令牌需要读取存储列表及任务状态、列目录、建目录、上传、移动、改名和删除权限。

A 和 B 使用同一套完整相对目录模板，直接按有码／无码／未知分类，不再添加 `ready` 层。此配置适用于新任务，已整理文件保持原位，未完成任务仍按保存的目标路径恢复：

```text
A/有码/[ABC-123] 标题/movie.nfo
A/有码/[ABC-123] 标题/poster.jpg
A/有码/[ABC-123] 标题/fanart.jpg
B/有码/[ABC-123] 标题/ABC-123.mp4
```

分片命名为 `ABC-123-CD1.mp4`、`ABC-123-CD2.mp4`。标题截短根据最终远程路径计算；目录和文件名模板不能越出目标根目录。元数据先上传到 A 的 `.javsp-staging/<任务号>`，确认后发布到正式位置，全部发布成功后才操作视频。

扩展名、最小大小、忽略目录、已有 NFO 跳过规则和番号识别沿用本地扫描规则。来源根目录直属视频也可以处理。已有目标文件不会覆盖；本批次命名冲突也会保留来源并报告。

## 清理与恢复

一个源文件夹内全部视频及分片成功后，JavSP 会删除整个文件夹，**包括残留字幕、图片和文本**。未识别、被过滤、冲突或失败的视频会阻止该文件夹及其祖先清理。来源根目录始终保留，没有成功影片的目录不删除。

运行时请让上游停止向待处理目录写入。程序会在操作和删除前刷新检查新增／变化内容，但网盘没有跨请求事务，无法锁住其他上传者。

删除前将已完成目录改为唯一的 `.javsp-cleanup-<随机号>`，再次核对内容后删除。该过程的进度也保存到 SQLite；后来新建的原名称目录不作为旧删除请求的目标。

失败后保留 `.javsp-alist` 并重新执行相同命令。程序先恢复固定的目标路径和分片编号，再扫描新影片：

- 已有明确成功记录的步骤会核对后跳过。
- 上传任务超时可能仍在后台执行，再次运行会查询原任务。
- 改名或移动的响应丢失时，需要可用的哈希／稳定 ID 证明已到达目标；证据不足或源位置仍存在时保留待核对，不重复提交不确定的操作。
- 后台上传任务明确失败／取消时，下次运行改用新的唯一暂存路径，保留旧的部分上传文件；后台移动任务明确失败时，只有源文件仍一致且目标不存在才重新提交。任务结果未知时不会按失败处理。
- 清理暂存目录出现新增内容、删除结果不确定或恢复记录发生冲突时，程序会报告路径／任务号并保留数据，需核对后处理。不要通过删除 SQLite 来强行绕过检查。

SQLite 中保存服务地址、挂载身份和 A/B 路径。需要改用其他路径时，指定新的 `work_dir`，保留原状态目录用于恢复旧任务。成功完成的本地元数据临时文件会清除；远程元数据暂存目录可能留下空目录，可在确认没有未完成任务后清理。

旧扫描的“哪些文件是视频”会固定保存，重启后缩减扩展名列表不会让旧的未处理视频变成可删除残留。目录清理也会核对稳定 ID 或创建时间；接口不提供可用目录身份时保留目录。

退出码 `0` 表示本轮没有操作失败或冲突，`1` 表示存在未完成操作或全局错误。日志最后列出成功、失败、冲突、清理目录和跳过视频数量；跳过项不会被视为已成功处理。

## 运行日志

AList 日志直接显示在终端，同时按每次运行保存 UTF-8 文件到 `alist.work_dir/logs/日期-时间-随机号.log`（当前为 `.javsp-alist/logs`）。启动和退出时会显示完整日志路径，无需另开日志选项。

日志包含刮削进度、源目录与视频文件、A/B 目标路径、每个元数据文件的上传和发布结果、异步任务编号、每个视频的改名和移动结果、清理成功或目录保留原因。远程操作只有核对完成后才显示成功；请求已提交或返回不代表成功。恢复运行会注明已有任务及核对结果。

末尾逐项列出整理成功、刮削／整理失败、冲突、跳过视频、清理结果及保留目录。整理成功和源目录删除分别报告：同一文件夹中部分影片失败时，成功影片仍可上传和移动，但文件夹会保留。失败明细带源目录、操作阶段、涉及的文件路径及错误原因。日志只记录 AList 流程，不收集全部抓取器的调试堆栈；原有站点报错仍按原来的方式显示。日志文件保留，可按需手动清理 `logs`，不要删除恢复用的 `state.sqlite3`。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest unittest/test_file.py unittest/test_avid.py unittest/test_lib.py unittest/test_func.py unittest/test_alist_client.py unittest/test_alist_pipeline.py unittest/test_alist_names.py unittest/test_config_legacy.py unittest/test_update.py unittest/test_alist_integration.py -q
```

真实 AList 集成测试默认跳过。显式设置 `JAVSP_ALIST_INTEGRATION=1`、`JAVSP_ALIST_TOKEN` 后运行 `unittest/test_alist_integration.py`，会在 `/115` 和 `/local-emby` 下创建随机命名的独立测试目录，只上传很小的人工测试文件。它使用真实命名、NFO、海报生成和远程接口，爬虫和封面下载使用固定测试数据；结束后只清理这些测试目录。
