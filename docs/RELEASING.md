# 版本、候选包与发布

应用版本只在 `src/version.py::APP_VERSION` 修改，例如 `v1.0.0-beta.7`。`pyproject.toml` 通过 setuptools 动态读取同一值，Python 元数据规范化为 `1.0.0b7`；它们表达同一版本。普通源码提交不增加应用版本。需要交付不同的二进制时，先核对远端标签和 Release，再选择未使用的版本，提交对应的 `docs/releases/<版本>.md`。

`main` 表示已合并源码；修复分支表示正在验证的修改；候选是一个固定完整 Git SHA 和一组固定字节的资产。发布标签等于应用版本，指向候选的构建提交。候选验收后合并时，若合并得到新提交或需要改代码，应重新构建并核实身份，不把旧包描述为新提交产物。已经发布的标签和资产不移动、不覆盖；后续修复使用新的版本。

规范仓库：[Auth3r/ok-leiming](https://github.com/Auth3r/ok-leiming)。公开快照和新构建的更新 API、manifest 均使用此规范名称。原私人历史位于独立私有档案；不要将其 refs 或对象推入公开仓库。

## 准备候选

1. 在 Windows x64 / Python 3.12 上，核对目录、Git 状态、远端 `main`、现有标签和 Release。台式机的工作树、构建中间目录、输出和大型缓存放在 F 盘。保留已有工作区及个人配置，在最新 `main` 上建立独立修复分支。
2. 更新应用版本和该版本说明，完成源码测试与审查。依赖按 `requirements.txt` 和 `tools/requirements-build.txt` 安装到 F 盘虚拟环境；执行 `python -m pip check`。锁文件必须保留 Fluent Widgets 要求的 PySide6 元包及同版 Addons、Essentials、Shiboken，不能靠删除依赖声明制造“检查通过”。
3. 提交全部候选源码，记录完整 SHA 和干净状态。输出路径必须为空且独立于日常安装目录。例如：

```powershell
python -m pytest
python -m compileall -q src tools main.py
python -m pip check
git diff --check
python tools/build_release.py --output-dir F:\Codex\candidate-beta7 --work-dir F:\Codex\candidate-beta7-build
python tools/verify_release.py F:\Codex\candidate-beta7\Leiming-Automation-v1.0.0-beta.7-win-x64.zip
```

构建器检查锁定依赖、单一版本来源、已存在的本地/远端同名标签，并拒绝覆盖已有输出。`--allow-dirty` 始终生成 `-TRIAL.zip`，即使源码干净也标记为试验产物，不能用于发布。源码副本重建同一已发布版本时使用该模式，不能将重建结果替换原资产。

## 核对交付身份

| 信息 | 来源及用途 |
| --- | --- |
| 应用版本 | `src/version.py`；GUI、日志、更新比较使用完整 Beta/RC/正式版本 |
| Git 提交 | 构建时固定的完整 SHA；包内 manifest 和对应源码档案均记录 |
| 发布标签 | 与应用版本相同，并指向该构建 SHA；预发布在 GitHub 标记 `prerelease=true` |
| EXE 元数据 | 构建时从应用版本产生；字符串保留完整版本，Windows 数字字段区分 alpha、beta、RC、正式版 |
| ZIP 名称 | `Leiming-Automation-<版本>-win-x64.zip` |
| ZIP 摘要 | 同名 `.zip.sha256`，内容为 SHA-256、两个空格、精确 ZIP 文件名 |
| 包内清单 | `Leiming-Automation/release-manifest.json`；版本、提交、依赖及除清单自身外全部文件的 SHA-256 |
| 发布清单 | 输出目录的 `release-manifest.json`；包含包内字段并附 ZIP 名、字节数、SHA-256 |
| 远端版本 | 本项目实际已发布的 Release 标签；草稿不用于更新，Beta 不依赖 `/releases/latest` |

`tools/verify_release.py` 同时核对 ZIP 名与摘要、两个 manifest、实际 EXE 版本、全部文件哈希、对应源码版本及 Git 原始文件字节。对应源码 ZIP 中的 manifest 标记 `source-provenance`，只证明源码身份；最终外层清单覆盖该源码 ZIP，避免摘要循环。标签名称或窗口标题本身不能证明 EXE 来自哪个提交。

## 验收、PR 与 Release

本次首个公开 Beta 7 按用户授权从脱敏快照重新构建，执行专项回归、包身份和匿名下载核验，并用新 EXE 实际查询公开版本；不重复全仓库测试、不操作游戏。

把候选完整解压到独立测试目录，实际打开 EXE，在关于页点击检查更新并保存原生截图、日志、运行路径、EXE 哈希和启动时的提交记录。分别记录模拟异常分支、真实网络访问、真实候选下载/校验/安装/重启，以及跨机器游戏验收。没有可访问的更高已发布版本时，完整真实升级仍是 `NOT PROVEN`，不能用假 Release 或本地测试服务器替代。

通过相关检查后推送修复分支并创建指向 `main` 的 PR。确认批准的候选提交后，创建该提交的唯一标签，创建 Release 草稿并上传已经验收的同一个 ZIP、`.zip.sha256` 和外置 `release-manifest.json`；不要在上传前重新构建而保留旧测试结论。核对 GitHub 返回的目标提交、资产名、大小、下载权限和哈希，再决定是否发布预发布或正式版。

公开仓库与 Release 应支持匿名访问，普通用户无需 Token。不要将开发者 Token 写入源码、包或默认配置。当前同版查询只能证明无升级/无降级；跨机器游戏验收和真实线上高版本升级单独记录。

## 验收证据

每个候选保存命令、退出码、测试通过/失败/跳过数量、源码 SHA、干净状态、构建日志、两个清单、ZIP/EXE 哈希、原生截图及重启回执。新提交、新 EXE 和旧包的结果分别记录。本次发布证据独立保存在维护者本地，不随公开源码导入。既有全仓库测试记录只作为历史证据，本次执行直接相关回归、新包身份与匿名访问核验。

动态 Python 版本字段依据 [setuptools 官方动态元数据说明](https://setuptools.pypa.io/en/latest/userguide/pyproject_config.html#dynamic-metadata)。
