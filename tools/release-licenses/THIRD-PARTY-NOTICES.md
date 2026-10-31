# 第三方组件、许可证与对应源码

本文件随《镭明闪击》自动化 Windows 便携包分发。第三方组件的版权归各原作者所有，使用条款以各组件随包保留的原始许可证、版权通知和下列补充正文为准。本说明不把第三方组件改为本项目所有。

`dependency-manifest.json` 列出构建环境中的分发名称、准确版本及原始许可字段；各同名版本目录保留其 `METADATA.txt` 与上游许可证树。构建工具也可能出现在说明中，以便交代最终 EXE 的来源；清单不意味着每个依赖的每个模块都被加载。

## 主要组件

| 组件／分发版本 | 许可及随包材料 |
| --- | --- |
| Python 3.12 运行时 | PSF 及其所含第三方许可；见 `Python-LICENSE.txt`。实际补丁版本见 `release-manifest.json`。 |
| ok-script 2.0.0b7 | AGPL-3.0；见 `AGPL-3.0.txt`、原包许可及对应源码中的 `third-party-source/ok`。上游：https://github.com/ok-oldking/ok-script 。 |
| PySide6-Essentials／Shiboken6 6.11.1 与使用的 Qt 6.11.1 库 | 本次采用开源 LGPL-3.0 选项；见 `LGPL-3.0.txt`、`GPL-3.0.txt`、Qt 第三方通知及下方源码和替换说明。原始 metadata 所列其他可选许可一并保留，不声称拥有 Qt 商业许可。 |
| PySide6-Fluent-Widgets 1.11.3 | GPL-3.0；见原包 LICENSE、`GPL-3.0.txt`、对应源码中的 `third-party-source/qfluentwidgets`。上游：https://github.com/zhiyiYo/PyQt-Fluent-Widgets/tree/PySide6 。 |
| PySideSix-Frameless-Window 0.8.2 | LGPL-3.0；见原包 LICENSE、GNU 正文及对应源码中的 `third-party-source/qframelesswindow`。上游：https://github.com/zhiyiYo/PyQt-Frameless-Window/tree/PySide6 。 |
| pynput 1.8.2 | LGPL-3.0；保留原包 `COPYING.LGPL`，对应 Python 源码随应用对应源码交付。上游：https://github.com/moses-palmer/pynput 。 |
| onnxocr-ppocrv5 0.0.20 与随包 OCR 模型 | Apache-2.0；见原包 LICENSE、`Apache-2.0.txt` 和下方模型来源。 |
| OpenVINO 2026.3.0、OpenCV-Python 5.0.0.93、OpenCC 1.2.0 | Apache-2.0 及各自所含第三方许可；保留原包许可树，不以一份 Apache 正文替代其内部组件通知。 |
| PyDirectInput 1.0.4 | MIT；`PyDirectInput-MIT.txt` 为其 metadata 项目链接指向的官方 `LICENSE.txt` 原文，版权通知为 Ben Johnson（2020），全文另含上游致谢。上游：https://github.com/learncodebygaming/pydirectinput 。 |
| pyappify 1.0.10（Python 包） | 上游 metadata 与同版本源包明确声明 MIT classifier；详见下方原始材料及标准条款说明。 |
| PyInstaller 6.22.3 引导程序 | GPL-2.0-or-later WITH Bootloader-exception；见实际构建工具原始 `PyInstaller-6.22.3-COPYING.txt`，包含完整许可及引导程序例外。例外允许按应用自身许可分发由其打包的程序。 |
| Mesa software OpenGL／LLVM | 随 PySide6 提供的 `opengl32sw.dll`；Mesa 11.2.2 和 LLVM 3.6.2 的原许可、版权与补充通知见同目录文件及下方来源说明。 |
| Microsoft Visual C++ 运行库 | 单独受 Microsoft 许可／再分发条款约束，不被改授 GPL、LGPL 或 MIT。见随包 Microsoft 原始条款及官方再分发说明。只保留上游发行中供运行使用的未修改库，不包含 Debug 非再分发目录。 |

