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
- 支持任务结束或定期运行的半自动维护：按明确规则收录新成果，并保持幂等。
- 通过计划重派生、SHA-256、无覆盖原子发布、原子交换 CAS、可恢复 `pending`/治理事务和固定目录锁避免并发覆盖或半更新。

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

原始实验数据、活动上传目录、未提交的用户修改、唯一分析结果、未解决的交接文件和溯源证据默认受保护。半自动模式只会删除未超过 `max_cache_delete_bytes` 且通过路径、Git、保护规则、指纹与 SHA-256 复查的 `.DS_Store` 与 `*.pyc`，并清理由此变空的 `__pycache__`。超限缓存和其他文件未经精确授权只进入候选清单。

真正删除前，候选文件会先在固定父目录中原子隔离，再次检查 inode、当前策略、Git tracked/dirty 状态和保护规则。若文件被替换、刚被纳入 Git 或规则发生变化，管理器会恢复文件并安全停止，不会追随被替换的父目录。

成果发布采用“只创建、不覆盖”：如果目标文件在发布瞬间已存在，任务会安全停止；如果已管理成果的内容发生变化，则使用确定性的内容版本路径更新清单，不覆盖旧文件。中断后可根据项目内 `pending` 状态恢复，但不会据此扩大删除权限。

`MANIFEST.csv` 与 `README.md` 使用原子交换式 compare-and-swap：只有被换出的文件仍与预检 inode 和摘要一致时才提交；并发编辑或父目录移动会触发回滚。切换 `copy`/`wrapper` 时会重新计算兼容的扩展名和目标目录。

## 推荐成果目录

```text
deliverables/
├── README.md
├── MANIFEST.csv
├── MAINTENANCE_STATUS.md
├── CLEANUP_CANDIDATES.csv
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

## 持续管理（方案 B）

方案 B 是按项目启用的半自动模式：自动维护符合明确规则的最终成果，只自动清理严格白名单缓存，日志和其他中间文件仍等待人工确认。

先初始化项目：

```bash
python3 ~/.codex/skills/managing-project-files/scripts/manage_project_files.py \
  init --project-root /path/to/project --mode semi-auto
```

然后编辑项目中的 `.codex/project-files-policy.json`，填写真实的成果来源、保护路径和扫描根目录。默认不会猜测哪些文件是最终报告。

默认 `cleanup_roots` 为空，因此在项目明确配置窄范围扫描根目录之前，不会遍历清理目录。不要为大型项目或 HPC 结果树直接填入 `.`。

一个最小成果规则示例：

```json
{
  "name": "final-reports",
  "category": "report",
  "include": ["reports/final/**/*.md"],
  "exclude": ["reports/final/drafts/**"],
  "destination": "reports",
  "promotion": "wrapper",
  "status": "current"
}
```

日常命令：

```bash
# 只生成计划、状态和清理候选，不收录、不删除
python3 ~/.codex/skills/managing-project-files/scripts/manage_project_files.py \
  scan --project-root /path/to/project

# 半自动项目：扫描后收录成果，并清理严格白名单缓存
python3 ~/.codex/skills/managing-project-files/scripts/manage_project_files.py \
  maintain --project-root /path/to/project

# 查看上一次扫描和执行状态
python3 ~/.codex/skills/managing-project-files/scripts/manage_project_files.py \
  status --project-root /path/to/project
```

推荐在“产生或修改正式报告、最终图表、总结表”的 Codex 任务结束时运行一次 `maintain`，再按需建立项目级定期审计作为兜底。不默认安装 `launchd`、cron、watchdog 或 HPC 登录节点守护进程。

## 验证成果目录

skill 的持续管理器和轻量验证器都只依赖 Python 标准库：

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
│   ├── continuous-management.md
│   └── contracts.md
├── scripts/
│   ├── manage_project_files.py
│   └── validate_deliverables.py
├── tests/
├── README.md
└── LICENSE
```

## 共同完善

项目文件管理高度依赖具体领域和工作流。欢迎通过 Issue 提交实际项目中遇到的混乱模式、安全边界或验证需求，也欢迎通过 Pull Request 改进清理规则、成果清单契约和验证器。

提交修改时，请优先保证：不误删原始数据，不破坏现有技术路径，不把未经验证的“可重建”当作事实。

## 开源协议

本项目采用 [MIT License](LICENSE)。
