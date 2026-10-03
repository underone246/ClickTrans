# 划译 ClickTrans

鼠标框选屏幕任意区域 → 本地 OCR → 大模型翻译 → 悬浮卡片实时显示译文。
支持**中文 / 日语 / 英语**三语任意互译。

```
按下 Alt+Q  →  拖拽框选  →  松手  →  译文边流式出来边显示
```

截图和文字识别全程在本机完成，**只有识别出的纯文本会发到翻译接口**。

> **不想装 Python？** 直接下载打包好的单文件 exe（约 127 MB，双击即用、无需任何依赖）：
> **[⬇ Releases](https://github.com/underone246/ClickTrans/releases/latest)**

---

## 快速开始

### 0. 最省事：直接下 exe

去 **[Releases](https://github.com/underone246/ClickTrans/releases/latest)** 下载
`ClickTrans.exe`，双击运行即可，跳过下面所有环境配置步骤。

### 1. 从源码运行（准备环境）

需要 Python 3.11+（本机用的是 3.13）。

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 启动

```bash
.venv\Scripts\python.exe run.py
```

启动后程序常驻系统托盘（图标可能在「^」折叠区里）。首次启动会在后台加载 OCR 模型，
托盘图标从虚线变实心就绪，实测约 0.4-0.5 秒。

### 3. 配置 API Key

右键托盘图标 → **设置翻译 API Key…**，粘贴 Key。

Key 用 Windows DPAPI 按当前用户加密，存在 `~/.clicktrans/secret.bin`，
**不会**写进 `config.toml`，也不会因为配置被同步到网盘而泄露。

也支持用环境变量临时覆盖：`CLICKTRANS_API_KEY=sk-xxx`。

---

## 使用

| 操作 | 说明 |
|---|---|
| `Alt+Q` | 唤起框选（快捷键可在 `config.toml` 改） |
| 拖拽 | 框选要翻译的区域 |
| `Shift` | 框选时按住，循环切换偏好语言（中文 → 日语 → 英语） |
| `Esc` | 取消框选 / 关闭结果卡片 |
| 双击托盘图标 | 直接框选（等同热键） |
| 中键点托盘图标 | 打开历史记录 |

**结果卡片**下方的四个按钮：

- `原文` — 在原文和译文之间切换
- `复制` — 复制当前显示的内容
- `重译` — 用同一块选区重新翻译
- `换向` — 交换源语言和目标语言（比如英→中 变成 中→英）

### 语言方向是怎么定的

你只需要设一个**偏好语言**（默认中文），实际方向自动推导：

> 检测到的源语言 ≠ 偏好语言 → 译成偏好语言；相等 → 译成回退语言

| 检测到的源语言 | 偏好 = 中文 | 偏好 = 日语 | 偏好 = 英语 |
|---|---|---|---|
| 中文 | → 英语 | → 日语 | → 英语 |
| 日语 | → 中文 | → 中文 | → 英语 |
| 英语 | → 中文 | → 日语 | → 中文 |

**已知局限**：日语全汉字短句（「株式会社」「確認事項」）没有假名，会被判成中文。
这不影响方向（方向由偏好决定），只会让提示词里注入的语气约束不太对。

### 内存

托盘菜单里能看到实时内存占用。实测（`tools/check_ocr.py`）：

| 状态 | 内存 |
|---|---|
| 解释器 + 依赖导入完（基线） | ~59 MB |
| 模型已加载（活跃） | ~119 MB（+60 MB） |
| 空闲 30 分钟后自动卸载 | ~107 MB（**只收回 12 MB**） |
| 完整应用跑完一次识别（含所有界面对象） | ~146 MB |

**卸载只收回 12 MB 是正常的**，不是卸载没生效：mobile 权重本身就小，真正
吃内存的 opencv / onnxruntime 运行时一旦导入就常驻，不会还回去。
所以别指望它降回空载——**也别为了这个去关 onnxruntime 的内存池**，
实测那只会让推理慢一倍而不会多释放内存（详见 ARCHITECTURE.md 7.9）。

托盘菜单可以手动「立即卸载模型」，也可以把空闲卸载改成 10 / 30 / 60 分钟或关闭。

### 速度

从松手到译文开始往外蹦，耗时几乎全部花在 OCR 上，而 OCR 的耗时**由选区大小决定**：

| 选区（像素） | OCR 耗时 |
|---|---|
| 小（~10 万） | 约 100 ms |
| 中（~30 万） | 约 230 ms |
| 大（接近 400 万上限） | 约 730 ms |

所以框选时**不要把整屏都框进去**——框住要读的那几行就够了，差距是 7 倍。

### 历史记录

托盘菜单 → **历史记录…**。数据存在 `~/.clicktrans/history.json`：

- 普通条目滚动保留 500 条，置顶条目不占配额
- 相同原文 + 相同语言对会自动命中复用（同时充当翻译缓存）
- 无网络时：先查历史有没有同一条原文，命中就直接显示；没命中就显示识别出的原文并暂存，联网后可重译
- 支持搜索、按方向筛选、导出 Markdown

> `history.json` 是**明文**的（要能直接看、直接手改）。框选过敏感内容记得清空。

---

## 打包成单文件 exe

```bash
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe -m PyInstaller build/clicktrans.spec \
    --noconfirm --distpath dist --workpath build/_pyi
# 产物：dist/ClickTrans.exe（约 127 MB，单文件免依赖）
```

已验证：exe 启动后能注册全局热键、从**打包进去的权重**加载 OCR 模型
（约 470 ms），常驻内存约 142 MB，与源码运行一致。

两个容易踩的坑，spec 里已经处理掉了，但值得知道：

- **RapidOCR 的 onnx 权重和 config.yaml 必须手动收进去。** PyInstaller 只收
  `.py`，漏掉这些文件打包照样成功，但一框选就报「模型路径不存在」。
- **PIL 不能排除。** 它看着像只有测试在用，实际 `rapidocr_onnxruntime/utils.py`
  直接 `from PIL import Image`，排掉就启动崩。

打包后配置、历史、日志仍写在 `~/.clicktrans/`，与源码运行共用同一份，
所以在两者之间切换设置不会丢。

---

## 配置文件

`~/.clicktrans/config.toml`（首次启动自动生成）

```toml
[general]
hotkey = "alt+q"
preferred_lang = "zh"       # 偏好语言 zh | ja | en
launch_at_startup = false
single_instance = true

[ocr]
engine = "rapidocr"
model = "mobile"              # mobile | server
upscale = 1.0                 # 检测器内部已会缩放，再放大是重复劳动
min_confidence = 0.6
unload_after_idle_min = 30    # 0 = 永不卸载
max_pixels = 4000000          # 超大选区先缩放，这是延迟的主要来源
det_limit_side_len = 960      # 限制长边（不是放大短边）
det_limit_type = "max"
use_angle_cls = false         # 屏幕文字不会是倒的，方向分类纯属浪费
rec_batch_num = 6

[translate]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
stream = true
timeout_s = 20
proxy = ""                  # 留空则读系统代理
ja_style = "polite"         # 日语译文风格 polite | plain

[history]
limit = 500
dedupe = true

[glossary]
# 术语表，对所有语言方向生效
# "Transformer" = "Transformer"
```

**换翻译服务商**只需要改 `base_url` + `model`，任何 OpenAI 兼容接口都行
（DeepSeek / 通义 / Kimi / OpenRouter / 本地 Ollama…）。

**关于 `model = "server"`**：server 版 OCR 精度略高但权重 200 MB 起步。
需要自己把 `*det*.onnx` / `*rec*.onnx` / `*cls*.onnx` 放到
`~/.clicktrans/models/server/`，找不到会自动回退 mobile 并记日志。

---

## 开发

```bash
# 单元测试（坐标换算 / 语言判定 / 历史淘汰 / 文本重组）—— 共 89 项
.venv\Scripts\python.exe -m pytest tests -q

# 不启动 GUI，验证 OCR 链路 + 内存回收
.venv\Scripts\python.exe tools/check_ocr.py

# 无头集成冒烟：卡片 + 托盘 + 历史面板 + 真实跑一遍完整管线
.venv\Scripts\python.exe tools/smoke_gui.py

# 调参用：每组配置独立子进程跑基准
.venv\Scripts\python.exe tools/tune_ocr.py
```

`tools/check_ocr.py` 会生成一张中/日/英混排的测试图，跑一遍识别，
并打印「加载前 / 加载后 / 卸载后」三个内存数字。**第三个数字只降 8-13 MB
是正常的**（模型很小，opencv / onnxruntime 的运行时收不走）；真正要看的
是识别耗时和 `[校验] 命中关键词 4/4`。

`tools/smoke_gui.py` 在 offscreen 模式下把真实对象都造出来跑一遍，覆盖
「裁剪 → OCR → 语言判定 → 翻译失败提示」整条链路。**注意它必须用 PIL 画
测试图**：offscreen 平台没有字体数据库，QPainter 会把文字全渲染成豆腐块，
OCR 一块都识别不出来，会误报成管线坏了。

### 目录结构

```
clicktrans/                ← 项目根
├─ run.py                 开发期启动脚本
├─ build/
│  ├─ clicktrans.spec     PyInstaller 配置
│  └─ launcher.py         打包入口（__main__.py 有相对导入，不能直接当脚本跑）
├─ tools/                 开发工具（测试图渲染、OCR 基准、无头冒烟）
├─ tests/                 单元测试
└─ clicktrans/            包本体
   ├─ lang.py             三语检测与方向决策（纯本地，不发请求）
   ├─ history.py          history.json 读写（历史 + 缓存是同一份数据）
   ├─ pipeline.py         编排器，跑在唯一的工作线程里
   ├─ hotkey.py           Win32 RegisterHotKey 封装
   ├─ capture/
   │  ├─ geometry.py      ★ 多屏 + DPI 坐标换算
   │  ├─ grabber.py       屏幕抓取，含 DXGI 兜底
   │  └─ overlay.py       全屏框选窗
   ├─ ocr/
   │  ├─ base.py          OcrEngine 协议
   │  ├─ layout.py        行 → 段落重组、代码行识别
   │  └─ rapidocr_engine.py  ★ 模型加载 / 卸载 / 关 onnxruntime 线程自旋
   ├─ translate/
   │  ├─ prompts.py       方向感知的提示词
   │  └─ llm.py           OpenAI 兼容 + SSE 流式
   ├─ ui/
   │  ├─ card.py          ★ 结果悬浮卡片（限频渲染）
   │  ├─ history_panel.py 历史记录面板
   │  └─ tray.py          托盘菜单
   └─ utils/              日志 / 单实例 / 内存读数 / 开机自启
```

架构设计的完整说明见 **[ARCHITECTURE.md](ARCHITECTURE.md)**。

---

## 设计上的几个关键取舍

**覆盖窗显示的是「已经抓好的静态截图」，不是透明玻璃窗。**
透明窗在视频播放器 / GPU 加速窗口 / 独占全屏下抓不到底下内容；
静态截图保证所见即所得。这也让覆盖窗可以放心抢焦点，Esc / Shift 的键盘
处理因此变得非常可靠。

**翻译接口返回的是 Iterator 而不是字符串。** 这是「实时」的唯一来源——
返回完整结果的话，用户就要盯着加载圈等一秒多。

**只有一个工作线程。** ONNX 推理会话不是线程安全的，多线程要么加锁
（等于串行），要么每线程一份模型（内存翻倍）。

**历史记录和翻译缓存是同一份数据。** 省掉了一整套缓存层。

---

## 已知问题

- **日语假名识别偏弱**。RapidOCR 自带的模型以中英为主，片假名和小字号假名
  容易识别错。需要更高精度的话，按上面说明换 server 版模型。
- **日语全汉字短句会误判成中文**（见上文「语言方向」）。
- **不支持跨屏框选**。拖拽被限制在鼠标按下时所在的那块屏幕内——跨屏窗口
  在混合 DPI 下会被系统拉伸，选框和实际内容对不上。
- **DXGI 独占全屏**（部分游戏、全屏视频）需要额外装 `dxcam` 才能兜底：
  `pip install dxcam`。不装的话会拿到纯黑画面。
- **大选区会明显变慢**。4M 像素上限下约 730 ms，是纯推理时间，没有取巧空间。
- **exe 启动比源码慢一点**。单文件包每次运行都要把 127 MB 解压到临时目录，
  冷启动多花 1-2 秒。介意的话可以把 spec 改成 `--onedir`（`COLLECT` 模式），
  代价是变成一个文件夹。

---

## 许可证

[MIT](LICENSE) © 2026 underone246

简单说：随便用、随便改、随便商用，**但出问题别找我**，且需保留版权声明。