NumPy、Pillow、Shapely／GEOS、pywin32、requests 等其余组件和内部库的许可、版权通知见各准确版本目录。摘要和 metadata 的空许可字段不能替代原始正文。

## 对应源码与公开交付

`licenses/corresponding-source.zip` 随同运行包交付应用源码、资源、依赖声明和构建脚本，以及需要交付的第三方 Python 源码。源码的发布提交身份来自随包 `release-manifest.json`；不包含 `.git`、账号、个人配置、研发采证、实机截图或历史测试录像。

本应用与 ok-script 结合的交付按 AGPL-3.0 提供所需对应源码；GPL／LGPL 组件保持各自原许可。相关库的原版权头不得移除。普通用户启动程序无需阅读或执行源码。

公开源码仓库为 https://github.com/Auth3r/ok-leiming ，对应源码压缩包也随完整安装包交付。公开分发保留接收人依相关开源许可享有的复制、修改和再分发权利；分发者不得对这些许可覆盖的代码增加相冲突的限制。后续转交便携包应保留整个 `licenses` 目录和对应源码。

## Qt／PySide6 6.11.1 源码与动态库替换

本程序使用动态加载的 Qt／PySide6 库，而非把 Qt 静态链接进应用程序。相关库在便携目录 `_internal/PySide6`；Shiboken 在 `_internal/shiboken6`。许可适用的库修改及为调试这些修改进行的逆向工程不受本应用额外限制。

对应版本的官方源码可直接免费下载：

