# Brush Bridge 笔刷桥

**在 Photoshop、Clip Studio Paint (CSP)、Procreate、SAI2、Krita、GIMP 之间搬运笔刷的桌面工具。**
面向画师设计：不用命令行，勾选笔刷 → 选择目标软件 → 点「开始导入」。

[English](#english) · [下载](https://github.com/CanderFlower/brush-bridge/releases/latest) · MIT License

---

## 能做什么

| 功能 | 说明 |
|---|---|
| 💻 检测软件和版本 | 自动找到电脑上安装的绘画软件、版本号、是否正在运行、笔刷保存位置 |
| 🖌 直接浏览已装的笔刷 | 直接读取 Photoshop 画笔库、CSP 辅助工具、SAI2 自定义工具、Krita 资源包、GIMP 笔刷，带缩略图、搜索，并按软件里原本的分组（如 PS 的多层画笔组、CSP 的「工具 → 子工具组」）显示成文件夹树 |
| 📄 打开外部笔刷文件 | 把 `.abr` `.sut` `.brush` `.brushset` `.bundle` `.kpp` `.gbr` `.gih` 或 PNG 图片拖进窗口即可 |
| 🚚 一键导入 | 选好目标软件，自动转换格式并导入；做不到全自动的软件会给出明确的操作提示 |
| 🔧 修复导入失败 | 例如新版 Photoshop 导出的 ABR v10 在 CSP 里报错，可一键转成通用的 v6.2 格式（无损） |

## 支持情况

| 软件 | 读取已安装笔刷 | 打开文件 | 导出文件 | 自动导入 |
|---|---|---|---|---|
| Photoshop | ✅ 画笔库 Brushes.psp（含分组） | ✅ .abr v1/v2/v6/v10 | ✅ .abr v6.2 | ✅ 自动发送给 Photoshop 载入 |
| Clip Studio Paint | ✅ 辅助工具 | ✅ .sut | ✅ .sut | ⚠️ 实验性（需关闭 CSP，自动备份） |
| SAI2 | ✅ 自定义工具 | ✅ | ✅ | ✅ 写入 SAI2 设置（需关闭 SAI2，自动备份） |
| Procreate（iPad） | — 无法在电脑上检测 | ✅ .brush / .brushset | ✅ .brushset | 📱 生成文件，传到 iPad 点开即可 |
| Krita | ✅ 资源包 | ✅ .bundle / .kpp | ✅ .bundle | ✅ 放入资源文件夹，重启 Krita 生效 |
| GIMP | ✅ 用户笔刷 | ✅ .gbr / .gih / .vbr | ✅ .gbr | ✅ 放入笔刷文件夹，刷新即可 |
| 图片 | — | ✅ PNG/JPG 等作为笔尖 | ✅ PNG | — |

> 同一软件内的「原样搬运」（例如 Photoshop 画笔库 → .abr 文件 → CSP）会保留原始数据，不损失任何参数。
> 不同软件之间的转换会尽量映射笔尖、间距、压感、散布、纹理、双重画笔、颜色动态、混色等参数；
> 无法对应的效果（例如 PS 湿边、Procreate 湿边/烧边）会在结果里明确提示。
> 转到 CSP 时，自定义笔尖、纸纹和双重画笔会以原始分辨率生成为 CSP 素材，直接内嵌在 .sut 里。

## 使用方法

1. 从 [Releases](https://github.com/CanderFlower/brush-bridge/releases/latest) 下载 `BrushBridge.exe`，双击运行（无需安装）。
2. **① 选择来源**：左侧点一个软件，或把笔刷文件拖进窗口。
3. **② 勾选笔刷**：可以搜索、按分组筛选、全选。
4. **③ 导入到**：选目标软件（或「导出为文件」），点 **开始导入**。

**常见问题**

- *CSP 提示「此文件无法添加到工具组面板」*：要把 .abr / .sut 拖到 **辅助工具** 面板，而不是工具面板。
- *CSP 提示无法读取 .abr*：多半是新版 Photoshop 的 v10 格式。到「🔧 修复导入问题」页拖入文件，点「转换为通用的 v6.2 格式」。
- *直接写入软件设置会不会弄坏？* 写入前会自动备份到同目录的 `brush-bridge-backups` 文件夹，并要求先关闭目标软件。

## 从源码运行 / 开发

```bash
git clone https://github.com/CanderFlower/brush-bridge
cd brush-bridge
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
brush-bridge            # 图形界面
brush-bridge-cli apps   # 命令行：检测软件
python -m pytest        # 测试
python scripts/build_exe.py   # 打包 dist/BrushBridge.exe
```

架构：`brush_bridge/core/model.py` 定义与软件无关的笔刷模型；`formats/` 每种文件格式一个编解码器，`apps/` 每个软件一个适配器（检测、列出、安装）。新增软件只需添加一个插件文件。各格式的逆向笔记见 [docs/formats](docs/formats)。

欢迎提交 Issue 附上无法导入的笔刷文件（请确认你有权分享）。

## 许可

MIT。本仓库不包含任何商业笔刷；测试用的公开笔刷在测试时下载，来源与许可见 [docs/THIRD_PARTY.md](docs/THIRD_PARTY.md)。
Photoshop、Clip Studio Paint、Procreate、PaintTool SAI、Krita、GIMP 是其各自所有者的商标，本项目与它们无关。

---

## English

**Brush Bridge** is a friendly desktop app that moves brushes between Photoshop, Clip Studio Paint, Procreate, PaintTool SAI 2, Krita and GIMP.

- Detects installed painting apps and their versions.
- Browses the brushes already installed in each app (thumbnails, groups, search).
- Opens external brush files (`.abr`, `.sut`, `.brush`, `.brushset`, `.bundle`, `.kpp`, `.gbr`, `.gih`, images) by drag & drop.
- One-click conversion + installation into the target app (with automatic backups when writing app settings).
- Diagnoses files that an app refuses to import and repairs them (e.g. Photoshop 2020+ ABR v10 → v6.2 for Clip Studio Paint).

Download `BrushBridge.exe` from Releases, or run from source with `pip install -e .` then `brush-bridge`. MIT licensed.
