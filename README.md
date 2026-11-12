# Asynx Agent Skills

在支持 Agent Skills 且可执行 Python 脚本的 Agent 中，通过自然语言生成或编辑图片、制作 SVG 矢量图与 GIF 动画，并可由文字或图片生成 3D 模型，为其绑定骨骼、套用动画与转换格式。支持参考图、批量生成、进度查询与中断恢复，亦可在本机完成缩放、裁剪、切图与格式转换。

## 安装

运行环境要求 Python 3.10+。生成图片、视频或 3D 模型须配置 [LLMAPI API Key](https://llmapi.site/api-keys)；仅使用本地图片处理功能时无需配置密钥。

### Codex / Claude Code

可使用仓库自带的安装器：

```bash
git clone https://github.com/dawnx/asynx-skills.git
cd asynx-skills
python3 install.py
```

Windows 环境下将最后一行改为 `py install.py`。

安装器会检测 Codex 与 Claude Code，并引导用户输入 API Key。密钥应在终端中输入，不得发送至对话。

如需仅安装至指定 Agent，可使用 `python3 install.py --target codex`、`--target claude` 或 `--target both`。

默认安装位置：

| Agent | 用户主目录下的路径 |
| --- | --- |
| Codex | `.agents/skills/asynx-image` |
| Claude Code | `.claude/skills/asynx-image` |

### 其他 Agent

通过该 Agent 的 Skill 导入或安装功能选择本仓库的 `skills/asynx-image` 目录，脚本与参考文档均需保留；亦可将整个目录放入该 Agent 的 Skill 目录，具体位置以其说明为准。

运行时需要 Python 3.10+、Pillow，以及访问 Asynx 服务的网络权限。依赖安装与密钥配置见[其他 Agent 安装](skills/asynx-image/references/setup.md#其他-agent)。

## 开始使用

安装完成后，在对话中直接描述需求：

- “生成一张机械键盘的产品图，横版。”
- “把刚才那张键盘外壳换成深绿色，其他保持不变。”
- “生成一枚 SVG 格式的咖啡杯图标。”
- “生成一个小猫挥手的 GIF 动画。”
- “把这张产品图作为首帧，生成 5 秒的环绕展示视频，伴随轻柔音乐。”
- “生成一把木质扶手椅的 3D 模型。”
- “用刚才那张产品图生成 3D 模型，面数控制在 5 万以内。”
- “给刚才的角色模型绑定骨骼，加一个走路动画，再导出 FBX。”
- “生成 12 张不同配色的键盘图，先提交，稍后我再查进度。”
- “继续上次的任务，完成后把结果给我。”
- “把这张图片裁成正方形，再转成 WebP。”

也可直接要求 Agent“使用 Asynx 生成图片”。快捷调用方式因 Agent 而异，例如 Codex 输入 `$asynx-image`，Claude Code 输入 `/asynx-image`。安装后若未出现，请重新打开会话或刷新 Skill 列表。

单个任务默认等待完成后交付文件；需要稍后查看时，可要求“先提交”。批量任务支持查询进度、暂停与继续；离开对话后，已提交的任务继续生成，剩余项目可在返回后由 Agent 继续提交。

每张图的描述或尺寸不同时，可以一次给出清单（例如“按这 20 条文案各生成一张”），由 Agent 建成一个批次。想先了解费用时，可要求“先报价”：只查询价格，不提交、不扣费。账户余额、密钥或模型出现问题时，批次会自动暂停并说明原因，问题解决后可继续，已受理的任务不会重复提交。

图片支持指定模型、画幅比例与质量；修改本地图片时默认保持原图比例。本地参考图会先经适当压缩再上传，如需保留原始编码请明确说明。

可直接描述用途、画幅或像素尺寸；未指定画幅时由 Agent 按用途选择（用途不明确时为 3:2），分辨率档位使用服务默认设置。指定尺寸不被支持时会给出提示，不会擅自修改后重新生成；实际尺寸以结果文件为准。

也可生成 SVG 矢量图和 GIF 动画，交付原始文件。普通图片默认使用 `gpt-image-2.5-sunburst`，SVG/GIF 默认使用 `gpt-image-2.6-sunburst`，SVG 编写模型默认为 `gpt-6.1-sol`；明确指定模型时保留用户选择。更多用法见[高级图片参数](skills/asynx-image/references/operations.md#高级图片参数与结果格式)。

视频支持指定时长、画幅与分辨率，对白、音效或配乐要求可直接写入描述。支持图片与首尾帧输入；视频、音频参考须提供 URL。参数详情见[视频操作](skills/asynx-image/references/video.md)。

3D 模型可由文字描述或单张图片生成，交付 GLB 文件及预览图，可指定面数上限以及是否生成贴图与 PBR 材质。已生成的模型可检查能否绑定骨骼，再绑定骨骼、套用动画，或转换为 glTF、FBX、USDZ、OBJ、STL、3MF；骨骼绑定、每个动画与每次转换均为单独计费的任务。同一操作有多个可用 3D 模型时须指定模型。参数详情见[3D 模型操作](skills/asynx-image/references/model3d.md)。

等待中断或下载失败时，可继续获取原结果，无需重新生成。提交被服务拒绝时会明确说明任务未创建、未扣费，以及需要调整参数、处理账户还是稍后再试。取消已开始的任务仍可能产生费用。

结果默认保存在当前目录的 `generated-images` 下，按日期与描述命名子目录；可在[配置与维护](skills/asynx-image/references/setup.md#本机数据与保存目录)中设置固定的保存目录，或清理本机任务记录（不删除已生成的文件）。

## 密钥与配置

密钥保存在本机用户目录，多处安装共用同一份配置：

| 系统 | 配置文件 |
| --- | --- |
| macOS / Linux | `~/.config/asynx/config.json` |
| Windows | `%APPDATA%\Asynx\config.json` |

更换密钥时，可先由 Agent 提供配置指引，再到用户自己的终端输入。以下为 macOS / Linux 上 Codex 的示例：

```bash
python3 ~/.agents/skills/asynx-image/scripts/asynx.py configure
```

其他安装方式请将路径替换为实际安装目录中的 `scripts/asynx.py`。Windows 命令、环境变量与连接排查见[配置与维护](skills/asynx-image/references/setup.md)。如遇问题，也可直接要求“检查 Asynx 配置”，无需将密钥发送给 Agent。

## 更新与卸载

可直接要求 Agent“检查 Asynx Skill 更新”或“升级 Asynx Skill”。有新版本时会给出提示，仅在用户要求升级时才会安装。升级保留密钥、任务记录与已生成文件；通过其他工具安装的副本应使用原工具更新。手动更新、回滚及旧版安装处理见[更新说明](skills/asynx-image/references/setup.md#更新与回滚)。

使用本仓库安装器安装的副本，卸载时在克隆的仓库根目录运行：

```bash
python3 install.py --uninstall
```

Windows 使用 `py install.py --uninstall`。其他方式安装的副本，请通过对应 Agent 的 Skill 管理功能卸载，或移除其安装目录。卸载保留密钥、任务记录与已生成文件。

## 更多用法

- [操作参考](skills/asynx-image/references/operations.md)：批量任务、本地图片工具、输入限制与中断恢复。
- [视频操作](skills/asynx-image/references/video.md)：视频参数、参考素材与视频批次。
- [3D 模型操作](skills/asynx-image/references/model3d.md)：文字或图片生成 GLB 模型，绑定检查、骨骼绑定、动画与格式转换，参数、交付与恢复。
- [模型与提示词](skills/asynx-image/references/model-prompting.md)：复杂编辑、多参考图及图文排版的提示词建议。
- [配置与维护](skills/asynx-image/references/setup.md)：自托管地址、诊断、更新与迁移。

需要接入自有工作流或工作台时，可调用公开 API 或封装脚本命令。任务应通过公开接口管理，请勿直接操作 Skill 的数据库；密钥交由可信后端或本地程序保管。
