# Agent CLI

本 fork 为 LDDC 增加独立的 `lddc` 入口，便于脚本和 agent 调用。正常命令输出一个带版本号的 JSON 对象；诊断信息写入 stderr。`--help` 和 `--version` 输出普通文本。

## 安装与快速开始

建议使用 Python 3.11–3.13。CLI 复用现有核心，仍需 PySide6，但只创建 `QCoreApplication`，不打开窗口，也不需要启动桌面应用。

```sh
git clone --branch feat/agent-cli https://github.com/wpybtw/LDDC.git
cd LDDC
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/lddc capabilities
.venv/bin/lddc doctor
.venv/bin/lddc config init
.venv/bin/lddc config show
```

Windows 将 `.venv/bin/` 换成 `.venv\Scripts\`。激活虚拟环境后可直接使用 `lddc`；`python -m LDDC.cli` 是等价入口。已有 `python -m LDDC` GUI 入口保持不变。

`doctor` 只检查依赖是否可发现，不联网，也不验证各来源服务是否可用。

## 常用配置

完整模板见 [examples/cli.toml](../examples/cli.toml)，也可用 `lddc config init --output ./lddc.toml` 生成。只需填写要覆盖的字段。

```toml
[search]
sources = ["NE", "QM"]
limit = 10

[network]
timeout_seconds = 20.0
retries = 1
workers = 2
proxy = "" # 例如 http://127.0.0.1:7890；留空继承代理环境变量

[match]
min_score = 80.0
duration_tolerance_ms = 4000
top_k = 3

[files]
recursive = true

