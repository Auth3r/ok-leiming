# 第三方声明与许可证适用范围

本清单适用于镭闪自动化 1.0 Beta 7。项目仓库为 [Auth3r/ok-leiming](https://github.com/Auth3r/ok-leiming)，应用版本由 `src/version.py` 提供；实际交付还应核对 Git 提交与 `release-manifest.json`。

## 项目原创部分：MIT

`Copyright (c) 2026 Auth3r`。根目录 [LICENSE](../LICENSE) 中的 MIT 条款仅适用于项目所有者有权授权的原创业务代码、测试代码、构建脚本及原创文档，包含其按要求生成并维护的代码。Git 提交者名称中的工具标识不作为第三方资源版权主体的新声明。

以下内容不由这份 MIT 文件重新授权：第三方运行库与模型、上游保留的图标和翻译、游戏图像与商标、外来截图和历史素材，以及其随附的原始许可证。公开仓库中的文件位置或 Git 跟踪状态本身不是授权证明。

## 保留材料与来源

| 内容 | 来源及适用条款 | 本项目处理 |
| --- | --- | --- |
| `ok-script==2.0.0b7` | [ok-oldking/ok-script](https://github.com/ok-oldking/ok-script)，实际 wheel 附带 AGPLv3 | 保留 [原始许可](OK_SCRIPT_LICENSE.txt)；便携组合交付保留适用的 AGPL 对应源码义务，MIT 不覆盖框架 |
| Qt / PySide6-Essentials / Shiboken6 6.11.1 | LGPLv3 等开源选项及内部第三方条款 | 保留准确版本的许可树、通知、源码来源及动态库替换说明，不声称持有商业许可 |
| PySide6-Fluent-Widgets 1.11.3 | GPLv3；另有上游商业授权选项 | 保留原始 LICENSE 和对应源码 |
| PySideSix-Frameless-Window 0.8.2、pynput 1.8.2 | LGPLv3 | 保留原始许可、对应 Python 源码与适用义务 |
| `pyappify==1.0.10` Python 包 | [ok-oldking/pyappify-python](https://github.com/ok-oldking/pyappify-python)，同版本元数据声明 MIT classifier，但缺少独立许可证署名 | 保留真实元数据、原始源码、标准 MIT 参考条款与既有溯源记录；不替上游编造姓名或年份。它不同于同名 Rust Launcher 项目 |
| `onnxocr-ppocrv5==0.0.20` 与附带模型 | OnnxOCR / PaddleOCR 来源，随发行物保留 Apache-2.0 文本 | 保留模型来源、版权及文件哈希；不将模型重命名为自有作品 |
| OpenVINO、OpenCV、OpenCC 及其他依赖 | 各实际锁定发行物及内部库的原始许可证 | 以包内原文和准确版本为准，不用许可证名称摘要替代通知 |
| Python、PyInstaller、Mesa / LLVM、Microsoft 运行组件 | 各自许可、引导程序例外及再分发条款 | 继续保留 [现有完整分发清单](../tools/release-licenses/THIRD-PARTY-NOTICES.md) 与准确原文，不改授 MIT |
| `icons/icon.png`、`i18n/{en_US,zh_CN}/LC_MESSAGES/ok.{po,mo}`、`docs/UPSTREAM_README.md` 及沿用的模板起步内容 | ok-script-app，起步参考提交 `f134d07ce064c49060068c0511b5592b0e78a081`；中文翻译含后续修改 | 保留上游来源和原始权利，不由项目 MIT 重新授权，不补造上游未提供的署名或许可 |
| `templates/` 中的游戏运行模板 | 按维护者 2026-10-31 补充说明，来自正常游戏画面的截图裁片，未从游戏包解包，仅用于界面定位，项目非盈利；来源见各 manifest | 游戏图像与商标保留原权利，不属于项目原创代码的 MIT 授权；来源与用途陈述不作为新增第三方授权声明 |
| 私人开发截图、游戏回放及验收采证 | 保留于维护者私有档案 | 不导入本公开快照；运行模板仍按上行来源与原权利保留 |

许可证原文、组件准确版本和对应源码材料通过现有构建流程收集到便携包 `licenses/`；详见 [LICENSE-PROVENANCE.json](../tools/release-licenses/LICENSE-PROVENANCE.json) 及其配套文本。为原创代码采用 MIT 不减少任何第三方 copyleft、版权通知或对应源码义务，也不把组合交付物整体变成仅受 MIT 约束的软件。

## 本次公开分发范围

公开仓库从已脱敏最终源码快照开始，不导入私人开发历史、账号/聊天采证和个人数据。原开发仓库及全部旧 Release 保持在独立私有档案；公开范围见 [PUBLICATION_CHECK.md](PUBLICATION_CHECK.md)。

游戏材料、上游图标和翻译保留原始权利及来源说明。PyAppify 原始元数据和署名缺口如实记录，不编造版权人或扩大 MIT 范围。便携包保留完整 `licenses` 和适用对应源码，接收人依各组件原许可享有相应权利。

## 项目与游戏的关系

本项目与游戏官方无隶属或认可关系。游戏名称、商标及图像归相应权利人。MIT 的许可与免责范围仅及于可由本项目授权的内容，不替游戏权利人或其他第三方作兼容性、安全性或授权保证。
