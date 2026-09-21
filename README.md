# Asynx Agent Skills

一个面向 Codex 和 Claude Code 的开源 Agent Skill。它通过 Asynx 异步 Task API 生成、编辑和批量处理图片。
客户端使用 Python 实现；安装器会隔离管理参考图处理所需的 Pillow，并只在需要解析模型名称时读取当前部署的图片模型目录。

## 要求

- Python 3.10 或更高版本
- 一个同时拥有 `tasks:read` 和 `tasks:write` Scope 的 Asynx API Key

公共服务可以在 <https://asynx.llmapi.site/api-keys> 创建 Key。

## 安装

克隆仓库并运行安装器：

```bash
git clone https://github.com/dawnx/asynx-skills.git
cd asynx-skills
python3 install.py
```

Windows 使用：

```powershell
git clone https://github.com/dawnx/asynx-skills.git
cd asynx-skills
py install.py
```

安装器会自动检测 Codex 和 Claude Code，把同一个 `asx` skill 安装到对应目录，并把 Pillow 安装到 skill 自身的
`scripts/vendor/`，不会修改用户的全局 Python 环境。首次安装时只隐藏询问一次 API Key，并用只读模型目录请求检查配置。
重复运行安装器会复用已有 Key。

也可以显式指定目标：

```bash
python3 install.py --target codex
python3 install.py --target claude
python3 install.py --target both
```

Codex 可以对普通图片请求自动调用 skill，也可以显式使用 `$asx`。Claude Code 可以隐式调用，也可以使用 `/asx`。

在 Codex 中不需要输入命令。直接说“生成一张机械键盘”“把刚才那张换成蓝色”“再来 5 张”即可；Agent 会自动选择生成、编辑、最近结果或批次操作，默认等待完成并展示最终图片。只有结果存在多个合理候选时才会询问你选择哪一张。

如果需要自定义工作台、网页、脚本或业务工作流，可以直接调用 Asynx 公开 API，也可以在自己的后端封装已安装的 `asynx.py`。实现方式、技术栈、页面结构和业务数据库由用户自行决定；不要直接读取或修改 skill 的本地 `state.db`，也不要把 API Key 暴露到浏览器、前端构建产物、URL 或日志中。

运行安装器可以更新已有安装，已保存的 API Key 会被复用。

更新到新版本：

```bash
git pull
python3 install.py
```

卸载 skill：

```bash
python3 install.py --uninstall
```

非交互环境使用 `--yes`。卸载只删除 Codex/Claude Code 中的 skill 文件，不删除 API Key、批次数据库或已经生成的图片。

## 配置

安装器会自动配置公共 Asynx 服务。配置必须在用户自己的交互式终端中完成，不要让 Agent 代为输入 Key，也不要把 Key
粘贴到对话中。之后需要轮换 Key 时，运行对应的已安装脚本，而不是依赖当前工作目录中的仓库相对路径。

macOS/Linux：

```bash
# Codex
python3 ~/.agents/skills/asx/scripts/asynx.py configure

# Claude Code
python3 ~/.claude/skills/asx/scripts/asynx.py configure
```

Windows PowerShell：

```powershell
# Codex
py "$env:USERPROFILE\.agents\skills\asx\scripts\asynx.py" configure

# Claude Code
py "$env:USERPROFILE\.claude\skills\asx\scripts\asynx.py" configure
```

配置文件位于 macOS/Linux 的 `~/.config/asynx/config.json`，Windows 位于 `%APPDATA%\Asynx\config.json`，
并尽量设置为仅当前用户可读写。Codex、Claude Code 和同一用户安装的多份 `asx` 默认共用这个配置；更新或卸载 skill
不会删除它。只有切换 OS 用户、HOME 或显式设置不同的 `ASYNX_CONFIG_PATH` 时才会使用另一份配置。旧版本曾按
`XDG_CONFIG_HOME` 保存配置，新版安装器会在能发现旧文件时迁移到上述固定位置，并保留旧文件。

自托管部署可以显式指定 Base URL：

```bash
python3 ~/.agents/skills/asx/scripts/asynx.py configure \
  --base-url "https://asynx.example.com/api"
```

环境变量优先级高于配置文件，但普通 `export` 只对当前终端及其子进程有效，电脑重启或从桌面启动 Codex 后通常不会保留。
需要长期使用时优先运行 `configure` 写入用户配置文件：

```bash
export ASYNX_API_KEY="asx-your-api-key"
export ASYNX_BASE_URL="https://asynx.llmapi.site/api"
```

配置异常时先运行本地诊断。输出只显示 Key 的脱敏前缀，不会打印完整 Key；`doctor` 默认不联网，只有 `--verify` 会请求
Asynx 的只读模型目录：

