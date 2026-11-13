# 配置与维护

仅在首次设置、配置异常或版本管理时阅读本文档。命令中的 `<skill-dir>` 为当前 Skill 的绝对路径；Windows 可使用 `py` 替代 `python3`。普通生图直接调用即可：缺少配置时命令会给出指引。

## 首次设置

```bash
python3 "<skill-dir>/scripts/asynx.py" setup
```

`setup` 不需要 API Key，可由 Agent 直接运行。它做两件事：

- 检查图片处理依赖 Pillow。当前 Python 已能导入时不做任何安装；否则用 pip 安装到用户目录下的依赖目录（见[本机数据与保存目录](#本机数据与保存目录)），该目录不在 Skill 内，Skill 更新或重装后依赖仍在。
- 返回密钥状态与配置命令：`configured` 表示是否已有可用密钥，`configure_command` 是带本机绝对路径的配置命令，`message` 是可直接转述的说明。输出不包含密钥。

`configured` 为 `false` 时，把 `configure_command` 交给用户在自己的终端运行。命令报告 `pillow_not_installed` 或 `missing_api_key` 时同样先运行 `setup`。

安装失败时返回 `pip_not_available`（当前 Python 没有 pip）或 `dependency_install_failed`（网络或目录权限问题），`error.details.manual_command` 是可在终端手动执行的安装命令。依赖目录按 Python 小版本区分，更换 Python 版本后重新运行 `setup`。

## 配置与诊断

```bash
python3 "<skill-dir>/scripts/asynx.py" configure
python3 "<skill-dir>/scripts/asynx.py" config status
python3 "<skill-dir>/scripts/asynx.py" doctor
python3 "<skill-dir>/scripts/asynx.py" doctor --verify
```

- `configure` 只接受交互式终端输入，在非终端环境中直接退出；密钥由用户自己输入。保存成功仅表示已写入配置，未进行联网验证。
- `config status` 用于查看配置来源及环境变量覆盖情况。`doctor` 进一步给出当前副本的版本（`skill_version`）与位置（`skill_path`）、所用 Python、Pillow 是否可用（`pillow`）以及配置文件权限，问题列在 `issues` 中。两者默认不联网，输出中的密钥只显示前缀。
- 用户要求验证连接或密钥时使用 `doctor --verify`。该命令仅读取模型目录；通过不代表已验证生图权限、余额或实际生成。网络错误不等同于密钥错误。
- 使用公共服务时无需设置地址。仅在用户明确使用自托管部署时运行 `configure --base-url "https://asynx.example.com/api"`。
- API Key 在 LLMAPI 控制台创建：<https://llmapi.site/api-keys>。接受任意非空且不含空白字符的 LLMAPI Key。

默认配置位置：macOS/Linux 为 `~/.config/asynx/config.json`，Windows 为 `%APPDATA%\Asynx\config.json`。同一系统用户下的多份 Skill 默认共用该文件；显式设置 `ASYNX_CONFIG_PATH` 可指定另一份文件。

`ASYNX_API_KEY` 与 `ASYNX_BASE_URL` 环境变量优先于配置文件；更换文件内密钥后仍异常时，应先检查覆盖来源。普通终端的临时环境变量仅影响该进程及其子进程，桌面 Agent 不一定继承，重启后通常不保留。长期使用应优先保存至配置文件。

等待单个任务时，查询请求由服务端保持，任务结束或状态变化后立即返回，因此任务一结束命令即取回结果；没有需要调整的查询间隔设置。批次仍按 `--interval` 定时推进。

## 本机数据与保存目录

未指定 `--output-dir` 时，结果保存到默认目录下按任务命名的子目录。默认目录为运行命令时所在目录的 `generated-images`；需要固定位置时，在配置文件中设置 `"output_dir": "/absolute/path"`，环境变量 `ASYNX_OUTPUT_DIR` 优先。命令行的 `--output-dir` 始终优先于两者。

宿主对单条命令有时长上限时，可设置 `"max_wait_seconds": 100`（0–86400，0 表示等到任务截止）作为等待上限的默认值，环境变量 `ASYNX_MAX_WAIT_SECONDS` 优先，命令行 `--max-wait` 再优先。到时命令返回仍在运行的任务，任务不会取消。

任务记录保存在本机数据库（macOS/Linux 为 `~/.local/state/asx/state.db`，Windows 为 `%LOCALAPPDATA%\Asynx\state.db`）。`setup` 安装的 Pillow 位于数据库同级的 `vendor/py<主版本><次版本>` 目录（例如 `~/.local/state/asx/vendor/py312`，Windows 为 `%LOCALAPPDATA%\Asynx\vendor\py312`），环境变量 `ASYNX_VENDOR_PATH` 可指定其他目录；删除该目录后重新运行 `setup` 即可恢复。记录中不保存图片内容：本地参考图处理后暂存在缓存目录的 `uploads` 子目录（macOS/Linux 为 `~/.cache/asx/uploads`，Windows 为 `%LOCALAPPDATA%\Asynx\cache\uploads`），上传一次后由任务引用。未结束的任务与批次仍需要这些暂存文件，清理交给 `prune`。用户要求清理时：

```bash
python3 "<skill-dir>/scripts/asynx.py" prune --dry-run
python3 "<skill-dir>/scripts/asynx.py" prune
python3 "<skill-dir>/scripts/asynx.py" prune --older-than-days 90
```

`prune` 删除不再被任何未结束任务或批次引用的参考图暂存文件、清除已过期的上传记录并压缩数据库，结果中的 `uploads` 给出删除的文件数与释放的字节数；加 `--older-than-days` 时还会删除更早的已结束任务与已结束批次的记录。两者都不删除任何结果文件，也不触碰未结束的任务或批次；被删除记录对应的文件仍在原处，但不再出现在 `recent` 与 `task list` 中。先用 `--dry-run` 向用户说明将处理的数量，再执行。

## 安装、更新与卸载

仓库地址为 [dawnx/asynx-skills](https://github.com/dawnx/asynx-skills)。Skill 通过以下渠道之一安装，更新与卸载使用同一渠道；三者都保留用户目录中的密钥、任务数据、依赖目录与已生成文件。安装或更新后重新打开会话或刷新 Skill 列表，首次安装后运行一次 `setup`。

### Claude Code 插件

仓库同时是插件市场 `asynx-skills`，提供插件 `asynx`。在会话中输入：

```text
/plugin marketplace add dawnx/asynx-skills
/plugin install asynx@asynx-skills
/plugin marketplace update asynx-skills
```

前两条完成安装，第三条在需要时获取新版本；也可在 `/plugin` 的 Marketplaces 页为该市场开启自动更新。卸载在终端运行 `claude plugin uninstall asynx@asynx-skills`。通过插件安装的 Skill 以 `/asynx:asynx-image` 调用。插件按版本号发布：仓库中尚未发布新版本号的改动不会下发。插件更新后安装目录可能变化，此前记下的配置命令失效时重新运行 `setup` 获取。

### skills 命令行工具

适用于 Codex 及其他支持 Agent Skills 的 Agent，需要 Node.js 22.20+：

```bash
npx skills add dawnx/asynx-skills -g
npx skills update asynx-image -g
npx skills remove asynx-image -g
```

`add` 会让用户选择要安装到的 Agent，`-a <agent>`（如 `-a codex`）直接指定，`-y` 跳过确认。`-g` 表示用户目录：Codex 为 `~/.agents/skills/asynx-image`，Claude Code 为 `~/.claude/skills/asynx-image`；省略 `-g` 则安装到当前项目（Codex 为 `.agents/skills/asynx-image`），此时 `update` 与 `remove` 也省略 `-g`。`npx skills check` 只检查是否有更新。其他 Agent 的目录见该工具的[说明](https://github.com/vercel-labs/skills/blob/main/docs/agents.md)。

### 手动安装

将仓库中的 `skills/asynx-image` 整个目录放入 Agent 的 Skill 目录，保留 `SKILL.md`、`scripts`、`references` 与 `agents`。更新时用新版目录替换，卸载时移除该目录。

运行环境需要 Python 3.10+，并允许 Agent 执行脚本、访问所选素材与输出目录、连接 Asynx 服务。快捷调用与文件预览方式以宿主支持为准。
