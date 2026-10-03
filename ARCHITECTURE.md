# 划译 ClickTrans — 架构设计文档

> 一个 Windows 桌面小工具：按热键 → 鼠标拖拽框选屏幕任意区域 → 局部 OCR → 大模型翻译 → 悬浮卡片实时显示译文。
> 暂定名 `clicktrans`，可随时改。

## 1. 目标与约束

**要达成的体验目标**

| 指标 | 目标值 |
|---|---|
| 按热键到覆盖窗出现 | < 80 ms |
| 松手到看到第一个字译文 | < 1.2 s（理想 ~900 ms） |
| 支持语言 | 中文 / 日语 / 英语，任两两互译（6 个方向） |
| 常驻内存 | 活跃 < 200 MB，空闲（模型卸载后）< 130 MB |
| 完全离线可用的部分 | **仅查阅历史记录**（框选翻译依赖网络） |
| 联网时上传的内容 | **仅识别出的文本**，截图不出本机 |

**明确的非目标（第一期不做）**

- **不做本地离线翻译**（不引入 ARGOS / opus-mt 之类的本地模型，那是几百 MB 内存和模型的差价，不划算）
- 不做视频/字幕的连续流式翻译（那是另一套架构，需要定时抓帧 + 帧间差分 + 去重）
- 不做截图外的任何内容采集
- 不做账号体系、云同步

**技术选型（已确认）**

| 层 | 选型 | 理由 |
|---|---|---|
| 语言/UI | Python 3.11 + PyQt6 | 全局热键、透明置顶窗、截图、OCR 生态全在 Python 这边最省事 |
| OCR | RapidOCR + PP-OCRv3 **mobile** 模型 | 权重仅 ~16 MB，比 server 版省 200 MB+ 内存；中日英混排识别够用（注意自带的是 v3 不是 v4，且只覆盖中英文——日语假名偏弱，见 README 已知问题） |
| 翻译 | OpenAI 兼容接口（DeepSeek / 通义 / Kimi 任选） | 质量最好，且**支持 SSE 流式**，这是「实时」的关键；中↔日这类方向也只有大模型能译得通顺 |
| 展示 | 悬浮卡片跟随选区 | 原文可见，适合看代码和文档 |
| 存储 | 单个 `history.json` | 历史记录与翻译缓存合并成一份数据，取消 SQLite，少一套依赖 |
| 打包 | PyInstaller --onefile --noconsole | 单 exe 分发 |

---

## 2. 分层架构

```
┌─ 交互层 ─────────────────────────────────────────────┐
│  托盘菜单 / 设置面板 / 框选覆盖窗 / 结果悬浮卡片        │
│  历史记录面板（离线可用）                              │
├─ 编排层 ─────────────────────────────────────────────┤
│  Pipeline：串联采集→识别→语言判定→翻译→展示，管理取消    │
├─ 能力层（均可替换实现，靠 Protocol 解耦）───────────────┤
│  Capture │ OcrEngine │ LangDetect │ Translator        │
├─ 基础设施层 ─────────────────────────────────────────┤
│  配置中心 / 密钥加密 / 单实例锁 / 日志 / 热键注册         │
│  history.json —— 历史记录 + 翻译缓存（同一份数据）       │
└──────────────────────────────────────────────────────┘
```

**一个重要的边界**：截图和 OCR 全程在本机完成，只有纯文本出网。这既省流量也保隐私——用户框选的可能是有敏感信息的界面。

---

## 3. 目录结构

```
clicktrans/
├─ pyproject.toml
├─ build/clicktrans.spec              # PyInstaller 配置
├─ clicktrans/
│  ├─ __main__.py                     # 入口：单实例检查 → 装配 → 托盘
│  ├─ app.py                          # QApplication 装配、依赖注入
│  ├─ config.py                       # TOML 读写 + API Key 加密
│  ├─ hotkey.py                       # Win32 RegisterHotKey 封装
│  ├─ pipeline.py                     # ★ 核心编排器
│  ├─ lang.py                         # ★ 三语检测 + 翻译方向决策
│  ├─ history.py                      # ★ history.json 读写（历史 + 缓存）
│  ├─ capture/
│  │  ├─ overlay.py                   # 全屏半透明框选窗
│  │  ├─ grabber.py                   # 屏幕抓取（Qt 优先，dxcam 兜底）
│  │  └─ geometry.py                  # ★ 多屏 + DPI 坐标换算
│  ├─ ocr/
│  │  ├─ base.py                      # OcrEngine 协议 + TextBlock
│  │  ├─ rapidocr_engine.py           # PP-OCRv3 mobile + 空闲卸载 + 关线程自旋
│  │  ├─ winrt_engine.py              # 加载失败时的轻量兜底
│  │  └─ layout.py                    # ★ 行 → 段落重组
│  ├─ translate/
│  │  ├─ base.py                      # Translator 协议
│  │  ├─ llm.py                       # OpenAI 兼容 + SSE 流式
│  │  └─ prompts.py                   # ★ 方向感知的提示词模板
│  ├─ ui/
│  │  ├─ card.py                      # ★ 结果悬浮卡片
│  │  ├─ history_panel.py             # 历史记录浏览（离线可用）
│  │  ├─ tray.py                      # 托盘菜单 + 设置窗口
│  │  └─ theme.py
│  └─ utils/
│     ├─ log.py
│     └─ single_instance.py
└─ tests/
```

