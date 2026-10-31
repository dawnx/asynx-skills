# Asynx Agent Skills

在支持 Agent Skills、能够运行 Python 脚本的 Agent 中，用自然语言生成图片、修改图片或创作视频。支持参考图、批量生成、进度查询和中断后继续，也可以在本地缩放、裁剪、切图和转换格式。

## 安装

需要 Python 3.10+。生成图片或视频需要 [LLMAPI API Key](https://llmapi.site/api-keys)，本地图片处理无需密钥。

### Codex / Claude Code

可使用仓库自带的安装器：

```bash
git clone https://github.com/dawnx/asynx-skills.git
cd asynx-skills
python3 install.py
```

Windows 将最后一行换成 `py install.py`。

安装器会检测 Codex 和 Claude Code，并引导你输入 API Key。请在终端中输入密钥，不要发送到聊天中。

指定安装到某个 Agent 时，可使用 `python3 install.py --target codex`、`--target claude` 或 `--target both`。

默认安装位置：

| Agent | 用户主目录下的路径 |
| --- | --- |
| Codex | `.agents/skills/asynx-image` |
| Claude Code | `.claude/skills/asynx-image` |

### 其他 Agent

使用所用 Agent 的 Skill 导入或安装功能，选择本仓库的 `skills/asynx-image` 目录，并保留其中的脚本和参考文档。也可以将整个目录放入该 Agent 的 Skill 目录，具体位置以其说明为准。

运行时需要 Python 3.10+、Pillow，以及访问 Asynx 服务的网络权限。依赖安装和密钥配置见[其他 Agent 安装](skills/asynx-image/references/setup.md#其他-agent)。

## 开始使用

安装后，在对话中描述需求即可：

- “生成一张机械键盘的产品图，横版。”
- “把刚才那张键盘外壳换成深绿色，其他保持不变。”
- “把这张产品图作为首帧，生成 5 秒的环绕展示视频，伴随轻柔音乐。”
- “生成 12 张不同配色的键盘图，先提交，稍后我再查进度。”
- “继续上次的任务，完成后把结果给我。”
- “把这张图片裁成正方形，再转成 WebP。”

也可以明确告诉 Agent“使用 Asynx 生成图片”。快捷调用方式取决于所用 Agent，例如 Codex 输入 `$asynx-image`，Claude Code 输入 `/asynx-image`。安装后未显示时，重新打开会话或刷新 Skill 列表。

单个任务默认等待完成并交付文件；想稍后查看，可以说“先提交”。批量任务可以查询进度、暂停或继续；离开对话后，已提交的任务继续生成，剩余项目可在回来后让 Agent 继续提交。

图片支持指定模型、比例和质量，修改本地图片时默认保持原图比例。本地参考图会适当压缩后上传，需保留原始编码时请明确说明。

视频可以指定时长、画幅、分辨率，并在描述中提出对白、音效或配乐要求。支持图片和首尾帧输入；视频、音频参考需提供 URL。详细参数见[视频操作](skills/asynx-image/references/video.md)。

等待中断或下载失败时，可以继续获取原结果，无需重新生成。取消已开始的任务仍可能产生费用。

## 密钥与配置

密钥保存在本机用户目录，多份安装共用配置：

| 系统 | 配置文件 |
| --- | --- |
| macOS / Linux | `~/.config/asynx/config.json` |
| Windows | `%APPDATA%\Asynx\config.json` |

更换密钥时，可以让 Agent 提供配置指引，再到自己的终端输入。macOS / Linux 的 Codex 示例：

```bash
python3 ~/.agents/skills/asynx-image/scripts/asynx.py configure
```

其他安装方式请换成实际安装目录中的 `scripts/asynx.py` 路径。Windows 命令、环境变量和连接排障见[配置与维护](skills/asynx-image/references/setup.md)。遇到问题也可以直接说“检查 Asynx 配置”，无需把密钥发给 Agent。

## 更新与卸载

可以直接告诉 Agent：“检查 Asynx Skill 更新”或“升级 Asynx Skill”。有新版时会提示，只有你要求升级才会安装。升级保留密钥、任务记录和已生成文件。通过其他工具安装的副本，可使用原工具更新。手动更新、回滚和旧版安装处理见[更新说明](skills/asynx-image/references/setup.md#更新与回滚)。

使用本仓库安装器安装的副本，卸载时在克隆的仓库根目录运行：

```bash
python3 install.py --uninstall
```

Windows 使用 `py install.py --uninstall`。其他方式安装的副本，通过对应 Agent 的 Skill 管理功能卸载，或移除其安装目录。卸载保留密钥、任务记录和已生成文件。

## 更多用法

- [操作参考](skills/asynx-image/references/operations.md)：批量任务、本地图片工具、输入限制和中断恢复。
- [视频操作](skills/asynx-image/references/video.md)：视频参数、参考素材和视频批次。
- [模型与提示词](skills/asynx-image/references/model-prompting.md)：复杂编辑、多参考图及图文布局的提示词建议。
- [配置与维护](skills/asynx-image/references/setup.md)：自托管地址、诊断、更新和迁移。

需要接入自己的工作流或工作台时，可以调用公开 API 或封装脚本命令。请通过公开接口管理任务，不直接操作 Skill 的数据库；密钥由可信后端或本地程序保管。
