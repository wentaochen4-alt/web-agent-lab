# web-agent-lab 运行说明

一个通用的浏览器操作 Agent：Gemini（computer use 模式，走 Vertex AI）看网页截图、决定动作，Playwright 负责在 Chromium 里执行。它不限于某个网站或某类任务，起始网址填什么网站就操作什么网站；下面用到的会议室预订站和模拟商城只是本地示例场景。

## 目录结构

```
web-agent-lab/
├── computer-use-preview/   Google 官方参考实现（git clone，原样未改）
│   ├── .venv/              Python 3.12 环境（google-genai、playwright 等）
│   └── .env                Vertex 配置：项目、global 区域（已被 .gitignore 忽略）
├── agent/                  Agent 核心：core.py（主循环、计划、卡住检测）、sandbox.py（白名单、多标签页）、
│                           tools.py（自定义工具）、login.py（按网站扩展的登录处理）、trajectory.py、upstream.py
├── run_local.py            命令行入口
├── cache_replay.py         带缓存回放的命令行入口
├── app/                    网页版：server.py（接口）、state.py（运行状态）、takeover.py（接管）、static/（界面，原生 ES 模块）
├── sites/meeting/          示例场景：会议室预订（静态页面，端口 8770）
├── sites/shop/             示例场景：模拟商城（静态页面，假支付，端口 8771）
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

代码在 `app/`：`server.py`（后端，复用 `agent/` 里的 Agent）、`state.py`、`takeover.py` 和 `static/`（界面）。

### 操作任意网站

在“选项”里把起始网址改成目标网站，用一句话写清楚要做什么即可，比如查资料并整理成要点、填一张表单、在管理后台里改一项设置。任务可以跨多个页面和网站。

**多标签页**：网页打开新标签页时会保留并自动切过去（最多 8 个），模型可以查看有哪些标签页、切换、关闭。右边的画面始终是当前标签页。用到多标签页的运行不会写入缓存。

### 暂停与接管

- 运行中点“暂停”，它做完当前这一步就停下。
- 暂停、卡住、等你登录时，右边变成每 1.5 秒刷新的实时画面，可以直接在画面上点击、滚动；要输入文字，在卡片的输入框里输入后点“输入”（另有“回车”“退格”）。
- 处理完点“继续执行”，它会从当前页面接着做；可以顺手写一句话，告诉它接下来怎么做。
- 你输入的文字不会写进运行记录。有人接管过的运行不会写入缓存。

### 扫码登录

- 打开的页面需要登录时，它会停下来弹“需要你登录”，不会自己输入账号、密码或验证码。
- 页面上有二维码时，卡片里会单独放大显示一张清晰的，用手机 App 扫码即可；二维码过期会自动刷新。
- **什么时候算登录成功**：只有查到明确的已登录证据（该网站登录后才会写入的 cookie 新出现或换了值，并且已经离开登录页）才会自动继续。只是离开了登录页不算。查不到证据时保持等待，你登录后点“已登录，继续”。
- 目前只为京东写了证据规则（`agent/login.py`）；其他网站都需要你点“已登录，继续”。新网站在 `agent/login.py` 里加一个 `LoginSite` 子类。
- 登录状态（cookies）保存在本机的 `auth/storage_state.json`，下次运行自动带上；这个文件不进 git，不要分享。

## 命令行版运行步骤

下面用本地会议室预订站做示例。操作别的网站：`--initial_url 网址 --allow 域名`（或 `--allow_all`）。

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

结束状态：`COMPLETE` 完成 / `INCOMPLETE` 给了回答但计划里有一步没做成 / `GAVE_UP` 模型没给答案就结束了 / `STUCK` 卡住 / `TIMEOUT` 超时 / `MAX_STEPS` 超步数 / `SAFETY_STOP` 需要确认被停下。
| `--initial_url` | `http://127.0.0.1:8770/index.html` | 必须在白名单内 |
| `--allow` | 空 | 额外放行的外部域名（含子域名），例如 `--allow wikipedia.org wikimedia.org`。白名单外的跳转会被拦下，Agent 留在原页面 |
| `--highlight_mouse` | 关 | 在点击位置画红圈 |
| `--auto_confirm` | 关 | Gemini 认为“提交预订”这类操作不可撤销，会要求用户确认；不加这个参数就停在那一步，加了就自动放行（只适合本地测试站） |
| `--allow_all` | 关 | 不限制网站，任何网址都能访问 |
| `--single_tab` | 关 | 关闭多标签页：网页想开新标签页时并入当前页（官方代码原来的做法） |

不想弹窗口（无头模式）：在命令前加 `PLAYWRIGHT_HEADLESS=1`。注意官方代码里这个变量只要非空就是无头模式，写 `false` 也会变成无头。

## 安全设置

- **网络白名单**（命令行版）：默认只能访问本地示例站（`127.0.0.1` / `localhost` 的 8770、8771 端口），其他请求全部拦截并记进日志；用 `--allow` 放行指定域名，`--allow_all` 不限制。
- **登录状态**：默认是全新的浏览器环境。只有你通过“请你登录”流程亲自登录过的网站，cookies 才会保存在本机并在之后自动带上；删掉 `auth/` 目录即可清除。
- **需要确认就停**：模型返回 `require_confirmation` 时默认直接终止（状态记为 `SAFETY_STOP`），只有加了 `--auto_confirm` 才放行。
- **最大步数**：默认 25 步。

## 已知问题

- 官方 README 提到：Playwright 在某些系统上截不到原生下拉框（`<select>`），所以测试站里没有用下拉框。
- 每一步都会把截图发给 Gemini，费用从 Vertex 赠金里扣，一个简单任务大约几美分。