打星号的七个文件是**架构风险集中点**，它们的正确性决定了产品能不能用。其中 `geometry.py`（坐标）和 `rapidocr_engine.py`（内存）风险最高。

---

## 4. 核心接口

### 4.1 OCR 引擎

```python
# ocr/base.py
from dataclasses import dataclass
from typing import Protocol
import numpy as np

@dataclass(slots=True)
class TextBlock:
    text: str
    confidence: float
    box: tuple[tuple[float, float], ...]   # 4 点多边形，物理像素坐标
    order: int = 0                         # 阅读顺序

@dataclass(slots=True)
class OcrResult:
    blocks: list[TextBlock]
    text: str              # 已按阅读顺序 + 段落结构重组的纯文本
    elapsed_ms: int

class OcrEngine(Protocol):
    name: str
    def warmup(self) -> None: ...      # 启动时后台预加载模型
    def recognize(self, image: np.ndarray) -> OcrResult: ...
    def unload(self) -> None: ...      # 空闲时释放模型，把内存还给系统
    @property
    def is_ready(self) -> bool: ...    # 供托盘图标反馈状态
```

关键点：**只暴露 `recognize`，不暴露「检测」和「识别」两个阶段**。上层不该关心 Paddle 是两阶段还是 RapidOCR 是一阶段。想换引擎，写个新类就行。

`unload()` 是内存约束的直接产物，具体策略见 7.9。没有它，模型加载后就再也降不下来了。

### 4.2 翻译器

```python
# translate/base.py
from dataclasses import dataclass
from typing import Iterator, Protocol
from clicktrans.lang import Lang

@dataclass(slots=True)
class TranslateRequest:
    text: str
    source_lang: Lang          # 已在本地检测好，不让 LLM 去猜
    target_lang: Lang
    glossary: dict[str, str] | None = None

class Translator(Protocol):
    name: str
    # 返回增量片段，不是完整结果 —— 上层边收边渲染
    def translate(self, req: TranslateRequest) -> Iterator[str]: ...
```

`translate` 返回 **Iterator 而不是 str** 是刻意的。如果这里返回完整字符串，「实时」就没了，用户要盯着加载圈等 1-2 秒。

注意 `source_lang` 是 `Lang` 而不是 `str = "auto"`：语言检测被移到了 `lang.py` 本地完成（见 4.4）。让 LLM 自己猜源语言会多花 token 且不稳定，本地一行字符统计就能判准。

### 4.3 编排器

```python
# pipeline.py
class Pipeline(QObject):
    started   = pyqtSignal()
    partial   = pyqtSignal(str)        # 增量译文，可能被调用几十次
    finished  = pyqtSignal(object)     # TranslateResult
    failed    = pyqtSignal(str)

    def submit(self, pixmap: QPixmap, rect: QRect) -> None: ...
    def cancel(self) -> None: ...      # 新的框选到来时打断上一次
```

`submit` 是非阻塞的，且**后一次提交自动取消前一次**。用户连框三次不能出现三张卡片互相打架。

### 4.4 语言模块（中文 / 日语 / 英语 三语双向）

支持三语任意互译，共 **6 个定向语言对**。核心是「检测源语言 + 推导目标语言」两件事，全部在本地算，不发网络请求。

```python
# lang.py
from enum import StrEnum

class Lang(StrEnum):
    ZH = "zh"      # 中文
    JA = "ja"      # 日语
    EN = "en"      # 英语

def detect(text: str) -> Lang: ...
def resolve(src: Lang, preferred: Lang) -> tuple[Lang, Lang]: ...
```

**源语言检测规则**

