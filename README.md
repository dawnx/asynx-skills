# Asynx Agent Skills

一个面向 Codex 和 Claude Code 的开源 Agent Skill。它通过 Asynx 异步 Task API 生成、编辑和批量处理图片。
客户端只使用 Python 标准库，并在运行时读取当前部署的图片模型目录。

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

安装器会自动检测 Codex 和 Claude Code，把同一个 `asx` skill 安装到对应目录。首次安装时只隐藏询问一次 API Key，
并用只读模型目录请求检查配置。重复运行安装器会复用已有 Key。

也可以显式指定目标：

```bash
python3 install.py --target codex
python3 install.py --target claude
python3 install.py --target both
```

Codex 可以对普通图片请求自动调用 skill，也可以显式使用 `$asx`。Claude Code 可以隐式调用，也可以使用 `/asx`。

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

安装器会自动配置公共 Asynx 服务。之后需要轮换 Key 时，在自己的终端运行：

```bash
python3 skills/asx/scripts/asynx.py configure
```

Windows 将 `python3` 替换为 `py`。配置过程只询问 API Key，不要把 Key 粘贴到 Agent 对话中。

配置文件位于 macOS/Linux 的 `~/.config/asynx/config.json`，Windows 位于 `%APPDATA%\Asynx\config.json`，
并尽量设置为仅当前用户可读写。

自托管部署可以显式指定 Base URL：

```bash
python3 skills/asx/scripts/asynx.py configure \
  --base-url "https://asynx.example.com/api"
```

环境变量优先级高于配置文件：

```bash
export ASYNX_API_KEY="asx-your-api-key"
export ASYNX_BASE_URL="https://asynx.llmapi.site/api"
```

## 单个任务

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

默认命令会等待 Task 完成并下载 Asset。`--detach` 只提交任务；之后用 `wait TASK_ID` 继续等待和下载。

## 批量任务与追加

批次状态保存在本机 SQLite：macOS/Linux 默认是 `~/.local/state/asx/state.db`，Windows 默认是
`%LOCALAPPDATA%\Asynx\state.db`。Asynx 仍然为每张图片创建独立 Task，因此批次可以追加、暂停、恢复和单项失败。

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

`batch poll` 可以由 Agent 定时调用；进程中断后再次调用会从本地状态继续，已提交 Task 使用原幂等键，不会重复创建。
如果需要前台持续等待，可以使用 `batch wait BATCH_ID`。查看 Asynx 历史任务：

```bash
python3 skills/asx/scripts/asynx.py history --limit 20
```

批次图片默认保存到 `generated-images/<批次 ID>/`。输出文件、API Key、本地状态数据库都不应提交到 Git。

## 测试

客户端内部按职责拆分，`skills/asx/scripts/asynx.py` 只是稳定入口：

```text
asxlib/config.py    配置、API Key 和本地路径
asxlib/client.py    HTTP、重试和 Asynx API
asxlib/images.py    模型能力与图片输入
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
