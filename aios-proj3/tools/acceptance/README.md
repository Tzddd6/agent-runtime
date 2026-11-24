# 浏览器核心验收（离线模型）

需要Python3.11+、`pymupdf`、`pillow`与Node.js的`playwright`。夹具显式返回固定补丁和格式文本，不使用供应商Key、不产生模型费用。真实HTTP、Worker、测试进程和浏览器操作仍实际执行。

在项目根目录：

```bash
python -m pip install pymupdf pillow
npm install --prefix /tmp/aios-browser-deps playwright
export NODE_PATH=/tmp/aios-browser-deps/node_modules
npx --prefix /tmp/aios-browser-deps playwright install chromium
node tools/acceptance/browser_acceptance.cjs /absolute/path/to/python3
```

Windows可用`$env:NODE_PATH`指定依赖目录、`$env:AIOS_BROWSER_EXECUTABLE`指定已安装Edge或Chrome的完整路径。也可通过`AIOS_BROWSER_REPORTS`指定报告输出目录。未指定浏览器路径时使用Playwright Chromium。脚本不会下载安装依赖。

6条检查：两轮工程修复与ZIP独立复测；刷新恢复；零预算；非法图片；PNG/PDF/DOCX/TXT上传；公式/代码/表格/长文本流式与HTML注入防护。输出截图、JSON和独立测试日志。失败返回非零退出码，服务器在finally关闭。

这不是全浏览器矩阵，也不是对真实模型回答质量的评分。