字符集统计就够了，不需要上语言识别模型（那要多占几十 MB 内存）：

```python
# 优先级从高到低
# 1. 含平假名 (U+3040-309F) 或片假名 (U+30A0-30FF)  → JA，直接返回
# 2. 无假名，CJK 汉字占比 > 30%                      → ZH
# 3. 拉丁字母占比 > 60%                              → EN
# 4. 混合文本：按上述三类字符的加权占比投票           → 得票最高者
```

平假名/片假名是日语独有的，所以第 1 条判定几乎不会错。**已知局限**：日语全汉字短句（「株式会社」「確認事項」「以上」）没有假名，会被判成中文。这是纯字符统计的固有问题，不打算为它引入模型。

好在这个局限**不致命**：目标语言由用户偏好决定，检测错的只是源语言标记，方向不会错。它影响的仅仅是提示词里注入的约束段（比如本该用「です・ます」却按中译英处理）。历史记录里会提供手动纠正语言的入口。

**方向决策规则**

用户只需设一个「偏好语言」（默认中文），实际方向自动推导：

| 检测到的源语言 | 偏好 = 中文 | 偏好 = 日语 | 偏好 = 英语 |
|---|---|---|---|
| 中文 | → 英语 | → 日语 | → 英语 |
| 日语 | → 中文 | → 中文 | → 英语 |
| 英语 | → 中文 | → 日语 | → 中文 |

规则一句话概括：**源语言 ≠ 偏好语言 → 译成偏好语言；相等 → 译成回退语言**。中文的回退是英语，日语和英语的回退都是中文（这三个语种里中文用户最常见）。

框选时按住 `Shift` 可以循环切换偏好语言，不用进设置面板。

**方向感知的提示词**

6 个方向共用一套骨架，按语言对注入不同的约束段：

| 方向 | 额外注入的约束 |
|---|---|
| 中 → 日 | 默认「です・ます」敬体；专有名词保留汉字写法，不做音译 |
| 日 → 中 | 人名地名保留原文汉字；敬语层级用中文的自然表达还原，不堆砌「您」 |
| 中 → 英 | 保留技术术语原文（Transformer、Kubernetes 等）；成语意译不直译 |
| 英 → 中 | 长定语从句拆成短句；被动语态按中文习惯转主动 |
| 英 → 日 | 外来语写片假名；技术术语保留英文原词（日语技术文档的通行做法） |
| 日 → 英 | 敬语转成英文的礼貌层级（please / could you），不逐字对应 |

日语敬体风格由配置项 `ja_style = "polite" \| "plain"` 控制：正式文档用 polite，轻小说和技术笔记用 plain 更自然。

**所有方向共用的约束**

- 只输出译文，不要任何解释、前言、罗马音标注
- 严格保持原文的换行与段落结构
- 代码块、URL、邮箱、变量名、文件路径原样保留不译
- 原文里的数字、单位、型号不转换

---

## 5. 线程模型

只有三个线程，不要更多（预加载线程是一次性的，用完即退）：

| 线程 | 职责 | 约束 |
|---|---|---|
| **主线程** | Qt UI：覆盖窗、卡片渲染、托盘、历史面板 | 绝不阻塞。任何 > 16 ms 的操作都不许放这里 |
| **工作线程**（单个） | 预处理 + OCR 推理 + 网络请求 | 单线程串行 |
| **模型预加载线程** | 启动时加载 ONNX 模型 | 加载完即退出 |
| 主线程 QTimer（非线程） | 空闲 30 分钟 → 调 `engine.unload()` 释放模型 | 只释放模型权重，不退出进程 |

内存管理挂在这个 QTimer 上，而不是另起一个线程——`unload()` 只是释放引用，耗时不到 1 ms，不值得为它开线程。细节见 7.9。

**为什么工作线程只有一个？**
ONNX 推理会话不是线程安全的，多线程要么加锁（等于串行），要么每线程一份模型（内存翻倍）。单线程 + 队列是这里的最优解。OCR 单帧 200-500 ms 也远未到需要并行的程度。

网络请求也放在这个线程，用 `httpx` 同步流式（`iter_lines`），比引入 asyncio/qasync 简单得多，而且天然串行化了「OCR 分段送译」。

**跨线程通信只用 Qt 信号槽**，不要共享可变状态。`QPixmap` 不能跨线程，工作线程用的是 `numpy.ndarray`。

---

## 6. 数据流（运行时）

