# Asynx Agent Skills

在 Codex 或 Claude Code 中用自然语言生成、编辑图片，也可以批量追加任务、查询进度和继续上次的工作。支持本地缩放、裁剪、切图和格式转换。

## 安装

需要 Python 3.10+。远程生图还需要 [LLMAPI API Key](https://llmapi.site/api-keys)；本地图片工具无需密钥。旧版 `asx-` Key 仍可兼容。

```bash
git clone https://github.com/dawnx/asynx-skills.git
cd asynx-skills
python3 install.py
```

Windows 将最后一行换成 `py install.py`。

安装器会检测 Codex 和 Claude Code。需要指定目标时，使用 `--target codex`、`--target claude` 或 `--target both`。图片依赖安装在 Skill 内，不修改全局 Python 环境。

首次安装会引导输入 API Key，输入时不显示字符；请勿把密钥发到对话中。安装器随后检查模型目录连接，不生成图片；连接通过不代表已经验证实际生图权限和余额。再次安装会复用已有密钥。

默认安装位置：

| Agent | 用户主目录下的路径 |
| --- | --- |
| Codex | `.agents/skills/asynx-image` |
| Claude Code | `.claude/skills/asynx-image` |

## 开始使用

重新打开会话或刷新 Skill 列表，然后直接描述需求：

- “生成一张机械键盘的产品图，横版。”
- “把刚才那张键盘外壳换成深绿色，其他保持不变。”
- “参考这张图片的风格，生成一张竖版人物卡牌。”
- “批量生成 12 张，再追加 5 张。”
- “查看刚才的任务进度。”
- “把这张图片裁成正方形，再转成 WebP。”

也可以显式调用：Codex 使用 `$asynx-image`，Claude Code 使用 `/asynx-image`。能否自动选中 Skill 取决于 Agent 的设置和请求内容。

单张生成默认等待并展示结果；要求后台提交时，会先返回受理状态。批量任务先返回进度，之后可查询、继续推进或要求等待完成。已提交的任务在远端执行；尚未提交的批次项目需要 Agent 继续轮询，Skill 本身不提供常驻后台服务。

未指定模型时使用脚本固定默认模型，不随机切换。可以明确指定模型、比例或质量；新图用途明确时 Agent 会选择合适画幅，编辑本地图片默认保持原图比例。参考图默认由脚本压缩后上传，要求保留原始编码时请明确说明。

查询进度不会提交新的生图任务。中断后可以继续原任务，下载失败也可单独补下载；批次结果会列出已完成图片和失败项目，不需要重新生成整个批次。

## 密钥与配置

配置保存在当前系统用户目录中，多份 Skill 默认共用：

| 系统 | 配置文件 |
| --- | --- |
| macOS / Linux | `~/.config/asynx/config.json` |
| Windows | `%APPDATA%\Asynx\config.json` |

更换密钥时，在自己的终端运行已安装脚本。macOS / Linux 的 Codex 示例：

```bash
python3 ~/.agents/skills/asynx-image/scripts/asynx.py configure
```

Claude Code 将路径中的 `.agents` 换成 `.claude`。Windows PowerShell 的 Codex 示例：

```powershell
py "$env:USERPROFILE\.agents\skills\asynx-image\scripts\asynx.py" configure
```

“已保存”不等于“连接已验证”，完成后可以回到对话继续使用。长期使用建议保存配置；`ASYNX_API_KEY` 环境变量会覆盖文件中的密钥，普通终端中的临时设置不会在重启后保留。

遇到配置异常，可以让 Agent “检查 Asynx 配置”；详见[配置与排障](skills/asynx-image/references/setup.md#配置与诊断)。

## 更新与卸载

可以让 Agent 检查版本或执行升级。任务成功后最多每 6 小时联网检查一次稳定版，其余使用缓存；发现新版会继续提醒，只有你要求升级时才替换文件。更新服务不可用不会阻止使用当前版本。

手动检查和升级，以 macOS / Linux 的 Codex 安装为例：

```bash
python3 ~/.agents/skills/asynx-image/scripts/asynx.py update check
python3 ~/.agents/skills/asynx-image/scripts/asynx.py update apply
```

更新保留密钥、任务记录和图片。存在本地修改时会停止；不要未经检查强制覆盖。回滚、其他目标和关闭提示见[更新说明](skills/asynx-image/references/setup.md#更新与回滚)。

旧版 `asx` 需要重新安装，不能只改目录名。可让 Agent 备份旧入口后安装新版，密钥和任务记录无需迁移；见[旧版迁移](skills/asynx-image/references/setup.md#旧版-asx-迁移)。

卸载时，在克隆的仓库根目录运行：

```bash
python3 install.py --uninstall
```

Windows 使用 `py`；非交互卸载加 `--yes`。卸载保留密钥、任务记录和生成图片。

## 更多用法

- [操作参考](skills/asynx-image/references/operations.md)：批量任务、本地图片工具、输入限制和中断恢复。
- [模型与提示词](skills/asynx-image/references/model-prompting.md)：复杂编辑、多参考图及图文布局的提示词建议。
- [配置与维护](skills/asynx-image/references/setup.md)：自托管地址、诊断、更新和迁移。

需要自定义工作台或业务流程时，可以调用 Asynx 公开 API，或在自己的后端封装脚本。使用自己的数据库，不直接操作 Skill 的内部状态文件，也不要将密钥暴露到浏览器或项目中。CLI 的结构化输出供程序使用，普通对话无需阅读这些字段。
