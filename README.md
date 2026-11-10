# web-agent-lab

一个通用的浏览器操作 AI Agent：你用一句话描述任务，它打开网页、看截图、一步步点击和输入，做完告诉你结果。遇到提交、购买这类关键操作会先停下来问你。

它不绑定某个网站或某类任务：搜索、浏览、整理信息、填表、后台操作都可以，也可以跨页面、跨网站、在多个标签页之间来回操作。仓库里的会议室预订站和模拟商城（`sites/`）只是方便本地试用的示例场景，购物只是其中一种用法。

底层模型是 Gemini（computer use 模式，经 Vertex AI 调用），浏览器由 Playwright 控制。在 Google 官方参考实现 [computer-use-preview](https://github.com/google/computer-use-preview) 的基础上，补上了做成一个可用产品需要的部分。

## 做了什么

| 功能 | 说明 |
|---|---|
| 网页版 | 下任务、实时看每一步的截图和动作、确认关键操作、回看历史记录 |
| 可编辑计划 | 开始前先出计划给你过目，可以改；执行时逐项打勾 |
| 缓存回放 | 同样的任务做成功一次后，记下每一步点的是哪个页面元素，下次直接回放，不调用模型 |
| 自愈 | 网站改版导致回放失败时，自动让 Agent 重做并更新缓存 |
| 安全 | 关键操作要确认（回放时也一样）；网站白名单；部署在服务器上时禁止访问内网 |
| 卡住处理 | 连续几步页面没变化就提醒模型换办法，仍然不行就请人接管或停止 |
| 接管 | 随时暂停；等你登录、卡住或暂停时，可以直接在网页版的实时画面上点击、滚动、输入，处理完交还给它，还能留一句话告诉它接下来怎么做 |
| 扫码登录 | 遇到登录页会停下来请你本人登录：二维码单独放大显示、过期自动刷新；查到明确的已登录证据才自动继续，否则等你确认。登录状态只存在本机。它不会输入账号密码，也不处理验证码 |
| 多标签页 | 网页打开的新标签页会保留，模型可以查看、切换、关闭标签页 |
| 轨迹日志 | 每一步的截图、动作、模型思考都存下来，可以回放和分析 |

## 效果

同一个“预订会议室”任务（本地测试站，填表并提交）：

| 方式 | 耗时 |
|---|---|
| 官方参考实现的默认设置 | 约 80 秒 |
| 降低思考深度、首轮附上页面截图 | 约 46 秒 |
| **缓存回放** | **约 5 秒** |

## 目录

```
web-agent-lab/
├── computer-use-preview/   Google 官方参考实现（git 子模块，未改动）
├── agent/                  Agent 核心（通用，不绑定具体网站）
│   ├── core.py             主循环、计划、卡住检测、请人接管
│   ├── sandbox.py          浏览器访问约束：白名单、禁内网、多标签页
│   ├── tools.py            给模型的自定义工具
│   ├── login.py            按网站扩展的登录处理：二维码、过期刷新、已登录证据
│   ├── trajectory.py       轨迹日志
│   └── upstream.py         接入官方参考实现
├── run_local.py            命令行入口
├── cache_replay.py         缓存回放：录制、回放、自愈
├── app/                    网页版
│   ├── server.py           FastAPI 后端：接口和调用协调
│   ├── state.py            一次运行的内存状态
│   ├── takeover.py         接管输入和坐标转换
│   └── static/             界面：原生 ES 模块，无框架、无构建步骤
├── sites/meeting/          示例场景：会议室预订（本地测试站）
├── sites/shop/             示例场景：模拟商城（本地测试站，假支付）
├── deploy/                 服务器部署用的 systemd 配置
└── RUN.md                  详细运行说明
```

## 快速开始

需要一个开通了 Vertex AI 的 Google Cloud 项目，并且本机已经用 `gcloud auth application-default login` 登录。

```bash
git clone --recursive https://github.com/wentaochen4-alt/web-agent-lab.git
cd web-agent-lab
python3 -m venv computer-use-preview/.venv
computer-use-preview/.venv/bin/pip install -r requirements.txt
computer-use-preview/.venv/bin/playwright install chromium
printf 'USE_VERTEXAI=true\nVERTEXAI_PROJECT=你的项目ID\nVERTEXAI_LOCATION=global\n' > computer-use-preview/.env

# 本地示例站（可选，另开一个终端；只在试用首页的“预订会议室”示例时需要）
python3 -m http.server 8770 --bind 127.0.0.1 --directory sites/meeting

# 网页版
computer-use-preview/.venv/bin/python -m uvicorn app.server:app --port 8780
```

然后打开 http://127.0.0.1:8780 ，在“选项”里把起始网址换成你想让它操作的网站即可。更多用法（命令行版、参数、接管、扫码登录、缓存回放）见 [RUN.md](RUN.md)。

## 部署到服务器

`deploy/` 里有三个 systemd 配置：两个示例站和网页版都只监听本机端口，再由反向代理（比如 Caddy）加上密码对外提供。服务器上会打开 `BLOCK_PRIVATE_NETWORK`，Agent 无法访问服务器本机和内网的其他服务。服务器没有屏幕，浏览器以无窗口模式运行，接管和扫码登录都通过网页版的实时画面进行。

官方参考实现固定的 Playwright 1.55 不支持 Ubuntu 26.04，在这类系统上装完依赖后需要再升级一次：

```bash
computer-use-preview/.venv/bin/pip install -U playwright
sudo computer-use-preview/.venv/bin/playwright install-deps chromium
computer-use-preview/.venv/bin/playwright install chromium
```

服务器上调用 Gemini 用的是服务器用户自己的应用默认凭证：

```bash
gcloud auth application-default login --no-launch-browser
gcloud auth application-default set-quota-project 你的项目ID
```