- Qt 6.11.1：[官方目录](https://download.qt.io/official_releases/qt/6.11/6.11.1/single/)，[完整 tar.xz 源码](https://download.qt.io/official_releases/qt/6.11/6.11.1/single/qt-everywhere-src-6.11.1.tar.xz)。
- PySide6／Shiboken6 6.11.1：[官方目录](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/)，[完整 tar.xz 源码](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.1-src/pyside-setup-everywhere-src-6.11.1.tar.xz)。

要使用修改后的库，先停止并关闭程序、备份便携目录，再将重新编译且接口兼容的 Windows x64 Qt DLL、相关 PySide6 `.pyd`／DLL 与必要插件替换到原位置；PySide6／Shiboken 的构建须与本包 Python 3.12 ABI 一致。保持对应版本、依赖关系和目录结构，不混用不兼容模块。本应用不对这些库设置禁止替换的签名锁。也可使用应用对应源码和构建脚本重新生成便携包。

Qt 所含第三方代码另有许可。`Qt-6.11.1-THIRD-PARTY-NOTICES.md` 保留准确 v6.11.1 的版权通知，`Qt-6.11.1-THIRD-PARTY-LICENSES.zip` 保留官方 `qtbase`、`qtsvg`、`qtimageformats` 的原始 attribution、许可证正文和引用文件。这是包含可能未启用平台组件的保守通知集合，不把 Qt 文档当前小版本的页面当作本包 6.11.1 的精确版权清单。

Qt 官方许可说明：https://doc.qt.io/qt-6.11/lgpl.html 。下载位置及许可证获取 SHA256 在 `LICENSE-PROVENANCE.json` 中记录。

## pyappify Python 包的许可来源

本包采用的是 PyPI `pyappify==1.0.10` Python 包，源项目为 https://github.com/ok-oldking/pyappify-python 。同版本 PyPI 源包中的三份 Python 源文件与 `pyproject.toml` 已与该项目提交 `243049ad391daf6b980396a1dc53ce8e42073fc4` 核对，除换行规范化外内容一致；记录见 `LICENSE-PROVENANCE.json`。

上游 wheel、同版本源包和上述提交未附单独 LICENSE 文件或版权所有者署名，仅在 metadata／pyproject 中明确列出 `License :: OSI Approved :: MIT License`。本包原样保留这些材料，并附 `MIT-standard.txt` 作为该 classifier 所指 MIT 标准完整条款。标准文本的版权占位符属于标准模板，**不是为上游填写的新署名**；没有杜撰作者或版权年份。

同名 `ok-oldking/pyappify` 是另一个 Rust 启动器项目，它的 GPL 许可证不能当作本 Python 包的许可证。若后续上游补充署名或专门许可证，后续分发应保留相应原始通知。

## OCR 模型与来源

OCR 使用 `onnxocr-ppocrv5==0.0.20` 原始分发内的 `onnxocr/models/ppocrv5` 资源，包括检测 `det/det.onnx`、识别 `rec/rec.onnx`、角度分类 `cls/cls.onnx` 和 `ppocrv5_dict.txt`。本应用不训练或修改这些模型权重，也不把模型重命名为自有模型。实际文件大小与 SHA256 见 `LICENSE-PROVENANCE.json`。

该分发的 metadata 指向 [ok-oldking/OnnxOCR](https://github.com/ok-oldking/OnnxOCR)，上游说明其为 [jingsongliujing/OnnxOCR](https://github.com/jingsongliujing/OnnxOCR) 的 fork，并将 PP-OCRv5 模型随包提供。许可采用随包保留的 Apache-2.0 正文；PP-OCRv5 原始项目来源为 [PaddlePaddle/PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)。PaddleOCR 原始代码／模型项目声明 Apache-2.0；本包保留 PaddlePaddle 原版权通知和模型分发方许可，不根据模型文件大小猜测 mobile／server 变体。

## Mesa、LLVM 与 Windows 运行组件

`opengl32sw.dll` 是 PySide6 随包提供的软件 OpenGL 备用库，其原始文件 SHA256 为 `4a7d90f91fdecb5df7b426bc2d05974b8d7ffa450af2d1f93f3eca05800718da`。其 PE 各代码／数据节与 [Qt 官方 llvmpipe 目录](https://download.qt.io/development_releases/prebuilt/llvmpipe/windows/) 的 Mesa 11.2.2 `signed_sha256` 分发逐节一致，整文件因签名数据不同而不声称 SHA256 相同。库内版本文本包含 `Mesa 11.2.2` 和 `LLVM 3.6.2`。

`Mesa-11.2.2-LICENSE-NOTICE.txt` 和 `Mesa-11.2.2-COPYING.txt` 来自 [Mesa 官方 11.2.2 源码](https://archive.mesa3d.org/older-versions/11.x/11.2.2/mesa-11.2.2.tar.xz)，保留其默认 MIT 和组件许可说明。`LLVM-3.6.2-LICENSE.txt` 及补充通知来自 [LLVM 官方 3.6.2](https://releases.llvm.org/3.6.2/)。本发布没有额外加入另一份 OpenGL DLL。

Microsoft 组件的版权归 Microsoft Corporation。原运行库许可见 `Microsoft-Visual-C-Runtime-2015-2022-License.txt`；分发者的再分发条件另见 Visual Studio 原条款中的 Distributable Code 和 [官方再分发列表](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution)。运行库终端用户条款不是向打包者新授予再分发权；分发者应使用合法上游提供、被允许再分发的未修改运行组件。相应原始文本保留，不向其附加本应用的开源许可。

## 发布检查要求

以下缺项或冲突应先修复，不能将包标为完成合规交付：

- AGPL／GPL 的正文或适用对应源码缺失，或源码与所分发版本不对应。
- LGPL 正文／引用的 GPL 正文缺失，适用库源码或替换／重新组合方式缺失，或为修改库设置相冲突的限制。
- MIT、BSD、Apache 等原始版权／许可通知被移除；Apache 组件存在原始 NOTICE 时未保留；OCR 权重来源无法确认。
- Qt 嵌入第三方组件只列链接或许可名称而未提供应保留的原通知，或误用 Qt 商业许可声明为开源分发授权。
- 再分发了许可不明、未经授权、Debug 非再分发或被修改的 Microsoft 运行组件。

本次对 pyappify 的上游缺署名情况按上述原始材料如实记录，没有声称下载到了上游未提供的独立 MIT 文件。所有获取的原文及可核对来源保存在 `licenses`；再次分发时不要删减该目录。
