# gemini-web2api

<p align="center">
  <img src="logo.png" width="200" alt="gemini-web2api logo">
</p>

[English](README.md)

将 Google Gemini 网页端转换为 OpenAI 兼容 API. 零成本, 跨平台, 单文件.

## 特性

- **可选密钥**: `API_KEYS` 为空时免密, 填入密钥后按 OpenAI Bearer Key 校验
- **OpenAI 兼容**: 直接替换 `/v1/chat/completions` 和 `/v1/models`
- **工具调用**: 完整的 Function Calling 支持 (OpenAI 格式)
- **多模型**: Flash (3.7/3.6), 扩展思考 (2万字+输出), Pro, Auto, Lite
- **思考深度**: 通过 `@think=N` 后缀调节 (0=最深, 4=最浅)
- **多模态**: 图片/文件输入 (Scotty 上传) 与 AI 生图输出 (markdown 或纯 URL)
- **联网搜索**: 内置互联网访问 (Gemini 原生搜索能力)
- **跨平台**: 纯 Python, 仅一个可选依赖 (`httpx` 用于流式输出)
- **流式输出**: 基于 `httpx` 的 SSE Streaming, 支持 `stream_options.include_usage`
- **Codex CLI**: Responses API (`/v1/responses`) 兼容 OpenAI Codex
- **Gemini CLI**: Google 原生 API (`/v1beta/models`) 兼容 Gemini CLI

## 快速开始

```bash
pip install httpx
python gemini_web2api.py        # 独立单文件 (由 build_single_file.py 生成)
# 或
python -m gemini_web2api        # 包形式 (Docker/Railway 使用)
```

服务启动在 `http://localhost:8081/v1`.

配置优先级: 默认值 < `config.json` (可选) < 环境变量 < 命令行参数. 无需 `config.json`, 只用环境变量即可启动:

```bash
set GEMINI_COOKIE=__Secure-1PSID=...; SAPISID=...
set API_KEYS=sk-test
python -m gemini_web2api
```

## 客户端配置

### Cherry Studio / ChatBox / 任何 OpenAI 兼容客户端

| 字段 | 值 |
|------|-----|
| Base URL | `http://localhost:8081/v1` |
| API Key | `API_KEYS`/`config.json` 中的任意密钥；未配置时随便填 |
| Model | `gemini-3.5-flash-thinking` |

### curl

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-your-key" \
  -d '{"model":"gemini-3.7-flash","messages":[{"role":"user","content":"你好!"}]}'
```

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-your-key")
resp = client.chat.completions.create(
    model="gemini-3.5-flash-thinking",
    messages=[{"role": "user", "content": "解释量子计算"}]
)
print(resp.choices[0].message.content)
```

### Gemini CLI

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://localhost:8081
gemini
```

支持 Google 原生 API 端点:
- `GET /v1beta/models` — 模型列表
- `POST /v1beta/models/{model}:generateContent` — 非流式生成
- `POST /v1beta/models/{model}:streamGenerateContent` — 流式生成 (SSE)

## 可用模型

| 模型 | 说明 | 输出量 |
|------|------|--------|
| `gemini-3.7-flash` | 最新 Flash (与 3.6-flash 同一线路) | ~1.2万字 |
| `gemini-3.6-flash` | 全能模型 (默认) | ~1.2万字 |
| `gemini-3.5-flash` | gemini-3.6-flash 别名 | ~1.2万字 |
| `gemini-3.5-flash-thinking` | 扩展思考, 最长输出 | **~2万字** |
| `gemini-3.5-flash-thinking-lite` | 自适应思考深度 | ~1.5万字 |
| `gemini-3.1-pro` | 高级数学与代码 (需 cookie) | ~1.2万字 |
| `gemini-3.1-pro-enhanced` | Pro 增强输出 (实验性) | ~1.2万字 |
| `gemini-auto` | 自动选择模型 | 不定 |
| `gemini-flash-lite` | 最快响应, 轻量 | ~1万字 |

### 思考深度

在模型名后追加 `@think=N`:

```
gemini-3.5-flash-thinking@think=0   # 最深 (默认)
gemini-3.5-flash-thinking@think=2   # 中等
gemini-3.5-flash-thinking@think=4   # 最浅
```

## 可选: Cookie 配置 (Pro 模型 / 文件上传)

匿名访问对文本有效, 但 `gemini-3.1-pro` 无认证时会路由到 Flash, 图片/文件上传需要登录态. 要获得真正的 Pro 路由与文件能力, 需要 cookie:

- **环境变量 (推荐)**: `GEMINI_COOKIE` = 完整 `Cookie` 头单行 (自动解析 `SAPISID`), 或 JSON `{"cookie": "...", "sapisid": "..."}`. `GEMINI_SAPISID` 可覆盖.
- **文件方式**: `python gemini_web2api.py --cookie-file cookie.txt` (仍受支持).
- **运行时推送**: 扩展 `gemini-cookie-sync-extension` 可把当前会话直接 POST 到运行中的服务 (`POST /admin/cookie`, 用 `ADMIN_KEY` 保护), 无需重启.

### 如何获取 Cookie

1. 打开 Chrome, 访问 [gemini.google.com](https://gemini.google.com) 并登录账号
2. 打开开发者工具 (F12) → Application → Cookies → `https://gemini.google.com`
3. 复制 cookie 值, 拼成 `Cookie` 头格式:

