# 上游、许可与更新接口

## 实际来源

- 起步模板：[ok-oldking/ok-script-app](https://github.com/ok-oldking/ok-script-app)，最初检查与复用的提交为 `f134d07ce064c49060068c0511b5592b0e78a081`。
- 核心框架：[ok-oldking/ok-script](https://github.com/ok-oldking/ok-script)，本项目 `requirements.txt` 锁定 `ok-script==2.0.0b7`，同时锁定 `pyappify==1.0.10`。
- 本项目复用 Qt GUI、任务注册、配置、截图、模板匹配、OCR、输入和日志接口。业务逻辑使用 `BaseTask.next_frame/find_one/find_feature/ocr/click/sleep/pause/log_info/log_error/info_set`。
- 原始模板说明保存在 `UPSTREAM_README.md`，用于来源核对，不作为本版启动或更新指引。本项目自己的仓库为 `Auth3r/ok-leiming`，不是上游模板仓库。

## 许可及公开检查

实际安装的 `ok-script==2.0.0b7` wheel 附带 AGPLv3，原文保存在 `OK_SCRIPT_LICENSE.txt`。保留该锁定发行物的许可，不用上游当前 master 的新许可替换它。其他组件按各自许可保留版权、声明和对应源码；便携包中的 `licenses` 及 `THIRD-PARTY-NOTICES.md` 提供具体材料。

镭闪自动化基于 OK 框架二次开发。项目所有者有权授权的原创部分采用根目录 [MIT License](../LICENSE)，第三方原始材料不被改授 MIT，具体边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。便携组合交付继续保留适用的 copyleft 义务。PyAppify 原发行物署名信息、上游图标及翻译保留来源和原权利，不替第三方新增授权。按维护者 2026-10-31 补充说明，游戏运行模板来自正常游戏截图裁片，未从游戏包解包，仅用于界面定位，项目非盈利；游戏图像和商标不纳入项目原创代码的 MIT 授权。

公开源码与更新源为 `Auth3r/ok-leiming`。原私人开发仓库原地改名为 `Auth3r/ok-leiming-private-archive`，保持私有、原 ID、历史、PR/Issue 和旧 Release；本仓库从已脱敏快照开始，见 [PUBLICATION_CHECK.md](PUBLICATION_CHECK.md)。

## Beta 6 的实际断点

旧候选 `fd2abdc` 的真实 EXE 点击检查更新时报 `PyAppify executable was not found`。`9fae351` 增加的 `UpdateApi` 仍只包装 Launcher API，并未补足服务。实际安装的 `pyappify==1.0.10` Python wheel 是外部 Launcher 的文件通信客户端；便携包没有 Launcher、app.json 或更新源。`portable_runtime.prepare()` 还会清除继承的 `PYAPPIFY_*`，避免外部启动器改写本应用身份。

上游 Rust Launcher 使用 Git 标签、源码和隔离 Python/pip 环境。迁入它需要重做本项目现有 PyInstaller ZIP 发布布局，并调整源码运行与依赖部署方式。它不是可直接配置成便携 ZIP 替换器的组件，因此本次保留上游 UpdateCard 控件和后台线程，接入项目自己的 Releases 传输与安装事务。

## Beta 7 的更新调用链

1. `DiagnosticsApp.on_show_main_window()` 调用 `install_update_api()`，将实际关于页控件接入 `GitHubUpdateClient`。Beta 默认勾选测试版本，Beta 不再误标为正式版。
2. 查询 `https://api.github.com/repos/Auth3r/ok-leiming/releases`，遍历分页，排除 draft 和无效标签。使用语义版本排序；不依赖排除预发布的 latest 端点。
3. 选择高于当前安装版本的发布，重新检查发布状态，匹配完整 `Leiming-Automation-<tag>-win-x64.zip` 和同名 `.sha256`。资产下载使用固定仓库的 API ID；认证头不传给 CDN 重定向。
4. 验证下载长度、SHA-256、ZIP 路径、manifest 仓库/平台/版本/源码提交/非 trial 身份及每个文件哈希。拒绝配置注入、未登记文件和路径逃逸。
5. `start_update_install()` 在安装目录旁建立独立 helper，复制当前 EXE 与所需 `_internal`。主入口先识别 `--apply-update`，在 Qt 和单实例检查前执行安装事务。
6. worker 等旧进程退出后再次校验暂存包、复制个人配置与运行数据，保留完整旧目录备份，再交换目录并重启。实际 GUI 就绪后回传版本、Git SHA、路径和进程号；失败保留或恢复旧版本并报告原因。只读安装或文件占用导致失败时不强行提权、不终止其他进程。

所有游戏策略保持既有行为。任务运行时可以检查版本，安装前须先停止任务；更新准备失败不阻止正常启动。未发布 Release、网络失败、权限错误、无更新和无有效版本分别展示，空列表不视为安装成功。

## 公开访问和当前发布状态

公开更新源为 `Auth3r/ok-leiming`，默认匿名读取 Releases，普通用户无需 GitHub Token。程序不读取开发者 Git 凭据，也不持久保存凭据。

当前应用版本为正式版 `v1.0.0`，保留 Beta 8 的既有业务行为；是否已对外发布，以本项目 Releases 为准。Beta 7／8 的历史发布记录和资产保持不变。标签、包内与外部 manifest、对应源码及实际 EXE 必须指向同一完整提交。私人档案内的同名历史版本与原 Release ID 不用于本公开发布身份。

Beta 6 不包含此更新器，首次迁入必须手动下载并完整解压 v1.0.0 包。Beta 7／8 可通过已有更新器升级到正式版。v1.0.0 默认仅检查正式发布，手动勾选测试版本后才包含 Beta／RC；检查到同版时不安装、不降级。完整线上升级必须有真实源版本、较高目标版本及安装／重启回执，不能用同版查询代替。

## 版本来源与发布约束

`src/version.py` 为单一来源，pyproject 动态读取等价 PEP 440 版本。构建工具派生 PE 数值及文本版本、ZIP 名称、发布标签和 manifest；常规生产构建拒绝脏源码、依赖不符和已存在的同名标签。首个公开 Beta 7 曾从脱敏干净快照构建，未复用私人旧包。v1.0.0 继续从固定干净提交重新构建，再创建对应正式标签，不放宽包身份校验。包内清单记录全量运行文件哈希，外部清单记录 ZIP 哈希。具体命令与冻结/验收/发布次序见 [RELEASING.md](RELEASING.md)。

## 是否需要与上游绑定或同步

不需要注册或绑定上游，也不需要为了分发本项目而向上游提交代码。四件事应分别处理：使用框架是运行依赖；保留许可证与声明是分发义务；配置更新源是本项目发布实现；向上游贡献代码是独立协作行为。

[模板官方说明](https://github.com/ok-oldking/ok-script-app/blob/master/README.md) 将其定义为应用起步模板；[PyAppify 官方说明](https://github.com/ok-oldking/pyappify/blob/master/README.md) 使用应用自己的 `pyappify.yml`、`git_url` 和版本标签；[ok-script 发布指南](https://github.com/ok-oldking/ok-script/blob/master/docs/after_quick_start/README.md) 描述自有更新仓库与镜像配置。这些是可选发布方案，本项目当前使用的完整 PyInstaller 便携包不因此自动获得更新能力。
