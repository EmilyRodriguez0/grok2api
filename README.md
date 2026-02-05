# Grok2API

OpenAI 兼容的 Grok API 代理服务，支持 Chat Completions 和 Responses API。

## ✨ 核心特性

- ✅ **OpenAI Chat Completions API 兼容** - 完整支持 `/v1/chat/completions` 端点
- ✅ **OpenAI Responses API 兼容** - 新增 `/v1/responses` 端点
- ✅ **流式和非流式响应** - 默认非流式，显式 `stream=true` 启用流式
- ✅ **多模态支持** - 文本、图像、音频、文件上传
- ✅ **视频生成** - 支持 Grok 视频生成模型
- ✅ **图像生成** - 支持 Grok Imagine 图像生成
- ✅ **Token 自动管理** - 自动刷新和负载均衡
- ✅ **Web 管理界面** - Token 和配置管理

---

## 📦 安装

### 环境要求

- Python 3.10+
- Redis/MySQL/PostgreSQL (可选，用于分布式部署)

### 快速开始

```bash
# 克隆项目
git clone https://github.com/your-repo/grok2api.git
cd grok2api

# 安装依赖
pip install -r requirements.txt

# 配置环境变量
cp .env.example .env
# 编辑 .env 文件，配置必要参数

# 启动服务
python main.py
```

服务默认运行在 `http://localhost:8000`

---

## 🚀 API 使用指南

### 1. Chat Completions API

完整兼容 OpenAI Chat Completions API。

#### 非流式请求（默认）

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4",
    "messages": [
      {"role": "user", "content": "你好"}
    ]
  }'
```

**响应**：
```json
{
  "id": "chatcmpl-xxx",
  "object": "chat.completion",
  "created": 1730000000,
  "model": "grok-4",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "你好！我是 Grok..."
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

#### 流式请求

```bash
curl -N http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4",
    "messages": [
      {"role": "user", "content": "你好"}
    ],
    "stream": true
  }'
```

**响应**（SSE 流）：
```text
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"delta":{"role":"assistant"}}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"delta":{"content":"你"}}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"delta":{"content":"好"}}]}

data: [DONE]
```

---

### 2. Responses API (新)

符合 OpenAI Responses API 规范的新端点。

#### 非流式请求（默认）

```bash
curl http://localhost:8000/v1/responses \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4",
    "input": "你好"
  }'
```

**响应**：
```json
{
  "id": "resp-xxx",
  "object": "response",
  "created": 1730000000,
  "model": "grok-4",
  "status": "completed",
  "output": [{
    "id": "msg-xxx",
    "type": "message",
    "role": "assistant",
    "content": [{
      "type": "output_text",
      "text": "你好！我是 Grok..."
    }]
  }],
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0
  },
  "output_text": "你好！我是 Grok..."
}
```

#### 流式请求

```bash
curl -N http://localhost:8000/v1/responses \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4",
    "input": "你好",
    "stream": true
  }'
```

**响应**（SSE 事件流）：
```text
event: response.created
data: {"type":"response.created","response":{...}}

event: response.output_item.added
data: {"type":"response.output_item.added","output_index":0,...}

event: response.content_part.added
data: {"type":"response.content_part.added","output_index":0,...}

event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"你"}

event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"好"}

event: response.output_text.done
data: {"type":"response.output_text.done","text":"你好！..."}

event: response.completed
data: {"type":"response.completed","response":{...}}
```

#### 带系统指令

```bash
curl http://localhost:8000/v1/responses \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4",
    "input": "你好",
    "instructions": "你是一个友好的助手"
  }'
```

#### 多模态输入

```bash
curl http://localhost:8000/v1/responses \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4",
    "input": [
      {
        "role": "user",
        "content": [
          {"type": "input_text", "text": "这是什么？"},
          {"type": "input_image", "image_url": "https://example.com/image.jpg"}
        ]
      }
    ]
  }'
```

---

### 3. 使用 OpenAI SDK

#### Chat Completions

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="YOUR_API_KEY"
)

# 非流式
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "你好"}
    ]
)
print(response.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "user", "content": "你好"}
    ],
    stream=True
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

#### Responses API

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="YOUR_API_KEY"
)

# 非流式
response = client.responses.create(
    model="grok-4",
    input="你好"
)
print(response.output_text)

# 流式
stream = client.responses.create(
    model="grok-4",
    input="你好",
    stream=True
)
for event in stream:
    if event.type == "response.output_text.delta":
        print(event.delta, end="")