```
__Secure-1PSID=...; SAPISID=...; __Secure-1PSIDTS=...
```

**推荐 (浏览器扩展)**: 使用 `gemini-cookie-sync-extension` (仓库内), 一键导出或直接推送到服务.

### 登录账号路径与 XSRF Token

已登录的 Gemini 页面 URL 若带账号序号 (`https://gemini.google.com/u/1/app/...`), 请设置 `GEMINI_AUTH_USER=1`. 登录态请求还可能需要 XSRF token (`GEMINI_XSRF_TOKEN`), 即页面源码中的 `SNlM0e`; 扩展会自动提取.

如果登录态请求返回 HTTP 400 且错误中包含 `xsrf`, 请刷新 Gemini Web 后更新 XSRF, 并确认 `auth_user` 与浏览器 URL 中的 `/u/<序号>/` 一致. 文件请求返回 `BardErrorInfo [1100]` 时, 先检查 `GET /v1/diag` 的 `page_scrape_ok`, 通常意味着 cookie 过期或账号无文件权限.

Pro 路由需要 **Gemini Advanced** (付费订阅). 免费 Google 账号的 cookie 可以登录认证, 但会静默回退到 Flash.

## 配置文件 (可选)

`config.json` 与 `DEFAULT_CONFIG` 同结构, 常用键:

```json
{
  "port": 8081,
  "host": "0.0.0.0",
  "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
  "api_keys": ["sk-your-key"],
  "image_format": "markdown",
  "rate_limit": 0,
  "admin_key": null,
  "token_cache_file": null
}
```

- `api_keys`: 空数组 `[]` 不校验密钥；填入后 `/v1/*` 需要 `Authorization: Bearer <key>` 或 `x-api-key: <key>`.
- `image_format`: 生图输出格式, `markdown` (默认, `![generated image](url)`) 或 `url` (纯链接).
- `rate_limit`: 每密钥每分钟最大请求数, `0` = 不限.
- `admin_key`: `POST /admin/cookie` 的管理密钥 (未设置时回退到 `api_keys` 校验).
- `token_cache_file`: 可选, 把抓取的页面 token 缓存到文件, 重启后免重新抓取.

对应环境变量: `GEMINI_IMAGE_FORMAT`, `GEMINI_RATE_LIMIT`, `GEMINI_ADMIN_KEY`, `GEMINI_TOKEN_CACHE_FILE` (以及 `PORT`, `HOST`, `API_KEYS`, `GEMINI_COOKIE` 等, 详见 `config.py`).

## Docker 部署

```bash
docker build -t gemini-web2api .
docker run -d --name gemini-web2api -p 8081:8081 \
  -e GEMINI_COOKIE="__Secure-1PSID=...; SAPISID=..." \
  -e API_KEYS=sk-your-key \
  gemini-web2api
```

镜像只需环境变量即可启动, 不需要挂载 `config.json`.

> **注意**: 如果 Docker 默认 bridge 网络下出现空回复 (`content: null`), 请切换到 host 网络: `docker run --network host ...`. 这是 Gemini 上游拒绝来自 Docker NAT IP 段的请求导致的.

## 代理配置

如果无法直接访问 `gemini.google.com` (连接超时), 需要配置代理:

**方式 1: 命令行参数**
```bash
python gemini_web2api.py --proxy http://127.0.0.1:7890
```

**方式 2: 环境变量**
```bash
set GEMINI_PROXY=http://127.0.0.1:7890
```

支持 Clash, V2Ray, Shadowsocks 等任何 HTTP 代理.

## 已知限制

- **多模态需要登录态**: 图片/文件上传与 Pro 路由需要有效 cookie; 无 cookie 时只支持文本.
- **Pro/Ultra 非真实路由**: 无付费订阅 cookie 时, `gemini-3.1-pro` 实际路由到 Flash 模型.
- **单轮对话**: 每次请求是独立对话, 多轮上下文通过在 prompt 中包含历史消息模拟.
- **频率限制**: Google 可能限制高频请求, server 会自动重试; 可配置 `rate_limit` 防止被打爆.
- **usage 为估算值**: Gemini Web 的 StreamGenerate 响应不含真实 token 数, 返回的 usage 为字符数估算.

## 系统要求

- Python 3.8+
- `httpx` (`pip install httpx`) — 用于流式请求
- 需要能访问 `gemini.google.com` (部分地区需代理)

## 工作原理

逆向 Google Gemini 网页端的 StreamGenerate 协议, 将 OpenAI API 格式与 Gemini 内部 protobuf-like 格式互转. 模型选择通过请求 payload 的 `[79]` 字段控制, 映射自 Gemini 前端 JS 源码中的 `MODE_CATEGORY` 枚举.

## 开发

- 包 `gemini_web2api/` 是唯一源码; `gemini_web2api.py` 由 `python build_single_file.py` 生成, 不要手改.
- 测试: `python -m unittest discover -s tests`
- 校验: `python -m py_compile gemini_web2api.py gemini_web2api\*.py`

## 致谢

- [linux.do](https://linux.do) 社区
- 开源 API 代理生态

## License

MIT