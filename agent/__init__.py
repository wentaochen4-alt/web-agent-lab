"""通用浏览器操作 Agent 的核心（不依赖网页版，也不绑定任何具体网站或任务类型）。

  upstream.py    接入官方参考实现 computer-use-preview
  trajectory.py  运行记录（trajectory.jsonl + 截图编号）
  sandbox.py     浏览器访问约束 + SandboxedPlaywright
  tools.py       给模型用的自定义工具
  login.py       各网站的登录页识别、二维码查找和过期刷新、登录状态判断
  core.py        LoggedAgent：主循环、计划、卡住检测、请人接管
"""
