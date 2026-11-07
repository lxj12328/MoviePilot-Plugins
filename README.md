# MoviePilot V3 插件集合与开发仓库

本项目是面向 **MoviePilot V3** 的插件开发与管理仓库。所有插件工具遵循独立存放、互不干扰的架构设计，每个插件拥有专属的功能目录、详细说明文档与测试套件。

---

## 🗂 插件工具目录索引 (Plugin Registry)

每个插件工具代码互相独立存放，目录位于 `plugins.v3/` 下，配套的单元测试存放于 `tests/v3/`。

| 插件名称 (Plugin Name) | 目录位置 (Directory) | 版本 (Version) | 功能简介 (Description) | 状态 (Status) |
| :--- | :--- | :--- | :--- | :--- |
| **123云盘助手 (Pan123Pilot)** | [`plugins.v3/pan123pilot/`](./plugins.v3/pan123pilot/README.md) | `1.5.3` | 123云盘助手：Web原生秒传与直填Token、单飞并发互斥锁与C级file_digest极速单哈希计算、正片优先调度与Web S3伴生小文件分片上传、正片与刮削伴生文件全量Symedia Webhook实时联动、伴生文件与qB任务解耦隔离、交互式分页看板(首页/上下页/页码直跳/正片与伴生分类筛选)与/records检索、影视多级目录级联同步、精准做种达标判定、qB状态标签联动与紧急空间腾退机制 | 活跃 (Active) |
| **123云盘TG转存助手 (Pan123TgPilot)** | [`plugins.v3/pan123tgpilot/`](./plugins.v3/pan123tgpilot/README.md) | `1.2.0` | 123云盘 Telegram 频道智能转存与画质洗版助手：三级并发极速转存架构(多频道并发抓取+消息并发转存+单分享内多集文件并发秒传，内置全局QPS节流与429退避防风控、两阶段预检合并零漏单)、全兼容*.share.123pan.cn/123pan/个性化分享与全系分流域名、剧集SxxExx(含E01-E04范围)标注与同集精准洗版比对、分享目录树递归Etag秒传落盘、电影与剧集智能分流与子目录自愈重建、Web V3标准回收站清理旧版、置顶筛选与页码滑动窗口分页看板 | 活跃 (Active) |

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