```
用户按 Alt+Q
  → 主线程：为每个 QScreen 创建全屏半透明覆盖窗
  → 覆盖窗显示「已抓好的整屏截图 + 灰化遮罩」
  → 用户拖拽出选框（橡皮筋 + 尺寸标注，Esc 取消）
  → 松手 → 从整屏截图裁出选区（物理像素）
  → 投递 ndarray 到工作线程，覆盖窗立即销毁
  → 工作线程：预处理 → RapidOCR → 文本重组
  → 分块送 LLM（SSE）→ 每收到 token 发 partial 信号
  → 主线程：卡片增量追加文本
  → 收完 → finished，卡片显示完整译文 + 操作按钮
```

**「先抓图再显示」这个顺序很关键。** 覆盖窗显示的是静态截图而不是透明玻璃，因为透明窗在很多场景（视频播放器、GPU 加速窗口、独占全屏）会抓不到底下的内容，而静态截图保证「所见即所得」，也顺带让遮罩效果好看。

---

## 7. 关键难点与对策

### 7.1 DPI 缩放（最容易出 bug 的地方）

Qt6 默认开启 HiDPI，这会制造两套坐标系：逻辑坐标和物理坐标。在 150% 缩放的屏幕上，框选 100 px 实际是 150 物理像素，裁剪时如果搞混，会截到错误区域或图片糊掉。

**统一策略**：整个 `capture` 模块内部一律使用**物理像素**，只在和 Qt 窗口系统交互的边界处做一次转换。

```python
# capture/geometry.py
def to_physical(logical: QPoint, dpr: float) -> QPoint:
    return QPoint(round(logical.x() * dpr), round(logical.y() * dpr))

app.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
    Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
)
```

一律从 `QScreen.devicePixelRatio()` 取倍率，**不要用 `QT_SCALE_FACTOR` 或猜**。多显示器 DPI 不同是常态（笔记本 150% + 外接显示器 100%）。

### 7.2 多显示器与负坐标

Windows 虚拟桌面的原点可能为**负值**（副屏设置在主屏左侧或上方时）。

```python
for screen in QGuiApplication.screens():
    geo = screen.geometry()        # 逻辑坐标，可能为负
    dpr = screen.devicePixelRatio()
    # 虚拟桌面上的物理偏移量 = geo.topLeft() * dpr
```

不要假设主屏在 (0,0)。覆盖窗要**每屏一个**（`setGeometry(screen.geometry())`），不要试图用一个跨屏大窗——跨屏窗口在混合 DPI 下会被系统拉伸，选框和实际内容对不上。

### 7.3 覆盖窗抢焦点

覆盖窗弹出时如果抢走焦点，被框选的原窗口会失焦（光标消失、下拉框收起），体验很糟。

```python
self.setWindowFlags(
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool                     # Tool 不占任务栏
)
self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
```

Windows 上还需要通过 `SetWindowLongPtrW` 加 `WS_EX_NOACTIVATE` 扩展样式，再用 `grabKeyboard()` 拿键盘（为了 Esc），两者配合才能「拿到键鼠但不激活」。

### 7.4 独占全屏 / GPU 加速窗口截不到

游戏、全屏视频、部分 Electron 应用走的是 DXGI 独占交换链，GDI 截图会返回纯黑。

**对策**：分层降级。
1. 首选 Qt 的 `QScreen.grabWindow(0)`（走 GDI，覆盖 95% 场景）
2. 检测到全黑（整图方差接近 0）→ 切 `dxcam`（DXGI 桌面复制 API）
3. 仍失败 → 卡片提示「该窗口不支持抓屏，可尝试窗口模式」

### 7.5 流式渲染闪烁

每收到一个 token 就调一次 `adjustSize()`，卡片会疯狂抖动，CPU 也吃不消。

```python
# ui/card.py —— 三件套
self.setFixedWidth(WIDTH)          # 宽度固定，不做自适应
self._buffer.append(chunk)         # token 先入缓冲区
self._flush_timer.start(50)        # 50 ms 合并刷新一次（限频）
# 高度只增不减：Y 轴向上生长时固定底边，避免卡片上下跳
```

再配合 `QTextOption` 的 word wrap，中文长句才不会出现逐字换行的抖动。

### 7.6 文本重组的质量

OCR 返回的是**行**，直接拼成一段会让 LLM 丢失结构，翻译出来的段落会乱。

