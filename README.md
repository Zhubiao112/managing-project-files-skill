# Managing Project Files Skill

一个面向 Codex 的项目文件治理 skill，用于解决报告、图表、日志和中间文件混杂，成果入口难找，以及存储空间被可重建文件持续占用等问题。

它的目标不是把项目强行重排成统一目录，也不是按文件名或时间批量删除文件，而是在保留原始数据、技术路径和科研溯源关系的前提下，为项目建立清晰、稳定、适合人工复盘的成果入口。

## 核心功能

- 建立项目内统一的 `deliverables/` 可读成果中心。
- 从已有报告和图表中筛选当前有效、真正需要复盘的内容，避免简单镜像整个结果目录。
- 使用 `MANIFEST.csv` 记录成果状态、展示路径与原始技术路径。
- 明确区分 `current`、`limited`、`pending`、`superseded` 和 `archive`。
- 将清理对象分为安全缓存、可重建中间文件和高风险或受保护文件。
- 在删除前检查 Git 状态、路径引用、生成流程、输入数据、活动任务和恢复能力。
- 检查成果清单、Markdown 链接、路径越界以及日志、轨迹、检查点等运行文件是否误入成果目录。
- 输出准确的删除清单、回收空间、受保护路径和暂缓处理项目。

## 适用场景

- 报告和图表散落在多个目录，每次复盘都要重新寻找。
- Codex 或分析流程生成的日志、缓存和临时文件与正式成果混在一起。
- 项目存在多个版本的结果，但当前版本和过时版本没有明确标记。
- 存储空间紧张，需要安全清理可重建内容。
- 科研项目需要保护原始实验数据、唯一结果和完整溯源记录。

## 安全边界

本 skill 不会把以下信号单独当作删除依据：

- 文件名包含 `old`、`tmp`、`backup` 或 `cache`；
- 文件较旧；
- 文件没有被 Git 跟踪或当前没有进程打开；
- 存在名字相似或看起来重复的文件；
- 文件可以压缩或移动到隔离目录。

原始实验数据、活动上传目录、未提交的用户修改、唯一分析结果、未解决的交接文件和溯源证据默认受保护。未经精确授权，skill 只生成候选清单，不执行删除。

## 推荐成果目录

```text
deliverables/
├── README.md
├── MANIFEST.csv
├── reports/
├── figures/
├── tables/
└── archive/
```

`deliverables/` 是人工查看入口，不是新的技术结果根目录。原始报告、脚本和结果仍保留在原有规范路径中。

## 安装

```bash
git clone https://github.com/Zhubiao112/managing-project-files-skill.git \
  ~/.codex/skills/managing-project-files
```

安装后重新打开 Codex 任务，或在新的项目任务中调用该 skill。

## 使用方式

在 Codex 中直接输入：

```text
使用 $managing-project-files 整理当前项目，建立可读成果入口，并列出安全清理候选项。
```

也可以进一步限定权限：

```text
使用 $managing-project-files 审计当前项目。不要移动原始数据，不要直接删除高风险文件，先给我清理候选表。
```

## 验证成果目录

skill 自带轻量验证器，只依赖 Python 标准库：

```bash
python3 ~/.codex/skills/managing-project-files/scripts/validate_deliverables.py \
  --project-root /path/to/project
```

验证器会检查目录结构、清单字段、状态值、成果和原始来源路径、Markdown 本地链接、路径越界及不应出现在成果目录中的运行文件。

## 仓库结构

```text
.
├── SKILL.md
├── agents/openai.yaml
├── references/
│   ├── cleanup-safety.md
│   └── contracts.md
├── scripts/validate_deliverables.py
├── README.md
└── LICENSE
```

## 共同完善

项目文件管理高度依赖具体领域和工作流。欢迎通过 Issue 提交实际项目中遇到的混乱模式、安全边界或验证需求，也欢迎通过 Pull Request 改进清理规则、成果清单契约和验证器。

提交修改时，请优先保证：不误删原始数据，不破坏现有技术路径，不把未经验证的“可重建”当作事实。

## 开源协议

本项目采用 [MIT License](LICENSE)。
