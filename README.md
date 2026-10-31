# 镭闪自动化 1.0 Beta 7

面向《镭明闪击》的 Windows 桌面自动化工具，提供日常任务、模组制作、肉鸽循环与维护结束提醒，减少固定流程中的重复操作。本项目基于 [OK 框架（ok-script）](https://github.com/ok-oldking/ok-script)进行二次开发，起步使用 [ok-script-app](https://github.com/ok-oldking/ok-script-app) 模板，锁定框架版本 `ok-script==2.0.0b7`，使用截图、模板匹配、OCR 与前台输入执行任务。

当前版本为 **`v1.0.0-beta.7` 预发布版**。规范仓库为 [Auth3r/ok-leiming](https://github.com/Auth3r/ok-leiming)，此公开仓库从已脱敏最终源码快照开始，私人开发历史和旧发布保留在独立私有档案仓库。下载见 [Beta 7 Release](https://github.com/Auth3r/ok-leiming/releases/tag/v1.0.0-beta.7)；包内 `release-manifest.json` 的完整提交与哈希标识实际构建。详见 [Beta 7 说明](docs/releases/v1.0.0-beta.7.md) 和 [发布流程](docs/RELEASING.md)。

公开源码与预发布下载无需 GitHub 登录或 Token。本仓库不导入私人开发历史、个人配置或验收采证目录，范围见[公开检查记录](docs/PUBLICATION_CHECK.md)。原创部分采用 [MIT License](LICENSE)，OK 框架及其他第三方保留原始版权、许可证和对应源码，详见[第三方声明](docs/THIRD_PARTY_NOTICES.md)。

## 功能与验证边界

| 功能 | 已实现行为 | 当前边界 |
| --- | --- | --- |
| 日常 | 理财基金、黑盾领取及派遣、暗涌 04-3、干火 ST-7、猎金 ER-2、每日奖励；支持单项及保存的完整路线开关 | 首次七项开关开启，启动软件不会自动开始。历史多轮分项不能拼成当前构建的一次完整实机通过 |
| 黑盾派遣 | 普通条件沿用界面顺序；四人同战术固定筛选乌斯怀亚并依次确认界面前四名 | 不能用普通委托代替四人专项验收；选择或提交不明时不重复提交 |
| 模组制作 | 旧地联合、军工、一致、先攻；准备、循环制作、继续当前模组、记录浮窗及人工校正 | 配队由用户完成；旧地当前赛季不可正常制作。军工／一致有历史实机记录，先攻随机后半程及所有恢复组合未完整覆盖，不保证完美成品 |
| 模组目标查找 | 目标可见才选择并核对详情，否则向下滚动；不逐张点击无关卡片预览 | 对应原修复有源码实测；新构建游戏响应仍需独立验证 |
| 肉鸽 | 720P 原生 WGC 面板及既有 4K 路径、循环次数、当前页面接管与悬浮窗 | 既定路线终点是第二层悬赏后结算，不是五层全通；全部随机事件与长期无人值守未验收 |
| 维护结束提醒 | 按间隔检查维护状态，确认进入主界面后提醒并停止 | 不自动接着运行日常；真实开服完整成功路径需相应场景验证 |
| 控制与诊断 | F7 暂停／继续、F8 停止、本地问题报告、运行与构建身份记录 | 报告不自动上传；截图和日志可能含个人信息，发送前自行检查 |

任务启动和每次输入仍核对当前页面、窗口及资源；不自动付费、购买次数或补充体力。历史源码、旧 EXE、离线回放、模拟响应和当前构建的实机结果分别记录，不能互相替代。详见[使用说明](docs/使用说明.md)、[本版发布说明](docs/releases/v1.0.0-beta.7.md)。

## 环境

- Windows x64。主要已有游戏验证环境为 **1280×720 客户区、窗口化、150% 系统缩放**。代码保留自动识别及 3840×2160 路径，不代表所有 DPI、显示器、机器和账号均已验证。
- 需要手动启动并登录 `Warp Knights.exe`。程序不负责登录、输入密码或启动游戏，不支持 macOS / Linux；执行会使用游戏前台。
- 便携 ZIP 包含 Python、Qt、OCR、模型、模板及依赖，无需另装开发环境。源码开发需 Python **3.12 x64**，版本范围和锁定依赖见 `pyproject.toml`、`requirements.txt`。
- 依赖锁包含同版本 PySide6、Essentials、Addons 与 Shiboken6，满足 Fluent Widgets 的真实依赖声明；构建前执行 `pip check` 和锁定版本核对。
- 游戏更新、HDR／滤镜、画质、界面布局和窗口变化可能导致识别停止。客户区尺寸、截图像素和桌面分辨率不是同一概念；不要通过降低识别阈值绕过错误。

## 获取与启动

从 [Beta 7 Release](https://github.com/Auth3r/ok-leiming/releases/tag/v1.0.0-beta.7) 下载 Windows x64 完整 ZIP、同名 `.sha256` 和 `release-manifest.json`。公开下载无需 Token。Beta 6 与历史 RC 保留在私有档案，不导入本仓库。

1. 取得维护者明确指定的完整 `Leiming-Automation-v1.0.0-beta.7-win-x64.zip` 与同名 `.sha256`，核对 SHA256。
2. 完整解压到一个新的可写目录，保留旧目录；不要在压缩包预览里运行，不要只复制 EXE。
3. 双击 `Leiming-Automation.exe`，核对窗口版本和构建号与 `release-manifest.json` 一致。
4. 手动登录游戏，在“截图方式 → 游戏连接与问题报告”检查预览、窗口和客户区，再核对任务开关、扫荡次数、制作配置及数量，手动开始。
5. 暂停使用 F7，停止使用 F8。已发出的游戏输入不会撤销，结果不明的消耗动作不要反复开始重试。

校验示例：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath .\Leiming-Automation-v1.0.0-beta.7-win-x64.zip
Get-Content -LiteralPath .\Leiming-Automation-v1.0.0-beta.7-win-x64.zip.sha256
```

源码使用者应核对所取提交的 `src/version.py` 与构建记录，使用当前 Beta 7 代码。在现有项目根目录执行：

```powershell
.\Setup.cmd
.\Start.cmd
```

`Setup.cmd` 在本工作树创建 `.venv`，按现有锁定文件安装，不复制其他工作树环境。首次配置由启动流程从默认来源创建；中文目录通过当前 Unicode 文件适配直接访问，不需要旧版 SUBST 盘符映射。台式机新增项目与验收产物使用 `F:\Codex`；已有项目不因文档整理迁移。

## 使用与配置

- “任务”提供日常路线和单项；“设置 → 日常任务”保存七项开关，“截图方式 → 暗涌扫荡设置”保存次数，0 表示跳过。每日已完成状态需以当前账号画面为准，完整日常不以昵称 OCR 成功作为启动条件。
- “模组制作”选择配置、数量（1～100）、刷新方式和人数门槛；记录浮窗可显示／隐藏、调整位置及透明度。“继续当前模组”处理当前已识别页面，未知历史保持未知，不重放结果不明的确认。
- “设置 → Roguelike”保存循环次数（1～100）和悬浮窗开关；720P 原生面板和既有 4K 入口共用设置，下一次开始生效。
- “抢开服：维护结束提醒”是独立任务，循环间隔为 5～3600 秒，默认 30 秒，不属于完整日常路线。

目录可写时，个人数据保存在运行目录；只读目录会使用当前 Windows 用户的 `Leiming-Automation/portable-安装位置摘要` 数据目录。资源仍从安装目录加载，重启沿用已选数据位置。同一安装重复打开不会终止已有实例的任务。

| 文件或目录 | 用途 |
| --- | --- |
| `settings.json` | 游戏进程、可选标题、分辨率与临时接管设置 |
| `configs/Tasks.json` | 日常开关、暗涌次数 |
| `configs/ModuleCrafting.json` / `configs/Roguelike.json` | 制作和肉鸽共享设置 |
| `configs/Advanced.json` / `configs/Developer.json` | 识别、超时、重试、调试及单步设置 |
| `config_version.json` / `config-backups/` | 配置结构版本与迁移备份，需随个人配置保留 |
| `logs/` / `reports/` | 运行回执、恢复证据及本地问题报告，可能含个人数据 |

完整字段、材料消耗及恢复边界以[使用说明](docs/使用说明.md)为准。不要复制开发者配置，不要清空账本、删除版本标记或降低阈值来消除错误。

## 更新机制

Beta 7 的“关于 → 检查更新”直接读取本项目 GitHub Releases，Beta 默认包含测试版本，排除草稿；只安装高于当前版本的 Windows x64 包。下载先校验 ZIP SHA-256、构建身份和全量运行文件，再由独立安装器等待原程序退出、保留个人数据、替换及重启；启动失败恢复原版本，完整旧目录保留为旁侧备份。

公开更新源为 `Auth3r/ok-leiming`，普通用户不需要 Token；程序默认匿名请求，不读取开发者 Git 登录或保存凭据。检查失败不阻止正常启动。只读安装目录不能直接自动替换，请继续使用手动独立解压与配置迁移。

**Beta 6 不包含本次更新器，首次升级必须手动下载并完整解压 Beta 7。** 没有真实更高已发布版本时，不把本地或模拟测试宣称为线上升级通过。手动更新仍按[配置迁移步骤](docs/使用说明.md#手动更新与保留配置)执行。调用链、公开更新源及选择理由见[上游与更新说明](docs/UPSTREAM.md)。

## 常见问题与已知限制

- **旧版提示找不到 Launcher：** 这是 Beta 6 的已确认断点，先手动安装包含新更新器的包。Beta 7 的 HTTP 401/403/404 表示访问失败，应检查网络、请求限额或发布状态，不是“已是最新”。
- **连接或输入失败：** 核对游戏进程、客户区、预览和权限，保持窗口未最小化；连接成功不等于所有页面已识别。
- **中文路径、只读目录或缺版本标记：** 当前已有专门处理；完整有效配置可恢复缺失标记，缺失／冲突配置仍会停止并保留文件。不要复制其他人的配置解决。
- **换号后任务结果异常：** 以当前画面核对，不沿用上一账号的完成状态；昵称读空不代表没有账号或已经完成。
- **识别、奖励或消耗结果不明：** 停止相关动作，保留原始日志和截图；不要为了取得成功回执重复扫荡、刷新或派遣。
- **反馈问题：** 在“截图方式”生成本地问题报告，检查其中的昵称、窗口标题、截图和日志后再发送。附版本、提交／构建号、分辨率、DPI、复现步骤和必要现场。

当前不承诺全部随机后半程、跨版本同件接续、多机器／多账号／全部 DPI 或长期无人值守稳定；历史原始证据缺失的测试会明确跳过，不算通过。源码与 EXE、不同提交和不同包的结果分别验收。

## 验证与贡献

请阅读 [AGENTS.md](AGENTS.md)，保持改动范围集中，不改变既定业务策略。本公开快照附带更新器、安装回滚、发布身份与配置迁移的专项 pytest 回归；私人开发采证和游戏回放素材不导入。开发工具不属于运行时锁定依赖，构建版本见 `tools/requirements-build.txt`。

```powershell
$evidence = 'F:\Codex\leiming-automation-evidence\beta6-local-check'
New-Item -ItemType Directory -Path "$evidence\temp" -Force | Out-Null
$env:TEMP = "$evidence\temp"
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
.\.venv\Scripts\python.exe -X utf8 -m pytest tests -q -ra -o "cache_dir=$evidence\pytest" "--basetemp=$evidence\temp\pytest"
git diff --check
```

构建使用 `tools/build_release.py`，验证实际 ZIP 和对应源码使用 `tools/verify_release.py`；构建成功不等于游戏实机通过。不要把账号、配置、缓存或私人采证提交到仓库。反馈与贡献通过 [Issues](https://github.com/Auth3r/ok-leiming/issues) 和 PR，附实际命令、结果、提交身份和必要截图。

## 许可证与免责声明

`Copyright (c) 2026 Auth3r`。[MIT License](LICENSE) 适用于项目所有者有权授权的原创代码和原创文档，不能覆盖第三方框架、上游保留内容、游戏图像或其他权利人的材料。便携组合交付仍需履行 AGPL / GPL / LGPL 等对应义务，保留 `licenses`、版权通知和必要对应源码；详见[第三方声明](docs/THIRD_PARTY_NOTICES.md)。

本工具与游戏官方无隶属或认可关系，游戏名称、商标及图像属于相应权利人。不保证账号安全、游戏更新后的兼容性或模组产出结果。使用前了解游戏规则并检查任务配置；MIT 与第三方许可中的免责及其他条款分别适用。