```python
# ocr/layout.py
# 1. 按 box 的 y 中心聚类成行（阈值：行高 * 0.6）
# 2. 行内按 x 排序拼接
# 3. 行间距 > 行高的 1.5 倍 → 判定为段落分隔，插入 \n\n
# 4. 行尾是标点或下一行首字符非大写 → 同一段落，插空格而非换行
# 5. 代码特征检测：缩进一致 / 含 :: {} => → 该块标记为「不翻译」，原样输出
```

第 5 条特别重要——用户框选代码时，变量名、关键字不能被翻掉。

### 7.7 长文本超时

框选一整页文档（>3000 字符）时，单次 LLM 请求会超时且卡住卡片。

**对策**：按段落切分，逐段送译，前一段译完就渲染，同时预热下一段。这样视觉上是连续输出的流。单段超过 1500 字符才再切。

### 7.8 请求去抖与取消

- 拖拽过程中不发任何请求，只在**鼠标松开时**提交
- 同一选区重复框选 → 查 `history.json` 命中则直接返回
- 新框选到来 → `httpx` 的 response 主动 `close()`，丢弃 in-flight 请求
- 缓存与历史合并成同一份数据（见 7.10），不需要独立的缓存层，也就不需要 SQLite

### 7.9 内存占用与模型生命周期

内存是这个工具的核心约束，必须主动管，不能听天由命。

**占用拆解（实测值，`tools/check_ocr.py` 输出）**

| 状态 | 内存 | 说明 |
|---|---|---|
| 解释器 + numpy/onnxruntime/opencv 导入完 | ~59 MB | 基线，导入后不会再走 |
| OCR 模型已加载（活跃态） | ~119 MB | +60 MB：mobile 权重 + 推理时的中间张量 |
| 空闲卸载后 | ~107 MB | 只能收回 **12 MB** |
| 完整应用（Qt + 全部界面对象 + 一次识别） | ~146 MB | `tools/smoke_gui.py` 实测 |

**关于"卸载只收回 12 MB"——这是预期行为，不是 bug。** mobile 权重本身就小；
真正吃内存的是 opencv / onnxruntime 的运行时，一旦 import 就常驻，
主线程里 `gc.collect()` 也拿不回来。所以内存目标不该定成"卸载后回到空载"。

**三条压缩措施**

1. **用 mobile 模型而不是 server 模型。** PP-OCRv3 mobile 的 det + rec + cls 权重合计约 16 MB；
   server 版权重 200 MB 起步，运行时占用更是数倍。中日英混排场景 mobile 够用。

2. **空闲卸载。** 主线程 QTimer，30 分钟没有框选动作就调 `engine.unload()`。
   下次框选重新加载，多等 0.4-0.5 秒（加载实测 400-440 ms，比最初估的 1.5-2 s 快得多）。
   托盘图标同步反馈状态，避免用户以为程序卡死。

3. **关掉 onnxruntime 的线程自旋（不是内存池！）。**

   > ⚠️ 这里原本写的是「关 `enable_cpu_mem_arena`」，**实测证明那是错的**，
   > 已删除。保留这段是为了避免以后有人凭直觉再加回去。

   原方案的推理是：arena 分配器在 `del session` 后不把内存还给 OS，所以要关掉。
   实测（`tools/tune_ocr.py`，每组配置独立子进程）：

   | 配置 | 小选区 | 中选区 | 大选区 |
   |---|---|---|---|
   | A 预放大 2× + det 736/min | 1943 ms | 2042 ms | 3805 ms |
   | F 去掉预放大 + det 960/max（arena 默认开） | 192 ms | 678 ms | 1082 ms |
   | G 再关掉线程自旋 | **97 ms** | **233 ms** | **729 ms** |

   关掉 arena（A/F 之间的对照实验）只多花时间，释放量毫无变化——
   两种设置下卸载都只收回 8-13 MB。真正的大头是**线程自旋**：
   16 逻辑核上 onnxruntime 默认起 12+ 线程忙等，一次识别烧掉 25 秒 CPU
   才换来 2 秒墙钟，且机器一有负载就抖到 6 秒。关掉后线程没事就睡。

```python
def patched_init(self, path_or_bytes, sess_options=None, *args, **kwargs):
    if sess_options is None:
        sess_options = ort.SessionOptions()
    for entry in ("session.intra_op.allow_spinning",
                  "session.inter_op.allow_spinning"):
        sess_options.add_session_config_entry(entry, "0")
    sess_options.log_severity_level = 3
    return original_init(self, path_or_bytes, sess_options, *args, **kwargs)
```

   补丁打在 `ort.InferenceSession.__init__` 上，因为 RapidOCR 内部自己构造
   session，不给我们传 `SessionOptions` 的机会。

