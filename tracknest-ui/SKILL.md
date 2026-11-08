---
name: tracknest-ui
description: Reuse or maintain the TrackNest UI design system in websites and admin tools, including its warm neutral themes, aligned layouts, forms, tables and disclosures. Use when the user requests this style or asks to update this UI package, not for unrelated visual redesigns.
---

# TrackNest UI

复用 TrackNest 的暖灰色、细边框、小圆角界面。使用包内的实际资源和页面结构，不凭截图重新猜测样式。

## 版本与参考

- 先读取 [VERSION](VERSION)，这是本包的版本来源；[CHANGELOG.md](CHANGELOG.md) 记录每次变更。
- 读取 [UI 规范](references/ui-contract.md)，了解组件、布局、主题和集成方式。
- [assets/workspace.html](assets/workspace.html) 是无框架的完整布局参考；[assets/example.html](assets/example.html) 展示组件及状态。
- [界面参考图](references/workspace-reference.jpg) 展示 1.0.0 的主次层级与对齐。图中的产品文案和业务数据仅供示意。

## 在新项目中使用

1. 将本 skill 的 `assets/` 完整复制到目标项目自己的静态资源目录，保留内部相对路径，并将 `VERSION` 复制到该目录记录所用版本。不要从 TrackNest 的 `templates/` 或运行数据目录复制内容。
2. 在 HTML 的 `head` 中加载 `theme.js`，再加载 `ui.css`。页面设置 `lang="zh-CN"` 或 `lang="en"`；使用主题按钮时保留 `id="theme"` 和可访问名称。脚本在绘制前恢复 `ui-theme`，并在 DOM 就绪后绑定按钮。主题脚本是可选的，固定主题只需 CSS 和 `data-theme`。
3. 从 `workspace.html` 选择需要的结构，替换示例标题、品牌、数据和交互。默认复用布局与组件类；业务权限、路由、表单提交和翻译由新项目实现。
4. 在消费项目自己的样式中添加业务特有布局。调整颜色先使用主题语义变量，避免散落的硬编码颜色；无需添加 CSS 框架、外部字体或构建工具。
5. 检查桌面和 390px 宽度、明暗主题、长标题、表单底部对齐、表格操作列和折叠区的键盘操作。页面本身不应横向溢出，必要时只让表格容器滚动。

使用示例：`使用 $tracknest-ui，按当前版本为这个项目制作后台管理页面。`

## 维护版本

仅在用户要求维护设计包时修改包内资源；普通项目改动默认留在消费项目。

- `PATCH`：兼容的样式或可访问性修正。
- `MINOR`：新增可复用组件或布局，保持现有接口可用。
- `MAJOR`：删除、重命名类名或改变主题、脚本集成契约。

发布新版本时更新 `VERSION` 和 `CHANGELOG.md`，同步受影响的示例与规范，再验证组件页、布局页及当前消费项目。仓库中的 `tracknest-ui/` 是维护源；个人 skills 目录是安装副本，发布后同步副本。其他项目保留已复制的版本，只有明确升级时才替换资源。