```bash
python3 ~/.agents/skills/asx/scripts/asynx.py config status
python3 ~/.agents/skills/asx/scripts/asynx.py doctor
python3 ~/.agents/skills/asx/scripts/asynx.py doctor --verify
```

Claude Code 或 Windows 用户把命令中的脚本路径替换为上面对应的已安装路径。诊断会报告实际配置文件、凭据来源、环境变量覆盖、
已安装副本和版本差异；若仍提示缺少 Key，请按诊断输出的绝对命令在交互式终端重新运行 `configure`。

## 单任务与本地账本

```bash
python3 skills/asx/scripts/asynx.py models

python3 skills/asx/scripts/asynx.py generate \
  --prompt "混凝土展厅中的红色椅子" \
  --model "gpt-image-2" \
  --image-size 2K \
  --aspect-ratio 16:9

python3 skills/asx/scripts/asynx.py edit \
  --prompt "把背景替换成白色摄影棚" \
  --image /absolute/path/source.png
```

参考图生成必须把图片与 Prompt 分开传入：

```powershell
py skills/asx/scripts/asynx.py generate `
  --prompt "参考该图片的风格和色调，生成一张卡牌立绘" `
  --reference "E:\ARPG\sanguo\heroCard\006linchong.png"
```

Prompt 中不要放图片路径。客户端不会替 Agent 猜测或上传 Prompt 中的本地文件；请始终使用 `--reference` 或 `--image`。

### 参考图处理

本地路径和 Data URL 输入默认都会被完整解码、校正 EXIF 方向并检查尺寸；必要时只按尺寸上限缩放一次，再以 WebP Q82
编码一次。客户端不会因为文件较大或网络较慢而反复降低质量或尺寸，避免不可控的画质损失。固定限制如下：

- 原图只支持 PNG、JPEG 和 WebP，每张源数据最大 25 MB。
- 处理后每张图最大 5 MB、最长边 1600 px、总像素不超过 2,560,000。
- 每个任务最多 5 张输入图，本地路径和 Data URL 的二进制数据合计最大 20 MB。
- 整个 JSON 请求体最大 32 MB。

5 MB 是处理后单图的硬上限：一次 Q82 编码后仍超限会直接拒绝请求，不会继续降质重试。较大的本地参考图请求会在 CLI
结果的 `warnings` 中提示上传开销；弱网环境下提交可能变慢，但不会改变图片处理策略。

只有用户明确要求保留原始格式或原始字节时才添加 `--keep-reference-original`。该选项只跳过 WebP 重编码；图片仍会被完整解码，
且必须满足格式、5 MB、1600 px、2,560,000 像素、总量和请求体限制，不能用来绕过校验。HTTP(S) 图片 URL 不由客户端下载，
而是保留 URL 交给 Asynx 服务端拉取和校验；图片数量和请求体限制仍然适用。

```bash
python3 skills/asx/scripts/asynx.py generate \
  --prompt "保留参考图的纹理细节，生成产品海报" \
  --reference /absolute/path/source.png \
  --keep-reference-original
```

默认命令会等待 Task 完成并下载 Asset；用户要求立即受理、后台运行或稍后查询时添加 `--detach`。`--detach` 返回 Task ID，
但不代表任务已经完成。每次提交都会写入本地 `state.db`，成功下载的 Asset 会自动建立索引。

中断、会话切换或需要稍后推进时使用本地任务接口：

```bash
python3 skills/asx/scripts/asynx.py task list
python3 skills/asx/scripts/asynx.py task status TASK_ID
python3 skills/asx/scripts/asynx.py task poll [TASK_ID]
python3 skills/asx/scripts/asynx.py task recover
python3 skills/asx/scripts/asynx.py task cancel TASK_ID
python3 skills/asx/scripts/asynx.py asset list TASK_ID
```

`task list` 查询本地任务；`task status` 查看本地快照；`task poll` 刷新一个或全部未完成任务并下载已完成 Asset；`task recover`
修复上次中断的提交状态并复用原幂等键；`task cancel` 请求上游取消，若返回 `cancel_requested`，上游可能继续执行并计费。不要为同一请求重新生成幂等键。

### 查找和继续编辑结果

可以按 Prompt、模型、Task ID 或文件信息查找最近结果：

```bash
python3 skills/asx/scripts/asynx.py recent --latest
python3 skills/asx/scripts/asynx.py recent --query "红色椅子"
```

编辑已经下载的结果时，无需手动定位文件路径，直接引用原 Task：