**不要做的事**

- 不要为了省内存改成「每次框选现加载模型」：冷加载 2-3 秒，直接毁掉实时体验
- 不要同时常驻两个 OCR 引擎：想留 WinRT 兜底，就做成「RapidOCR 加载失败时才初始化 WinRT」，而不是两个都预热

### 7.10 历史记录与离线模式

**一份数据，两个用途。** `history.json` 既是用户可见的历史记录，也是翻译缓存——重复框选同一段文字直接命中，省一次 API 调用也省一次等待。这样就不需要独立的缓存层（原本设计的 SQLite 可以整个砍掉）。

`~/.clicktrans/history.json`：

```json
{
  "version": 1,
  "items": [
    {
      "id": "20261001T110412-3f2a",
      "ts": "2026-10-01T11:04:12+08:00",
      "src_lang": "en",
      "tgt_lang": "zh",
      "engine": "deepseek-chat",
      "source_text": "...",
      "target_text": "...",
      "pinned": false
    }
  ]
}
```

**写入策略（三条都不能省）**

1. **原子写**：写 `history.json.tmp` → `os.replace()` 覆盖。直接覆写原文件的话，断电或强杀进程会留下半截 JSON，下次启动历史全丢。
2. **限流合并**：1 秒内的多次写入合并成一次，避免连续框选时频繁磁盘 IO。
3. **滚动淘汰**：超过 500 条删最旧的，但 `pinned = true` 的跳过不删。500 条大约是一两周的使用量，文件通常在 1 MB 以内，全量读进内存也没有压力。

**离线模式**

没有网络时翻译必然失败。这时**不弹错误框、不静默失败**，按顺序做三件事：

1. **先查历史。** 有没有完全相同的原文 + 相同语言对？有就直接显示，标题栏标注「来自历史记录」。这是最实用的一条——看同一份文档、同一段代码时，重复框选的概率很高。
2. **没命中就显示原文。** 卡片展示本地 OCR 的结果，附一句「当前无网络，已识别文字，翻译不可用」，并把这条以「未翻译」状态暂存进历史。
3. **网络恢复后后台补译。** 把「未翻译」的记录逐条补上译文，不打扰用户。

托盘主菜单的第一项是「历史记录」，**离线时永远可点**——这是离线状态下唯一有意义的入口。

**一点隐私提醒**：`history.json` 是明文的（要能直接看、直接手改），框选过敏感内容后请手动清理。托盘菜单提供「清空历史」。

---

## 8. 性能预算

从**鼠标松开**那一刻开始计时。本机（16 逻辑核，1.0x DPI）实测：

| 阶段 | 目标 | 累计 |
|---|---|---|
| 裁剪位图 + QImage→ndarray | 10 ms | 10 ms |
| 图像预处理（`fit_within`，通常不放大） | 5 ms | 15 ms |
| RapidOCR mobile 推理（模型已热）· 小选区 | 97 ms | 112 ms |
| RapidOCR mobile 推理（模型已热）· 中选区 | 233 ms | 248 ms |
| RapidOCR mobile 推理（模型已热）· 大选区（接近 4M 像素上限） | 729 ms | 744 ms |
| 文本重组 | 10 ms | ~750 ms |
| LLM 首 token（TTFB） | 500 ms | **~1250 ms** |
| 全文译完（200 字符） | 800 ms | ~2050 ms |
| ─ | | |
| 冷启动：进程刚起来 | 模型加载 400-450 ms | 只在启动时发生一次 |
| 空闲卸载后首次框选 | 模型加载 400-450 ms | 30 分钟无操作才触发 |

最初估计的「推理 350 ms / 加载 1.5-2 s」偏保守：加载实测只要 400-450 ms，
而推理在小/中选区比估计快、在大选区比估计慢。**真正决定体验的是选区大小**，
所以 `max_pixels = 4_000_000` 这个上限比调模型更值得关注。

**两个必要的预热动作**：
1. 模型预加载——进程启动时就在后台线程加载 ONNX，完成后托盘图标变实心
2. 连接预热——启动后对 LLM 端点发一个 1-token 的空请求，把 TLS 握手提前做掉，能省 100-200 ms

**模型没热好时不要静默卡住。** 两种情况都要在卡片上明说：进程刚启动显示「OCR 模型加载中（约 2 秒）」；空闲卸载后首次框选显示「正在唤醒模型…」。`engine.is_ready` 就是给这个判断用的。

---

## 9. 配置与安全

