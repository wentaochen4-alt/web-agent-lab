# web-agent-lab 运行说明

一个最小的“Agent 控制浏览器”实验环境：Gemini（computer use 模式，走 Vertex AI）看网页截图、决定动作，Playwright 负责在 Chromium 里执行。

## 目录结构

```
web-agent-lab/
├── computer-use-preview/   Google 官方参考实现（git clone，原样未改）
│   ├── .venv/              Python 3.12 环境（google-genai、playwright 等）
│   └── .env                Vertex 配置：项目、global 区域（已被 .gitignore 忽略）
├── run_local.py            启动脚本：在官方代码外加了白名单、日志、最大步数
├── sites/meeting/          本地测试网站：会议室预订（3 个静态页面）
└── runs/<时间>/            每次运行的记录
    ├── step_XX.png         每一步的截图
    ├── trajectory.jsonl    每一步的动作、网址（一行一个 JSON）
    └── result.txt          最终状态和 Agent 的回答
```

## 网页版（推荐）

```bash
cd ~/Desktop/web-agent-lab
computer-use-preview/.venv/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port 8780
```

然后打开 http://127.0.0.1:8780 。也可以在 Claude 里启动 `web-agent-app` 预览。用本地测试站的任务时，测试站（`web-agent-meeting`，端口 8770）也要开着。

- 左边：历史记录，点开可以回放每一次运行。
- 中间：你的任务、每一步的动作（点“查看思考过程”能看模型怎么想的）、需要确认或卡住时的卡片、最后的回答。
- 右边：Agent 看到的画面，蓝圈是它要点的位置；下面的缩略图可以逐张回看。
- 输入框左下角“选项”：起始网址、模型、思考深度、自动确认。
- 网页版**不限制网站**，任何网址都能去；提交、购买这类关键操作仍然会弹卡片问你（除非打开了自动确认）。命令行版默认还有白名单，加 `--allow_all` 可以取消。
- 运行中，发送按钮会变成停止按钮。
- **可编辑计划**（选项里“先拟定计划”，默认打开）：发出任务后先生成计划给你过目，可以改写、删除、添加步骤（在某一步按回车会在下面插入新的一步），点“按这个计划开始”才执行。执行时计划实时打勾；运行中点“修改计划”可以改剩下的步骤，下一步生效。
- 界面风格参考 AKQA：白底黑字、Instrument Serif 衬线大标题、编号清单、胶囊按钮。

代码在 `app/`：`server.py`（后端，复用 run_local.py 的 Agent）和 `static/`（界面）。

## 命令行版运行步骤

1. 启动测试网站（任选一种）：
   - 在 Claude 里让它启动 `web-agent-meeting` 预览；或者
   - 终端运行：`python3 -m http.server 8770 --bind 127.0.0.1 --directory ~/Desktop/web-agent-lab/sites/meeting`

2. 运行 Agent：

   ```bash
   cd ~/Desktop/web-agent-lab
   computer-use-preview/.venv/bin/python run_local.py --highlight_mouse --auto_confirm \
     --query "这是一个会议室预订网站。请预订 A 会议室：日期 2026-11-05，下午时段，预订人填写“测试用户”。提交后，把页面上显示的预约编号告诉我。"
   ```

   会弹出一个 Chromium 窗口，红圈表示 Agent 点击的位置。

3. 核对结果：上面这个任务的正确预约编号是 `BK-419247`（编号由“会议室|日期|时段|预订人”算出来，输入一样编号就一样）。

## 常用参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--query` | 必填 | 交给 Agent 的任务 |
| `--model` | `gemini-3.6-flash` | 也可以用 `gemini-3.5-flash`、`gemini-3.5-flash-lite`（更便宜） |
| `--max_steps` | 25 | 超过就强制停止，防止跑飞、烧钱 |
| `--max_minutes` | 10 | 整次运行的时长上限 |
| `--thinking` | `low` | 每一步的思考深度：minimal / low / medium / high，越浅越快 |

**卡住处理**：连续 3 步页面毫无变化（或重复同一个动作）算卡住。第一次卡住会提醒模型换办法；还卡住的话，在终端里运行时会暂停，请你在浏览器窗口里手动处理（登录、关弹窗等），按回车继续，输入 q 结束；非交互运行则直接停止（状态 `STUCK`）。

结束状态：`COMPLETE` 完成 / `GAVE_UP` 模型没给答案就结束了 / `STUCK` 卡住 / `TIMEOUT` 超时 / `MAX_STEPS` 超步数 / `SAFETY_STOP` 需要确认被停下。
| `--initial_url` | `http://127.0.0.1:8770/index.html` | 必须在白名单内 |
| `--allow` | 空 | 额外放行的外部域名（含子域名），例如 `--allow wikipedia.org wikimedia.org`。白名单外的跳转会被拦下，Agent 留在原页面 |
| `--highlight_mouse` | 关 | 在点击位置画红圈 |
| `--auto_confirm` | 关 | Gemini 认为“提交预订”这类操作不可撤销，会要求用户确认；不加这个参数就停在那一步，加了就自动放行（只适合本地测试站） |

不想弹窗口（无头模式）：在命令前加 `PLAYWRIGHT_HEADLESS=1`。注意官方代码里这个变量只要非空就是无头模式，写 `false` 也会变成无头。

## 安全设置

- **网络白名单**：浏览器只能访问 `127.0.0.1:8770` / `localhost:8770`，其他请求全部拦截，并记进日志。
- **全新浏览器环境**：不带任何登录状态，不会碰到你真实的账号。
- **需要确认就停**：模型返回 `require_confirmation` 时默认直接终止（状态记为 `SAFETY_STOP`），只有加了 `--auto_confirm` 才放行。
- **最大步数**：默认 25 步。

## 已知问题

- 官方 README 提到：Playwright 在某些系统上截不到原生下拉框（`<select>`），所以测试站里没有用下拉框。
- 每一步都会把截图发给 Gemini，费用从 Vertex 赠金里扣，一个简单任务大约几美分。
