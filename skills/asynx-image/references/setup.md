# 配置与维护

仅在安装、配置异常、版本管理或旧入口清理时阅读本文档。命令中的 `<skill-dir>` 为当前 Skill 的绝对路径；Windows 可使用 `py` 替代 `python3`。普通生图前不得增加配置检查或更新检查。

## 配置与诊断

```bash
python3 "<skill-dir>/scripts/asynx.py" configure
python3 "<skill-dir>/scripts/asynx.py" config status
python3 "<skill-dir>/scripts/asynx.py" doctor
python3 "<skill-dir>/scripts/asynx.py" doctor --verify
```

- `configure` 由用户在自己的交互式终端输入密钥，Agent 不得代填，也不得索要对话中的密钥。保存成功仅表示已写入配置，未进行联网验证。
- `config status` 用于查看配置来源及环境变量覆盖情况；需要进一步排查副本、权限等问题时使用 `doctor`。两者默认不联网，也不得读取配置原文输出完整密钥。
- 用户要求验证连接或密钥时使用 `doctor --verify`。该命令仅读取模型目录；通过不代表已验证生图权限、余额或实际生成。网络错误不等同于密钥错误。
- 使用公共服务时无需设置地址。仅在用户明确使用自托管部署时运行 `configure --base-url "https://asynx.example.com/api"`。
- API Key 在 LLMAPI 控制台创建：<https://llmapi.site/api-keys>。当前接受任意非空且不含空白字符的 LLMAPI Key；旧版 `asx-` Key 仍保持兼容。

默认配置位置：macOS/Linux 为 `~/.config/asynx/config.json`，Windows 为 `%APPDATA%\Asynx\config.json`。同一系统用户下的多份 Skill 默认共用该文件；显式设置 `ASYNX_CONFIG_PATH` 可指定另一份文件。

`ASYNX_API_KEY` 与 `ASYNX_BASE_URL` 环境变量优先于配置文件；更换文件内密钥后仍异常时，应先检查覆盖来源。普通终端的临时环境变量仅影响该进程及其子进程，桌面 Agent 不一定继承，重启后通常不保留。长期使用应优先保存至配置文件。

旧版本曾按 `XDG_CONFIG_HOME` 保存配置，新版安装器与配置命令会尝试迁移至固定位置，并保留旧文件。不得因发现多份文件而直接删除，应按诊断显示的实际来源处理。

新提交的单任务默认等待 20 秒后首次查询，之后图片约每 4 秒、视频约每 10 秒查询一次。需要调整时，可在上述配置文件中设置 `"initial_poll_delay_seconds": 20`（0–300 秒）；环境变量 `ASYNX_INITIAL_POLL_DELAY_SECONDS` 优先。普通调用无需传参。后台提交、已有任务查询/恢复及批次推进不额外增加该等待；提交响应已是终态时也直接返回。

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

`status` 离线查看版本与位置；`check` 使用 6 小时缓存，`--force` 立即联网。任务成功后的自动检查亦使用缓存，新版提示可能重复出现。用户要求关闭时可设置 `ASYNX_UPDATE_CHECK=off`，但不得将当前子进程的临时设置表述为已永久关闭。

用户要求升级时直接执行 `apply`；默认更新工具识别的所有已登记副本，`--target current` 仅更新当前副本，也支持 `codex` / `claude`。更新不需要 API Key，保留配置、任务数据与图片。检测到本地修改或非本工具管理的副本时，说明原因，不得自动添加 `--force`；后者应通过原安装工具更新。

升级失败会尝试自动恢复；需要恢复上一版本或处理中断的更新时使用 `rollback`。完成后提示用户重新打开会话或刷新 Skill 列表。

更新仅使用已发布的稳定 Release。仓库中尚未发布的改动，须在仓库根目录执行 `git pull` 后重新运行安装器；不得声称 `update apply` 会安装未发布的提交。

## 旧版 asx 迁移

自 0.6.1 起，名称与安装目录改为 `asynx-image`。旧版 `asx` 的更新命令无法完成更名，必须重新安装；不得仅修改目录名。

收到 `legacy_installation_notice` 时，先交付任务结果，再作简短提醒。仅在用户要求安装、升级或清理旧入口时处理以下路径：

- Codex：`~/.agents/skills/asx`、`~/.codex/skills/asx`。
- Claude Code：`~/.claude/skills/asx`。
- Windows：用户主目录下对应的相对路径。

须先确认目标目录中的 `SKILL.md` 与 `scripts/asynx.py` 属于 Asynx，无法确认时说明情况。待旧版命令退出后，将目标入口备份至所有 Agent 的 `skills` 扫描目录之外，并保留本地改动；符号链接仅移动链接本身，不得移动其指向的目标。随后运行新版仓库安装器。

仅处理安装入口，密钥、任务数据库、缓存与图片均需保留，无需迁移数据。macOS/Linux 的默认数据目录包括 `~/.config/asynx`、`~/.local/state/asx` 与 `~/.cache/asx`；Windows 为 `%APPDATA%\Asynx` 与 `%LOCALAPPDATA%\Asynx`。

完成后说明新入口位置与备份位置；安装失败时保留备份，并说明恢复方式。无相关要求时仅作提示，不得移动文件，也不得在普通生图前扫描旧目录。
