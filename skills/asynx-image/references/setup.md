# 配置与维护

仅在安装、配置异常或版本管理时阅读本文档。命令中的 `<skill-dir>` 为当前 Skill 的绝对路径；Windows 可使用 `py` 替代 `python3`。普通生图直接调用即可：缺少配置时命令会给出指引。

## 配置与诊断

```bash
python3 "<skill-dir>/scripts/asynx.py" configure
python3 "<skill-dir>/scripts/asynx.py" config status
python3 "<skill-dir>/scripts/asynx.py" doctor
python3 "<skill-dir>/scripts/asynx.py" doctor --verify
```

- `configure` 只接受交互式终端输入，在非终端环境中直接退出；密钥由用户自己输入。保存成功仅表示已写入配置，未进行联网验证。
- `config status` 用于查看配置来源及环境变量覆盖情况；需要进一步排查副本、权限等问题时使用 `doctor`。两者默认不联网，输出中的密钥只显示前缀。
- 用户要求验证连接或密钥时使用 `doctor --verify`。该命令仅读取模型目录；通过不代表已验证生图权限、余额或实际生成。网络错误不等同于密钥错误。
- 使用公共服务时无需设置地址。仅在用户明确使用自托管部署时运行 `configure --base-url "https://asynx.example.com/api"`。
- API Key 在 LLMAPI 控制台创建：<https://llmapi.site/api-keys>。接受任意非空且不含空白字符的 LLMAPI Key。

默认配置位置：macOS/Linux 为 `~/.config/asynx/config.json`，Windows 为 `%APPDATA%\Asynx\config.json`。同一系统用户下的多份 Skill 默认共用该文件；显式设置 `ASYNX_CONFIG_PATH` 可指定另一份文件。

`ASYNX_API_KEY` 与 `ASYNX_BASE_URL` 环境变量优先于配置文件；更换文件内密钥后仍异常时，应先检查覆盖来源。普通终端的临时环境变量仅影响该进程及其子进程，桌面 Agent 不一定继承，重启后通常不保留。长期使用应优先保存至配置文件。

等待单个任务时，查询请求由服务端保持，任务结束或状态变化后立即返回，因此任务一结束命令即取回结果；没有需要调整的查询间隔设置。批次仍按 `--interval` 定时推进。

## 本机数据与保存目录

未指定 `--output-dir` 时，结果保存到默认目录下按任务命名的子目录。默认目录为运行命令时所在目录的 `generated-images`；需要固定位置时，在配置文件中设置 `"output_dir": "/absolute/path"`，环境变量 `ASYNX_OUTPUT_DIR` 优先。命令行的 `--output-dir` 始终优先于两者。

宿主对单条命令有时长上限时，可设置 `"max_wait_seconds": 100`（0–86400，0 表示等到任务截止）作为等待上限的默认值，环境变量 `ASYNX_MAX_WAIT_SECONDS` 优先，命令行 `--max-wait` 再优先。到时命令返回仍在运行的任务，任务不会取消。

任务记录保存在本机数据库（macOS/Linux 为 `~/.local/state/asx/state.db`，Windows 为 `%LOCALAPPDATA%\Asynx\state.db`）。记录中不保存图片内容：本地参考图处理后暂存在缓存目录的 `uploads` 子目录（macOS/Linux 为 `~/.cache/asx/uploads`，Windows 为 `%LOCALAPPDATA%\Asynx\cache\uploads`），上传一次后由任务引用。未结束的任务与批次仍需要这些暂存文件，清理交给 `prune`。用户要求清理时：

```bash
python3 "<skill-dir>/scripts/asynx.py" prune --dry-run
python3 "<skill-dir>/scripts/asynx.py" prune
python3 "<skill-dir>/scripts/asynx.py" prune --older-than-days 90
```

`prune` 删除不再被任何未结束任务或批次引用的参考图暂存文件、清除已过期的上传记录并压缩数据库，结果中的 `uploads` 给出删除的文件数与释放的字节数；加 `--older-than-days` 时还会删除更早的已结束任务与已结束批次的记录。两者都不删除任何结果文件，也不触碰未结束的任务或批次；被删除记录对应的文件仍在原处，但不再出现在 `recent` 与 `task list` 中。先用 `--dry-run` 向用户说明将处理的数量，再执行。

## 安装与卸载

仓库地址为 [dawnx/asynx-skills](https://github.com/dawnx/asynx-skills)。以下命令须在克隆的仓库根目录运行，安装后的 Skill 包不包含安装器：

```bash
python3 install.py
python3 install.py --target both
python3 install.py --uninstall
```

安装器支持 `--target codex|claude|both`，默认自动检测这两个目标。Codex 安装至 `~/.agents/skills/asynx-image`，Claude Code 安装至 `~/.claude/skills/asynx-image`。Windows 使用用户主目录下相同相对路径。其他支持 Agent Skills 且可执行 Python 脚本的 Agent 同样可用，按下一节安装。

`--skip-config` 跳过配置与验证；`--no-verify` 仅跳过连接验证，使用该参数不代表已可用。依赖安装至 Skill 的隔离目录。非交互卸载须加 `--yes`，卸载保留密钥、任务记录与图片。

### 其他 Agent

通过该 Agent 的 Skill 安装功能导入本仓库的 `skills/asynx-image`，或将整个目录放入其指定的 Skill 目录。须保留 `SKILL.md`、`scripts`、`references` 等目录内容，安装后刷新 Skill 列表。

在用户终端中安装依赖并配置密钥，将 `<skill-dir>` 替换为实际安装路径：

```bash
python3 -m pip install --target "<skill-dir>/scripts/vendor" "Pillow>=10.2,<13"
python3 "<skill-dir>/scripts/asynx.py" configure
```

需要 Python 3.10+，并允许 Agent 执行脚本、访问所选素材与输出目录、连接 Asynx 服务。密钥由用户在终端输入。快捷调用与文件预览方式以宿主支持为准。

通过其他工具安装的副本，使用原工具更新或卸载；手动安装的副本通过替换 Skill 目录更新，卸载时移除该目录，并保留用户目录中的配置与任务数据。

## 更新与回滚

```bash
python3 "<skill-dir>/scripts/asynx.py" update status
python3 "<skill-dir>/scripts/asynx.py" update check
python3 "<skill-dir>/scripts/asynx.py" update check --force
python3 "<skill-dir>/scripts/asynx.py" update apply
python3 "<skill-dir>/scripts/asynx.py" update rollback
```

`status` 离线查看版本与位置；`check` 使用 6 小时缓存，`--force` 立即联网。任务成功后的自动检查亦使用缓存，新版提示可能重复出现。用户要求关闭时可设置 `ASYNX_UPDATE_CHECK=off`；该环境变量只对设置它的进程生效，长期关闭需由用户写入自己的环境配置。

用户要求升级时直接执行 `apply`；默认更新工具识别的所有已登记副本，`--target current` 仅更新当前副本，也支持 `codex` / `claude`。更新不需要 API Key，保留配置、任务数据与图片。检测到本地修改或非本工具管理的副本时，命令会停止并说明原因。`--force` 会覆盖本地修改，仅在用户明确要求时使用；非本工具管理的副本通过原安装工具更新。

升级失败会尝试自动恢复；需要恢复上一版本或处理中断的更新时使用 `rollback`。完成后提示用户重新打开会话或刷新 Skill 列表。

更新仅使用已发布的稳定 Release。仓库中尚未发布的改动，须在仓库根目录执行 `git pull` 后重新运行安装器。