`~/.clicktrans/config.toml`

```toml
[general]
hotkey = "alt+q"
preferred_lang = "zh"       # 偏好语言 zh | ja | en，实际翻译方向由它推导（见 4.4）
launch_at_startup = true
single_instance = true

[ocr]
engine = "rapidocr"
model = "mobile"            # mobile | server（server 精度略高，内存多 200MB+）
upscale = 2.0
min_confidence = 0.6
unload_after_idle_min = 30  # 0 = 永不卸载

[translate]
provider = "openai_compatible"
base_url = "https://api.deepseek.com/v1"
model = "deepseek-chat"
stream = true
timeout_s = 20
proxy = ""                  # 留空则读系统代理
ja_style = "polite"         # 日语译文风格 polite(です・ます) | plain(だ・である)

[history]
path = "~/.clicktrans/history.json"
limit = 500                 # 滚动保留条数，置顶项不计入
dedupe = true               # 相同原文 + 语言对命中时复用并置顶

[ui]
font_size = 15
card_width = 420
card_opacity = 0.96
auto_hide_ms = 0            # 0 = 不自动隐藏
theme = "follow_system"

[glossary]
# 术语表，翻译时注入提示词，对所有语言方向生效
# "Transformer" = "Transformer"
```

**API Key 的安全存储**：绝不写明文进 TOML。用 Windows DPAPI 按当前用户加密：

```python
# config.py
import win32crypt

def save_key(key: str) -> bytes:
    # CryptProtectData 绑定当前用户账户，其他用户/其他机器无法解密
    return win32crypt.CryptProtectData(key.encode(), None, None, None, None, 0)

def load_key(blob: bytes) -> str:
    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode()
```

密文存 `~/.clicktrans/secret.bin`。这样即使 config 被同步到网盘或提交进 git，Key 也不会泄露。

---

## 10. 错误处理与降级链

| 故障 | 表现 | 处理 |
|---|---|---|
| OCR 未识别到文字 | 选区是纯图/空白 | 卡片显示「未识别到文字」，1.5 s 后自动消失，不弹错误框 |
| OCR 置信度全低于阈值 | 文字太小或太糊 | 自动放大 3× 重试一次；仍失败则显示原文 + 「识别质量较低」提示 |
| **完全无网络** | 请求必然失败 | ① 先查 history.json 是否命中同一原文 → 命中直接显示历史译文；② 未命中则显示 OCR 原文 + 「翻译不可用」并暂存；③ 网络恢复后后台补译。详见 7.10 |
| LLM 请求超时 | 有网但接口不通 | 卡片内显示原文 + 重试按钮；连续 3 次失败自动降级为非流式请求 |
| LLM 返回异常格式 | 夹带解释性文字 | 提示词已约束只输出译文；仍在则做一次后处理剥离 |
| 源语言检测错误 | 日语全汉字短句被判成中文 | 只影响提示词语气，不影响方向（目标语言由偏好决定）；历史面板提供手动纠正语言 |
| 目标语言 = 源语言 | 检测结果与偏好撞车 | 按 4.4 的回退规则自动换向，不报错 |
| 覆盖窗创建失败 | 极少数多屏异常 | 降级为「固定区域模式」：记住上次选区，热键直接翻译该区域 |

**降级顺序**：LLM 流式 → LLM 非流式 → 历史记录命中 → 仅显示 OCR 原文（至少让用户能复制粘贴到别处）。**任何情况下都不许出现「什么都没发生」**。

---

## 11. 打包与分发

```python
# build/clicktrans.spec 关键部分
a = Analysis(
    ['clicktrans/__main__.py'],
    datas=[
        ('rapidocr/models/*.onnx', 'rapidocr/models'),   # 模型必须显式带上
        ('clicktrans/assets/*', 'clicktrans/assets'),
    ],
    hiddenimports=['onnxruntime', 'win32crypt', 'PIL._tkinter_finder'],
    excludes=['tkinter', 'matplotlib', 'pandas'],        # 砍体积
)
exe = EXE(..., console=False, upx=True)
```

预期产物体积：**90-140 MB**（ONNX Runtime + Qt 占大头，mobile 模型只占 16 MB）。
换用 mobile 模型顺带把打包体积也砍了一半——这是内存约束的额外收益。

其他注意项：
- `--noconsole` 后 `print` 会抛异常，所有输出走 `logging`
- 开机自启用注册表 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，不需要管理员权限
- 首次运行 Windows Defender 可能拦截自解压，建议提示用户加信任

---

## 12. 界面交互细节

