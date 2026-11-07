# MoviePilot V3 插件集合与开发仓库

本项目是面向 **MoviePilot V3** 的插件开发与管理仓库。所有插件工具遵循独立存放、互不干扰的架构设计，每个插件拥有专属的功能目录、详细说明文档与测试套件。

---

## 🗂 插件工具目录索引 (Plugin Registry)

每个插件工具代码互相独立存放，目录位于 `plugins.v3/` 下，配套的单元测试存放于 `tests/v3/`。

| 插件名称 (Plugin Name) | 目录位置 (Directory) | 版本 (Version) | 功能简介 (Description) | 状态 (Status) |
| :--- | :--- | :--- | :--- | :--- |
| **123云盘助手 (Pan123Pilot)** | [`plugins.v3/pan123pilot/`](./plugins.v3/pan123pilot/README.md) | `1.5.3` | 123云盘助手：Web原生秒传与直填Token、单飞并发互斥锁与C级file_digest极速单哈希计算、正片优先调度与Web S3伴生小文件分片上传、正片与刮削伴生文件全量Symedia Webhook实时联动、伴生文件与qB任务解耦隔离、交互式分页看板(首页/上下页/页码直跳/正片与伴生分类筛选)与/records检索、影视多级目录级联同步、精准做种达标判定、qB状态标签联动与紧急空间腾退机制 | 活跃 (Active) |
| **123云盘TG转存助手 (Pan123TgPilot)** | [`plugins.v3/pan123tgpilot/`](./plugins.v3/pan123tgpilot/README.md) | `1.1.2` | 123云盘 Telegram 频道智能转存与画质洗版助手：实时监听TG频道、电影与剧集智能分流转存至指定目录、归集子目录预检复用与失效自愈重建(严守用户配置父目录ID绝不乱丢根目录0)、Web V3标准回收站协议精准清理洗版旧文件、跨频道规范化精准去重(未达洗版条件不重复转存)、链接指纹库秒传去重、全量同步伴生文件(视频/字幕/NFO/海报图片)、完美解析带空格与特殊符号的123FSLinkV2秒传链接防丢后缀、多维画质评分与自动洗版升级、已转存资源分页数据表与关键字/状态检索、记录保留天数自动清理 | 活跃 (Active) |

> 💡 **提示**：每当新增或更新插件工具时，请同步更新此表格，确保项目工具目录与功能状态清晰可查。

---

## 📁 仓库目录结构规范

```text
MoviePilot-Plugins/
├── plugins.v3/                       # V3 插件源码根目录（各插件独立文件夹）
│   └── <plugin_id_lower>/            # 具体插件独立目录（如 pan123pilot）
│       ├── __init__.py               # 插件主入口文件（继承自 _PluginBase）
│       ├── README.md                 # 该插件的使用说明与功能详解文档
│       └── pyproject.toml            # 插件可选的独立 Python 依赖配置
├── tests/                            # 自动化测试目录
│   └── v3/
│       └── <plugin_id_lower>/        # 对应插件的独立测试用例
│           └── test_*.py
├── package.v3.json                   # 插件市场元数据索引清单
└── README.md                         # 项目总体说明与工具索引
```

---

## 🚀 插件测试与验证

### 1. 语法校验
代码编写或修改完成后，执行 Python 语法校验：
```bash
python3 -m compileall plugins.v3/<plugin_dir>/
```

### 2. 测试验证 (Docker / venv)
- **单元测试**：
  ```bash
  python3 -m unittest discover -s tests/v3/<plugin_dir>/ -p "test_*.py"
  ```
- **宿主/容器环境**：将插件目录挂载到 MoviePilot V3 运行容器的插件目录下进行实时加载与功能联调。
