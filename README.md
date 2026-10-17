# DG-Control Hub

郊狼（DG-LAB）多设备分布式综合控制中台。Windows 单文件绿色免安装（内嵌 Web 控制服务 + WebView2 桌面端），手机浏览器扫码即得与 PC 完全一致的双端同权操控。

> 仓库地址：<https://github.com/chenofchina/DG-Control-Hub>

## 功能特性

- **多设备卡槽**：动态增删卡槽，DG-LAB 官方 App 扫码接入（官方 V3 Socket 协议：引导包 / DGLAB / 200 绑定时序）
- **双通道独立调控**：A/B 通道滑块与 −5 −1 +1 +5 步进，App 端本地调整实时反向同步
- **自动化玩法引擎**：轮盘随机抽奖（可单次模式）、渐进阶梯爬升、呼吸 / 潮汐 / 连击 / 乱数四套波形，单设备或全员下发
- **双层安全架构**：全局安全上限（默认 50，硬上限 200），服务端下发前统一 `min(请求值, 上限)` 硬封顶
- **紧急制动**：顶栏红色急停 / 底部浮动急停 / 键盘空格键，一键清空全部玩法并将所有通道归零
- **Cloudflare Quick Tunnel**：一键公网穿透，外网访问控制台与 App 接入（wss）
- **Material Design 3**：四套主题色系 × 明暗模式、毛玻璃卡片、四种自定义壁纸（纯色 / 必应每日 / 外链 / 本地上传）
- **PWA**：手机端「添加到主屏幕」全屏运行；多管理端同时在线，全量状态实时同步
- **事件日志与诊断**：界面内滚动事件日志；`app.log` 落盘完整连接握手记录
- **用户可编辑配置**：主程序同目录 `config.json`（端口 / 安全上限 / 玩法默认值 / 隧道自启 / 窗口尺寸等）
- **OOBE 首次运行向导**：7 步引导（协议确认 → 安全 → 网络 → 上手 → 外观 → 完成）

## 快速开始

### 方式一：直接使用（推荐）

运行 `build.bat` 或到 [Releases](https://github.com/chenofchina/DG-Control-Hub/releases) 下载 `DG-Control-Hub.exe`，双击即用：

1. 首次启动完成 OOBE 向导
2. 界面中「添加设备卡槽」生成二维码
3. DG-LAB 官方 App →「Socket 控制 / 第三方连接」扫码
4. 手机浏览器扫码「手机端入口」获得同权控制端

### 方式二：源码运行

```bash
pip install -r requirements.txt
python app.py            # 桌面模式
python app.py --server-only   # 纯服务模式（无 GUI）
```

### 源码打包

```bash
build.bat
# 等价于：
pyinstaller --noconsole --windowed --onefile --clean --icon="icon.ico" ^
    --name="DG-Control-Hub" --add-data="web;web" --collect-submodules webview app.py
```

## 通信接口

| 通道 | 路径 | 说明 |
| --- | --- | --- |
| 管理端 | `ws://host:port/ws/admin` | 桌面 / 手机浏览器，JSON 全量状态同步 |
| 官方 App | `ws://host:port/`、`/{clientId}`、`/dglab/ws/{targetId}` | 官方 V3 Socket 协议桥接 |

## 目录结构

```text
DG-Control-Hub/
├── app.py            # 主入口（FastAPI + Uvicorn 服务线程 + pywebview GUI）
├── web/              # 前端静态资源（单文件 index.html，Vue3 + Tailwind + M3）
├── icon.ico          # 应用图标
├── requirements.txt  # 依赖声明
└── build.bat         # 一键 PyInstaller 打包
```

## 安全与免责声明

- 本软件与 DG-LAB（郊狼 / Dungeon Lab）官方**无任何隶属或授权关系**，非官方应用，仅通过官方 App 的第三方 WebSocket 接口规范桥接
- 佩戴心脏起搏器等植入设备者、心脑血管疾病患者、癫痫患者、孕妇及未成年人**禁止使用**；首次使用请从最低强度开始
- 操控他人设备必须事先获得对方明确、知情、可随时撤回的同意
- 启用 Cloudflare 隧道时流量将经由 Cloudflare 网络中转；未启用时全部通信仅在局域网内
- 完整《用户协议与隐私政策》见软件内「用户协议」入口（顶栏文档图标）

开源许可详见仓库 LICENSE 文件。
