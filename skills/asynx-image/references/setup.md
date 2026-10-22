# 配置与维护

仅在安装、配置异常、版本管理或旧入口清理时阅读。命令中的 `<skill-dir>` 是当前 Skill 的绝对路径；Windows 可用 `py` 替代 `python3`。不要在普通生图前增加配置或更新检查。

## 配置与诊断

```bash
python3 "<skill-dir>/scripts/asynx.py" configure
python3 "<skill-dir>/scripts/asynx.py" config status
python3 "<skill-dir>/scripts/asynx.py" doctor
python3 "<skill-dir>/scripts/asynx.py" doctor --verify
```

- `configure` 由用户在自己的交互式终端输入密钥，Agent 不代填、不索要对话中的密钥。保存成功只表示写入配置，没有联网验证。
- `config status` 查看配置来源与环境变量覆盖；需要进一步排查副本、权限等问题时用 `doctor`。两者默认不联网，不要读取配置原文输出完整密钥。
- 用户要求验证连接或密钥时使用 `doctor --verify`。它只读模型目录；通过不代表已验证生图权限、余额或实际生成。网络错误不等于密钥错误。
- 公共服务默认无需设置地址。用户明确使用自托管部署时才运行 `configure --base-url "https://asynx.example.com/api"`。

默认配置位置：macOS/Linux 为 `~/.config/asynx/config.json`，Windows 为 `%APPDATA%\Asynx\config.json`。同一系统用户的多份 Skill 默认共用；显式 `ASYNX_CONFIG_PATH` 可指定另一份文件。

`ASYNX_API_KEY` 和 `ASYNX_BASE_URL` 环境变量优先于文件；更换文件内密钥后仍异常时先检查覆盖来源。普通终端的临时环境变量只影响该进程及子进程，桌面 Agent 不一定继承，重启后通常不保留。长期使用优先保存配置。

旧版曾按 `XDG_CONFIG_HOME` 保存配置，新版安装器和配置命令会尝试迁移到固定位置，并保留旧文件。不因发现多份文件就直接删除，按诊断显示的实际来源处理。

新提交的单任务默认等待 20 秒后首次查询，之后约每 4 秒查询一次。需要调整时，可在上述配置文件中设置 `"initial_poll_delay_seconds": 20`（0–300 秒）；环境变量 `ASYNX_INITIAL_POLL_DELAY_SECONDS` 优先。普通调用无需传参。后台提交、已有任务查询/恢复及批次推进不额外增加这段等待；提交响应已是终态时也直接返回。

## 安装与卸载

仓库地址为 [dawnx/asynx-skills](https://github.com/dawnx/asynx-skills)。以下命令只在克隆的仓库根目录运行，安装后的 Skill 包不包含安装器：

```bash
python3 install.py
python3 install.py --target both
python3 install.py --uninstall
```

支持 `--target codex|claude|both`；默认自动检测。Codex 安装到 `~/.agents/skills/asynx-image`，Claude Code 安装到 `~/.claude/skills/asynx-image`。Windows 使用用户主目录下相同相对路径。

`--skip-config` 跳过配置和验证；`--no-verify` 只跳过连接验证，不能据此宣称已可用。依赖安装在 Skill 的隔离目录中。非交互卸载需要 `--yes`，卸载保留密钥、任务记录和图片。

其他 Agent 应使用其支持的 Skill 安装方式，不能假设本安装器能自动检测它们。

## 更新与回滚

```bash
python3 "<skill-dir>/scripts/asynx.py" update status
python3 "<skill-dir>/scripts/asynx.py" update check
python3 "<skill-dir>/scripts/asynx.py" update check --force
python3 "<skill-dir>/scripts/asynx.py" update apply
python3 "<skill-dir>/scripts/asynx.py" update rollback
```

`status` 离线查看版本和位置；`check` 使用 6 小时缓存，`--force` 立即联网。任务成功后的自动检查也使用缓存，新版提示可以重复出现。用户要求关闭时可设置 `ASYNX_UPDATE_CHECK=off`，但不要把当前子进程的临时设置说成已永久关闭。

用户要求升级时直接执行 `apply`；默认更新工具识别的已登记安装副本，`--target current` 仅更新当前副本，也支持 `codex` / `claude`。更新不需要 API Key，保留配置、任务数据和图片。检测到本地修改或非本工具管理的副本时，说明原因，不自动加 `--force`；后者可通过原安装工具更新。

升级失败会尝试恢复；需要恢复上一版或处理中断的更新时使用 `rollback`。完成后提示重新打开会话或刷新 Skill 列表。

更新只使用已发布的稳定 Release。仓库中尚未发布的改动，需要在仓库根目录 `git pull` 后重新运行安装器；不要声称 `update apply` 会安装未发布提交。

## 旧版 asx 迁移

名称与安装目录从 0.6.1 起改为 `asynx-image`。旧版 `asx` 的更新命令不能完成更名，必须重新安装；不要仅改目录名。

收到 `legacy_installation_notice` 时，先交付任务结果，再简短提醒。只有用户要求安装、升级或清理旧入口时，才处理以下路径：

- Codex：`~/.agents/skills/asx`、`~/.codex/skills/asx`。
- Claude Code：`~/.claude/skills/asx`。
- Windows：用户主目录下对应的相对路径。

确认目标目录中的 `SKILL.md` 和 `scripts/asynx.py` 属于 Asynx，无法确认则说明情况。等待旧版命令退出后，将目标入口移到所有 Agent 的 `skills` 扫描目录之外备份，保留本地改动；符号链接只移动链接本身，不移动目标。然后运行新版仓库安装器。

只处理安装入口，保留密钥、任务数据库、缓存和图片，无需迁移数据。macOS/Linux 的默认数据目录包括 `~/.config/asynx`、`~/.local/state/asx` 和 `~/.cache/asx`；Windows 为 `%APPDATA%\Asynx` 和 `%LOCALAPPDATA%\Asynx`。

完成后报告新入口和备份位置；安装失败时保留备份并说明恢复方式。没有相关指令时仅提示，不移动文件，也不在普通生图前扫描旧目录。