```bash
python3 skills/asx/scripts/asynx.py edit \
  --from-task TASK_ID \
  --prompt "把背景替换成白色摄影棚"
```

当用户说“刚才那张”“上一张”时，Agent 使用 `recent --latest`；有文字描述时再加 `--query`。不要扫描整个磁盘或请求远程 `history`。

`asset list` 或 `recent` 返回的本地文件不存在时，先报告缺失，再让用户选择重新下载或提供新图片。

默认模型和完整规范模型名会直接提交，不增加模型目录前置请求。`Gemini 3.1`、`seedream` 这类模糊名称
需要查询模型目录，结果会按 Base URL 缓存 5 分钟。模型缓存不包含 API Key：macOS/Linux 默认位于
`~/.cache/asx/models.json`，Windows 默认位于 `%LOCALAPPDATA%\Asynx\cache\models.json`。

单图片 Task 的首次轮询从 2 秒开始，后续退避最大为 4 秒。命令结果中的 `timings` 会分别记录本地准备、提交、等待和下载耗时；
下载阶段还会细分首字节、传输和本地保存耗时，方便定位性能问题。

## 批量任务与追加

任务和批次状态保存在统一的本机 SQLite v1 账本：macOS/Linux 默认是 `~/.local/state/asx/state.db`，Windows 默认是
`%LOCALAPPDATA%\Asynx\state.db`。Asynx 仍然为每张图片创建独立 Task，因此批次可以追加、暂停、恢复和单项失败。
首次使用新版本时会对旧的无版本核心账本执行一次破坏式重建；旧批次表和图片文件不会被删除，但旧单任务账本不会迁移。

创建一个 12 项批次：

```bash
python3 skills/asx/scripts/asynx.py batch create \
  --prompt "赛博朋克城市夜景" \
  --model "gpt-image-2" \
  --total 12
```

命令会返回批次 ID，并立即提交少量首批 Task。追加 5 项：

```bash
python3 skills/asx/scripts/asynx.py batch add BATCH_ID \
  --total 5 \
  --prompt "赛博朋克城市雨夜"
```

省略批次 ID 时使用最近的活动批次。追加时省略的参数继承原批次设置。

推进一次状态和下载：

```bash
python3 skills/asx/scripts/asynx.py batch poll BATCH_ID
python3 skills/asx/scripts/asynx.py batch status BATCH_ID --items
```

暂停、恢复或取消：

```bash
python3 skills/asx/scripts/asynx.py batch pause BATCH_ID
python3 skills/asx/scripts/asynx.py batch resume BATCH_ID
python3 skills/asx/scripts/asynx.py batch cancel BATCH_ID
```

取消只向上游发出请求；已经开始的 Task 可能继续运行并计费。若返回 `cancel_requested`，继续用 `batch poll` 观察最终状态。

`batch poll` 可以由 Agent 定时调用；进程中断后再次调用会从本地状态继续，已提交 Task 使用原幂等键，不会重复创建。
批次级追加、暂停、恢复、取消和统计只使用 `batch` 命令；单 Task 使用上面的 `task` 命令，不要交叉操作。
如果需要前台持续等待，可以使用 `batch wait BATCH_ID`。查看 Asynx 远端历史任务（仅用于显式诊断）：

```bash
python3 skills/asx/scripts/asynx.py history --limit 20
```

批次图片默认保存到 `generated-images/<批次 ID>/`。输出文件、API Key、本地状态数据库都不应提交到 Git。

## 测试

客户端内部按职责拆分，`skills/asx/scripts/asynx.py` 只是稳定入口：

```text
asxlib/config.py    配置、API Key 和本地路径
asxlib/client.py    HTTP、重试和 Asynx API
asxlib/images.py    模型能力与任务输入
asxlib/reference_media.py  参考图解码、归一化与限制校验
asxlib/artifacts.py  本地生成结果索引与历史引用
asxlib/state.py      版本化 Task、Asset 和 Event 账本
asxlib/tasks.py     单 Task 生命周期和 Asset 下载
asxlib/batches.py   SQLite 批次、追加和恢复
asxlib/cli.py       命令解析与分发
```

测试只使用本地假 HTTP 服务，不会调用真实供应商：

```bash
python3 -m unittest discover -s tests -v
```

也可以单独启动 Mock Asynx 服务进行手动联调。服务启动后会打印随机本地地址：

```bash
python3 tests/mock_asynx_server.py
```

另一个终端使用打印出的地址：

```bash
export ASYNX_API_KEY="asx-mock-test"
export ASYNX_BASE_URL="http://127.0.0.1:打印出的端口"
python3 skills/asx/scripts/asynx.py batch create --prompt "本地测试" --total 3
```