[output]
format = "lrc"
languages = ["orig", "ts"]
offset_ms = 0
directory = "exports"
overwrite = false
lrc_ms_digits = 3
add_end_timestamp = false
```

设置优先级：**本次命令的参数 > TOML 配置 > 内置默认值**。读取配置和命令参数覆盖不会改写 TOML 文件。未知字段、错误类型、越界值、重复来源等会返回退出码 2。

配置文件选择优先级：`--config PATH` > `LDDC_CLI_CONFIG` 环境变量 > 默认位置：

| 系统 | 默认路径 |
| --- | --- |
| macOS | `~/Library/Application Support/LDDC-CLI/config.toml` |
| Linux | `$XDG_CONFIG_HOME/lddc/config.toml`，未设置时为 `~/.config/lddc/config.toml` |
| Windows | `%APPDATA%/LDDC-CLI/config.toml` |

默认文件不存在时使用内置默认值；显式指定但不存在的文件会报错。`config init` 永不覆盖已有文件；`config validate` 校验并返回生效设置。

`output.directory` 的相对路径以**配置文件所在目录**为基准；命令行 `--output` 的相对路径以当前工作目录为基准。`directory` 为空且未传 `--output` 时，转换结果放在 JSON 的 `data.content` 中。

缓存、日志和核心内部配置使用配置文件旁的 `state/`。可用 `--state-dir PATH` 改变位置。它们与桌面应用的默认配置和缓存隔离；CLI 设置不会写入 GUI 的设置文件。

`--config`、`--state-dir`、`--json` 是全局选项，必须放在子命令之前：

```sh
lddc --config ./lddc.toml --state-dir ./runtime --json config show
```

`--json` 用于显式声明意图，JSON 本来就是默认输出。

## 命令

### 检查音频

```sh
lddc inspect "/path/to/song.flac"
lddc inspect "/path/to/album" --recursive
lddc inspect "/path/to/album.cue"
```

输出 `data.items` 中的歌曲信息，时长 `duration` 的单位为**毫秒**。支持核心可读取的音频类型。目录扫描不额外读取外置 CUE，以免与已分轨文件重复；可显式传入 CUE 文件检查曲目信息。损坏文件会单独记录错误，同时保留其他文件结果。此命令不联网、不修改标签。

### 搜索歌曲、专辑、歌单

```sh
lddc search --title "遍路" --artist "中島みゆき" --sources NE
lddc search --query "中島みゆき" --type album --sources NE QM --page 1
lddc search --query "example" --sources LRCLIB --timeout 15 --retries 0
```

`--query` 与 `--title` / `--artist` 互斥。组合查询时使用 `artist - title`。来源代码：`NE` 网易云、`QM` QQ 音乐、`KG` 酷狗、`LRCLIB` LRCLIB。LRCLIB 只支持歌曲搜索。

`--limit` 限制**每个来源当前页**返回的最大条目数，不跨页补齐。返回顺序遵循配置中的来源顺序及各来源原有顺序。

`search` 和 `match` 共用 `--sources`、`--limit`、`--timeout`、`--retries`、`--workers`、`--proxy`。来源在独立子进程中执行；超时覆盖启动、来源初始化和请求整个过程。`retries = 1` 表示最多两次尝试；`workers` 控制同时执行的来源数。批量匹配逐首处理，匹配失败时可能追加一次仅标题搜索，因此目录任务总耗时可显著超过单次超时。

配置/参数中的代理支持不含凭据的 HTTP(S) URL。需要认证时使用代理环境变量；`proxy = ""` 继承 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 等环境设置。

### 为本地歌曲生成匹配候选

```sh
lddc match "/path/to/album" --sources NE --dry-run --output ./candidates.json
lddc match "/path/to/song.flac" --min-score 80 --duration-tolerance-ms 2000 --top-k 3
```

当前 `match` **始终是只读候选匹配**，不写音频、不下载歌词。报告列出标题/歌手/专辑分项分数、时长差、可接受候选与被拒绝候选、版本风险提示。`--dry-run` 可显式标注这一行为；没有 apply 模式。`--output` 只保存 JSON 报告且拒绝覆盖已有文件。

分数是相似度启发式，**不是正确率，也不是歌词同步验证**。评分复用核心文本算法；标题和歌手完全相同时，即使专辑不同仍可能得到 100 分。请检查 `album_mismatch`、`remaster_candidate`、`duration_unknown` 等警告。缺少时长时不能执行时长过滤，会保留警告。所有候选的 `audio_sync_verified` 都是 `false`。

CLI 对标题末尾括号中的 `Remaster` / `Remastered`（可带年份）先移除版本标记再比较，避免短日文歌名被后缀淹没。报告同时保留 `components.title_raw` 原始评分和 `title_comparison` 实际比较文本，并提示重制版候选；不会移除 `Live` 等演出版本后缀。这只影响候选排序，不证明重制版与原版歌词时间轴一致。

### 离线转换已有歌词

```sh
lddc convert ./input.lrc --format srt --offset-ms 500 --output ./output.srt
lddc convert ./input.lrc --format lrc --languages orig ts
lddc convert ./input.lrc --format json --offset-ms 0 --output ./lyrics.json
```

输入是本地歌词文件，读取格式由现有 `LocalAPI` 支持。输出格式为 `lrc`、`verbatim-lrc`、`enhanced-lrc`、`srt`、`ass`、`json`。语言代码：`orig` 原文、`ts` 译文、`roma` 罗马音。

语言选项只选择输入文件已包含的内容，**不会自动翻译或补齐罗马音**。JSON 输出保留内部全部语言，忽略语言筛选，且要求偏移为零。其他格式的正偏移使歌词显示时间延后。

写文件采用临时文件加原子发布。默认不覆盖目标；可用 `--overwrite` 显式开启，或用 `--no-overwrite` 覆盖配置中的开启值。输入文件自身始终受保护，包括指向同一文件的符号链接和硬链接。

## 机器输出与退出码

```json
{
  "schema_version": 1,
  "command": "search",
  "status": "ok",
  "exit_code": 0,
  "data": {"items": [], "sources": []},
  "errors": []
}
```

此处省略了命令相关字段。`schema_version` 标识协议版本；`data` 随命令变化。`status` 为 `ok`、`partial` 或 `error`。非零退出码且仍含 `data.items` 时返回 `partial`，其中可能包含未匹配的本地曲目；不能只凭 `partial` 推断已有成功候选。机器调用需同时检查退出码、顶层 `errors` 和逐曲目 `status`。

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令成功；搜索零结果也属于成功 |
| 1 | 内部错误 |
| 2 | 参数、配置或输入内容无效 |
| 3 | 匹配中至少一首没有候选，或没有待匹配音频 |
| 4 | 来源错误；其他成功来源的结果仍保留 |
| 5 | 文件读写错误 |
| 6 | 报告的错误全部为来源尝试超时 |
| 7 | 缺少依赖 |
| 130 | 用户中断 |

批量结果同时出现文件和来源错误时，`match` 优先报告来源错误；仅文件读取失败则返回 5。存在操作错误时优先于“无候选”返回，完整原因在 `errors`。错误项包含稳定 `code`，并按情况携带 `source`、`path`、`type` 等字段。来源异常不直接回显可能包含凭据的原始异常文本。

## 当前范围与验证

本版本提供配置、环境检查、音频检查、远程元数据搜索、候选排序、已有本地歌词转换。尚无远程歌词下载命令、自动标签写入、翻译服务命令、断点续跑或 JSONL 流式输出。现有桌面功能未搬入 CLI。匹配结果也不证明来源一定有歌词。

离线测试不依赖音乐服务，也不访问用户曲库；使用临时 WAV 和自编测试歌词。运行：

```sh
python -m pip install pytest ruff
python -m pytest --confcutdir=tests_cli tests_cli -q
ruff check LDDC/cli tests_cli
```

`tests_cli` 独立于原有 GUI 测试的自动初始化 fixture。在线来源的可用性需另行实测，离线测试通过不代表外部 API 永久可用。
