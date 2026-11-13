# Asynx Agent Skills

在支持 Agent Skills 且可执行 Python 脚本的 Agent 中，通过自然语言生成或编辑图片、制作 SVG 矢量图与 GIF 动画，并可由文字或图片生成 3D 模型，为其绑定骨骼、套用动画与转换格式。支持参考图、批量生成、进度查询与中断恢复，亦可在本机完成缩放、裁剪、切图与格式转换。

## 安装

运行环境要求 Python 3.10+。生成图片、视频或 3D 模型须配置 [LLMAPI API Key](https://llmapi.site/api-keys)；仅使用本地图片处理功能时无需配置密钥。

### Claude Code

本仓库是一个插件市场，提供 `asynx` 插件。在 Claude Code 会话中依次输入：

```text
/plugin marketplace add dawnx/asynx-skills
/plugin install asynx@asynx-skills
```

安装后 Skill 以 `/asynx:asynx-image` 调用。

### Codex 与其他 Agent

使用开源的 [`skills` 命令行工具](https://github.com/vercel-labs/skills)，需要 Node.js 22.20+：

```bash
npx skills add dawnx/asynx-skills -g
```

命令会列出本机检测到的 Agent 供选择，也可用 `-a codex` 直接指定。`-g` 安装到用户目录，所有项目可用（Codex 为 `~/.agents/skills/asynx-image`）；省略 `-g` 时安装到当前项目（Codex 为项目内的 `.agents/skills/asynx-image`）。各 Agent 的目录见该工具的 [Agent 列表](https://github.com/vercel-labs/skills/blob/main/docs/agents.md)。

### 手动安装

不使用以上工具时，将本仓库的 `skills/asynx-image` 整个目录复制到 Agent 的 Skill 目录（例如 `~/.agents/skills/asynx-image`），具体位置以该 Agent 的说明为准。更新时用新版目录替换，卸载时移除该目录。

### 完成设置

安装后回到 Agent 对话，要求“设置 Asynx”。Agent 会检查并安装图片处理依赖 Pillow（装在用户目录中，不随 Skill 更新丢失），然后给出一条配置密钥的命令。在自己的终端运行该命令并输入 API Key，密钥不经过对话。详见[配置与维护](skills/asynx-image/references/setup.md)。

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

也可直接要求 Agent“使用 Asynx 生成图片”。快捷调用方式因 Agent 与安装方式而异：Codex 输入 `$asynx-image`；Claude Code 通过插件安装时输入 `/asynx:asynx-image`，通过 `skills` 工具或手动安装时输入 `/asynx-image`。安装后若未出现，请重新打开会话或刷新 Skill 列表。

单个任务默认等待完成后交付文件；需要稍后查看时，可要求“先提交”。批量任务支持查询进度、暂停与继续；离开对话后，已提交的任务继续生成，剩余项目可在返回后由 Agent 继续提交。

每张图的描述或尺寸不同时，可以一次给出清单（例如“按这 20 条文案各生成一张”），由 Agent 建成一个批次。想先了解费用时，可要求“先报价”：只查询价格，不提交、不扣费。账户余额、密钥或模型出现问题时，批次会自动暂停并说明原因，问题解决后可继续，已受理的任务不会重复提交。

图片支持指定模型、画幅比例与质量；修改本地图片时默认保持原图比例。本地参考图会先经适当压缩，再单独上传一次并由任务引用，同一批次共用的参考图不会重复上传；如需保留原始编码请明确说明。

可直接描述用途、画幅或像素尺寸；未指定画幅时由 Agent 按用途选择（用途不明确时为 3:2），分辨率档位使用服务默认设置。指定尺寸不被支持时会给出提示，不会擅自修改后重新生成；实际尺寸以结果文件为准。

也可生成 SVG 矢量图和 GIF 动画，交付原始文件。未指定模型时由服务选用默认模型（普通图片、SVG/GIF 和视频各有默认值，可在管理后台调整）；明确指定模型时保留用户选择。更多用法见[高级图片参数](skills/asynx-image/references/operations.md#高级图片参数与结果格式)。

视频支持指定时长、画幅与分辨率，对白、音效或配乐要求可直接写入描述。支持图片与首尾帧输入；视频、音频参考须提供 URL。参数详情见[视频操作](skills/asynx-image/references/video.md)。

3D 模型可由文字描述或单张图片生成，交付 GLB 文件及预览图，可指定面数上限以及是否生成贴图与 PBR 材质。已生成的模型可检查能否绑定骨骼，再绑定骨骼、套用动画，或转换为 glTF、FBX、USDZ、OBJ、STL、3MF；骨骼绑定、每个动画与每次转换均为单独计费的任务。同一操作有多个可用 3D 模型时须指定模型。参数详情见[3D 模型操作](skills/asynx-image/references/model3d.md)。

等待中断或下载失败时，可继续获取原结果，无需重新生成。提交被服务拒绝时会明确说明任务未创建、未扣费，以及需要调整参数、处理账户还是稍后再试。提交过程中断、不确定是否成功时，Agent 会先向服务查询原任务，不会重复提交。取消已开始的任务仍可能产生费用。

结果默认保存在当前目录的 `generated-images` 下，按日期与描述命名子目录；可在[配置与维护](skills/asynx-image/references/setup.md#本机数据与保存目录)中设置固定的保存目录，或清理本机任务记录与参考图暂存文件（不删除已生成的文件）。

## 密钥与配置

密钥保存在本机用户目录，多处安装共用同一份配置：

| 系统 | 配置文件 |
| --- | --- |
| macOS / Linux | `~/.config/asynx/config.json` |
| Windows | `%APPDATA%\Asynx\config.json` |

配置或更换密钥时，要求 Agent“设置 Asynx”，它会给出适用于本机安装位置的配置命令，形如：

```bash
python3 ~/.agents/skills/asynx-image/scripts/asynx.py configure
```

在自己的终端运行并输入密钥。Windows 命令、环境变量与连接排查见[配置与维护](skills/asynx-image/references/setup.md)。如遇问题，也可直接要求“检查 Asynx 配置”，无需将密钥发送给 Agent。

## 更新与卸载

更新与卸载通过安装时使用的渠道完成，两者都保留密钥、任务记录、图片处理依赖与已生成文件。

| 安装方式 | 更新 | 卸载 |
| --- | --- | --- |
| Claude Code 插件 | 会话中输入 `/plugin marketplace update asynx-skills` | 终端运行 `claude plugin uninstall asynx@asynx-skills` |
| `skills` 工具 | `npx skills update asynx-image -g` | `npx skills remove asynx-image -g` |
| 手动安装 | 用新版 `skills/asynx-image` 替换原目录 | 移除该目录 |

安装到项目内的副本，去掉 `skills` 命令中的 `-g`。Claude Code 中可在 `/plugin` 的 Marketplaces 页为 `asynx-skills` 开启自动更新；更新后重新打开会话生效。

## 更多用法

- [操作参考](skills/asynx-image/references/operations.md)：批量任务、本地图片工具、输入限制与中断恢复。
- [视频操作](skills/asynx-image/references/video.md)：视频参数、参考素材与视频批次。
- [3D 模型操作](skills/asynx-image/references/model3d.md)：文字或图片生成 GLB 模型，绑定检查、骨骼绑定、动画与格式转换，参数、交付与恢复。
- [模型与提示词](skills/asynx-image/references/model-prompting.md)：复杂编辑、多参考图及图文排版的提示词建议。
- [配置与维护](skills/asynx-image/references/setup.md)：依赖与密钥设置、自托管地址、诊断与更新。

需要接入自有工作流或工作台时，可调用公开 API 或封装脚本命令。任务应通过公开接口管理，请勿直接操作 Skill 的数据库；密钥交由可信后端或本地程序保管。
