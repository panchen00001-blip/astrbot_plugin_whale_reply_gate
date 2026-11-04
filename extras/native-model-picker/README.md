# 原生设置页的模型获取按钮

这是可选的 AstrBot WebUI 扩展，在原生设置页“Jev 模型”右侧增加“获取模型”按钮。插件自带的控制面板已经提供同一功能。

此扩展固定到 AstrBot **4.29.3**，源码提交为 `f61a7f42696da6df25dc7a249ec9b8d54ed9da18`。按钮使用当前表单中的接口地址和密钥获取列表；选择后仍由原生保存按钮保存配置。

| 文件 | 职责 |
| --- | --- |
| `PluginModelPicker.vue` | 模型选项、按钮和请求状态 |
| `native-model-picker.patch` | 接入 AstrBot 配置渲染器 |
| `build.py` | 下载固定源码、应用补丁和构建 |

## 构建

需要 Python 3.12、Node.js 22、npm 和 patch。pnpm 固定为 10.15.1，由脚本调用。

在本目录执行：

```bash
python3 build.py
```

脚本只生成 `.runtime/dist/`，不会修改正在运行的 AstrBot。构建使用固定源码和上游锁文件，包含 Vue 类型检查。

## 安装

把 `.runtime/dist/` 的内容复制到对应 AstrBot 实例持久化数据目录的 `dist/` 下，再重启该实例。例如，`ASTRBOT_DATA` 填写你自己的数据目录：

```bash
ASTRBOT_DATA=/path/to/astrbot/data
mkdir -p "$ASTRBOT_DATA/dist"
cp -a .runtime/dist/. "$ASTRBOT_DATA/dist/"
```

安装前保存已有的 `dist/`，以便恢复。恢复时移走自定义 `dist/`，重启后使用镜像自带的 WebUI。升级 AstrBot 时，这份固定版本的扩展需要同步适配。

扩展通过插件已有的 `/models` 接口获取列表。源码、依赖与生成文件都位于被 Git 忽略的 `.runtime/`，仓库仅包含组件、补丁和构建脚本。