```

---

## 🎮 支持的模型

| 模型 ID | 描述 | 类型 |
|---------|------|------|
| `grok-3` | Grok 3 基础模型 | 文本 |
| `grok-3-fast` | Grok 3 快速模式 | 文本 |
| `grok-4` | Grok 4 基础模型 | 文本 |
| `grok-4-mini` | Grok 4 Mini（思考模式） | 文本 |
| `grok-4-fast` | Grok 4 快速模式 | 文本 |
| `grok-4-heavy` | Grok 4 Heavy（需要 Super 权限） | 文本 |
| `grok-4.1-fast` | Grok 4.1 快速模式 | 文本 |
| `grok-4.1-expert` | Grok 4.1 专家模式 | 文本 |
| `grok-4.1-thinking` | Grok 4.1 思考模式 | 文本 |
| `grok-imagine-1.0` | Grok Imagine 图像生成 | 图像 |
| `grok-imagine-1.0-video` | Grok 视频生成 | 视频 |

---

## ⚙️ 配置说明

### Stream 参数行为（重要更新）

从本版本开始，`stream` 参数的默认行为已标准化：

- **默认非流式**：请求不包含 `stream` 参数或 `stream=null` 时，返回完整 JSON 响应
- **显式流式**：只有明确传递 `stream=true` 时，才返回 SSE 流式响应
- **配置文件不再影响默认行为**：`config.toml` 中的 `grok.stream` 配置已 **弃用（DEPRECATED）**

#### 迁移指南

如果您之前依赖配置文件的 `grok.stream=true` 来默认启用流式响应，需要在请求中显式添加 `stream: true`：

```diff
{
  "model": "grok-4",
  "messages": [...],
+ "stream": true
}
```

---

## 🔧 高级配置

### 环境变量

```bash
# 服务配置
SERVER_HOST=0.0.0.0
SERVER_PORT=8000
SERVER_WORKERS=1

# 日志级别
LOG_LEVEL=INFO

# 存储类型 (local/redis/mysql/pgsql)
SERVER_STORAGE_TYPE=local

# Redis 配置（可选）
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0

# 数据库配置（可选）
DATABASE_URL=mysql://user:pass@localhost/grok2api
```

### 配置文件

详细配置请参考 `config.defaults.toml`。

---

## 🛡️ 安全建议

1. **API Key 保护**：设置 `app.api_key` 启用接口认证
2. **管理后台密钥**：配置 `app.app_key` 保护管理界面
3. **CORS 配置**：生产环境建议限制 `allow_origins`
4. **Token 安全**：不要将 Grok Token 暴露在公开仓库

---

## 📊 性能优化

- **并发控制**：通过 `performance` 配置调整并发数
- **Token 池**：自动负载均衡多个 Token
- **缓存**：自动清理过期的上传文件和缓存
- **重试机制**：智能重试失败请求（401/429/403）

---

## 🐛 故障排查

### 问题：默认返回流式响应

**原因**：旧版本配置文件中 `grok.stream=true`

**解决**：
1. 更新 `config.defaults.toml`，将 `grok.stream` 改为 `false`
2. 或在请求中显式传递 `stream: false`

### 问题：Token 过期

**原因**：Grok Token 有效期有限

**解决**：
1. 启用自动刷新：`token.auto_refresh=true`
2. 手动在管理后台更新 Token

### 问题：图像/视频生成失败

**原因**：需要上传文件到 Grok

**解决**：
1. 确保网络连通性
2. 检查代理配置（如有）

---

## 📝 更新日志

### v2.0.0 (2025-02-06)

**重大更新**：
- ✨ 新增 OpenAI Responses API 支持 (`/v1/responses`)
- 🔧 修复 stream 默认值问题（默认非流式）
- 📝 标准化 API 行为，符合 OpenAI 规范

**Breaking Changes**：
- `stream` 参数默认行为从"可能流式"改为"默认非流式"
- 配置文件的 `grok.stream` 不再影响 API 默认行为（已弃用）

**详细变更**：
- 新增 `app/api/v1/responses.py` - Responses API 端点
- 新增 `app/api/v1/_openai_compat.py` - OpenAI 兼容公共函数
- 修复 `app/api/v1/chat.py` - stream validator 返回 False 而非 None
- 修复 `app/services/grok/chat.py` - 移除 3 处配置回退逻辑
- 修复 `app/services/grok/media.py` - 移除视频流配置回退
- 更新 `config.defaults.toml` - 标记 `grok.stream` 为 deprecated

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

## 📄 License

MIT License

---

## 🔗 相关链接

- [OpenAI API 文档](https://platform.openai.com/docs/api-reference)
- [Grok](https://x.ai/)

---

**注意**：本项目仅用于学习和研究目的，请遵守 Grok 的使用条款。