**框选覆盖窗**
- 十字光标，拖动橡皮筋，右下角显示选区尺寸（如 `420 × 180`）
- 底色遮罩 `rgba(0,0,0,0.35)`，选区内完全透亮
- `Esc` 取消
- `Shift` 按住时循环切换偏好语言（中 → 日 → 英），左上角实时显示「偏好语言：日本語」，松手后生效
- 可选：光标附近显示局部放大镜（3×3 像素块），对齐文字边界时很有用

**结果卡片**
- 位置优先级：选区右侧 → 左侧 → 下方 → 上方，贴屏幕边缘时自动夹紧
- 标题栏显示方向徽标，如 `EN → 中文`、`日本語 → 中文`，让用户一眼确认方向对不对
- 底部工具栏：`原文/译文` 切换（`Tab`）· 复制（`Ctrl+C`）· 重新翻译 · 切换方向 · 关闭（`Esc`）
- 命中历史记录时，标题栏加一个「历史」小标记，和实时翻译区分开
- 鼠标移出后 8 秒淡出（可配置），鼠标悬停则保持
- 卡片本身可拖动，位置在当前会话内记忆

**历史记录面板（离线可用）**
- 数据源就是 `history.json`，全量在内存里，搜索是纯字符串匹配，无延迟
- 列表项：`时间 · EN → 中文 · 原文摘要 · 译文摘要`
- 搜索框同时匹配原文和译文，可按语言方向筛选
- 每项操作：复制译文 / 复制原文 / 重新框译 / 置顶 / 删除
- 顶部显示 `已用 128 / 500 条`，让用户对容量有预期
- 置顶项（`pinned`）不参与滚动淘汰

**托盘菜单**
- 启用/停用翻译
- **历史记录**（永远可点，离线也能用）
- 偏好语言（中文 / 日本語 / English）
- OCR 模型（mobile / server）与空闲卸载策略
- 查看当前内存占用 / 手动卸载模型
- 打开配置文件 / 查看日志 / 关于 / 退出

---

## 13. 开发里程碑

| 阶段 | 交付 | 验收标准 |
|---|---|---|
| **M1 · 骨架** | 热键 → 覆盖窗 → 框选 → 存 PNG | 单屏/多屏/不同 DPI 下裁剪区域都精确对齐 |
| **M2 · 识别** | 接 RapidOCR mobile，卡片显示原文 | 中文、日语、英语混排段落识别正确，行序不乱，识别耗时 < 600 ms |
| **M3 · 语言与翻译** | `lang.py` 三语检测 + LLM 流式，端到端可用 | 6 个方向的译文都通顺；松手 → 首字上屏 < 1.2 s；格式保留原文换行 |
| **M4 · 历史与内存** | history.json 读写、历史面板、空闲卸载 | 离线可查历史；重复原文命中历史瞬时返回；空闲 30 分钟后内存降到 130 MB 以内 |
| **M5 · 打磨与分发** | 术语表、降级链、设置面板、PyInstaller 单 exe | 断网不崩；干净机器上双击即用，无需装 Python |

两条顺序约束：

1. **M1 是所有风险的集中点**，先把坐标换算彻底调通再往上叠功能。如果多屏下选区对不齐，后面所有体验都是错的。
2. **M4 的空闲卸载要单独验收**，用任务管理器盯着实际内存曲线确认降下去了。验收标准是**能收回 8-13 MB**（mobile 权重就那么大），而不是"回到空载"——opencv / onnxruntime 的运行时一旦导入就常驻，别为这个去关 arena，那只会让推理慢一倍。

---

## 14. 后续可扩展方向

- **连续模式**：热键 + 拖拽时进入「跟随翻译」，对固定区域定时抓帧，帧间差分去重后增量翻译（看视频字幕、直播用）
- **多引擎并发投票**：OCR 用 RapidOCR + WinRT 双跑，取置信度高的一方
- **扩充语种**：`lang.py` 的检测规则和 `prompts.py` 的方向表都是数据驱动的，加韩语、法语等只需增加表项，不动架构
- **术语库自动累积**：用户手动修正过的译法自动进术语表，越用越准
- **划词模式**：不框选，双击选中文本后直接翻译（需要 UI Automation，覆盖面不如截图方案）
- **历史导出**：把 history.json 导成 Markdown / Anki 卡片，看外文文档时顺手做成复习材料

> 本地离线翻译（ARGOS / opus-mt）已明确排除在路线图外——几百 MB 的内存代价换来的收益不划算。
