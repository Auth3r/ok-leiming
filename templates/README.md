# 固定 4K 模板

46 张裁片来自用户上传的 15 张截图与 `流程.mp4` 中已检查的画面。运行时通过上游模板匹配 API 搜索限定 ROI，不使用图像生成模型。

`manifest.json` 中每项记录：`file`（裁片）、`source`（原图名或视频秒数）、`crop`（原始 4K 坐标 x1/y1/x2/y2）、`roi`（运行时搜索区域）。视频模板在裁片生成阶段从 1080p 上采样至 4K。

重新生成：

```powershell
.\.venv\Scripts\python.exe tools\build_templates.py --images 'D:\Movies\成品\流程' --video 'D:\Movies\成品\流程.mp4'
```

更换模板后运行 `tools/check_evidence.py`，必须同时检查正例和不可领取／0 次数反例。仅改分辨率配置不会自动适配另一尺寸。

COCO 文件是框架初始化入口；具体匹配使用 `Vision` 将独立裁片和 ROI 传入框架，无需保存整张人物背景图。

待补：黑盾队员可用／禁用／已派遣与派遣成功、委托列表底部。现有模板中不包含可供自动派遣使用的人物图。
