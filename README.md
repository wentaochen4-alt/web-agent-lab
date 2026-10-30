# web-agent-lab

一个能自己操作浏览器的 AI Agent：你用一句话描述任务，它打开网页、看截图、一步步点击和输入，做完告诉你结果。遇到提交、购买这类关键操作会先停下来问你。

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
├── run_local.py            Agent 核心：白名单、轨迹日志、卡住检测、计划、命令行入口
├── cache_replay.py         缓存回放：录制、回放、自愈
├── app/                    网页版：server.py（FastAPI 后端）+ static/（界面）
├── sites/meeting/          本地测试网站：会议室预订
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

# 本地测试站（另开一个终端）
python3 -m http.server 8770 --bind 127.0.0.1 --directory sites/meeting

# 网页版
computer-use-preview/.venv/bin/python -m uvicorn app.server:app --port 8780
```

然后打开 http://127.0.0.1:8780 。更多用法（命令行版、参数、缓存回放）见 [RUN.md](RUN.md)。

## 部署到服务器

`deploy/` 里有两个 systemd 配置：测试站和网页版都只监听本机端口，再由反向代理（比如 Caddy）加上密码对外提供。服务器上会打开 `BLOCK_PRIVATE_NETWORK`，Agent 无法访问服务器本机和内网的其他服务。服务器没有屏幕，浏览器以无窗口模式运行，所以“卡住时人工接管”在服务器上不可用。
