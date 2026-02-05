# API 层重构实施计划

## 目标
1. **stream 参数标准化** - 默认 `false`，由客户端显式传递控制
2. **reasoning_content 分离** - `<think>` 内容输出到独立字段
3. **Bearer 重复修复** - 前端 `cancelBatchTask` 逻辑修正

---

## 修改清单

### 1. app/api/v1/chat.py

**修改点**：`ChatCompletionRequest.stream` 字段

```python
# 修改前
stream: Optional[bool] = Field(None, description="是否流式输出")

# 修改后
stream: bool = Field(False, description="是否流式输出")
```

**修改点**：`validate_stream` validator

```python
# None 时返回 False（OpenAI 兼容）
if v is None:
    return False
```

---

### 2. app/api/v1/image.py

**修改点**：`ImageGenerationRequest.stream` 字段

```python
# 修改前
stream: Optional[bool] = Field(False, description="是否流式输出")

# 修改后
stream: bool = Field(False, description="是否流式输出")
```

**新增**：`validate_stream` validator（与 chat.py 一致）

**移除**：`create_image` 函数中的手动默认值设置

```python
# 移除这段
if request.stream is None:
    request.stream = False
```

---

### 3. app/services/grok/chat.py

**修改点**：`ChatRequest.stream` 默认值

```python
# 修改前
stream: bool = None

# 修改后
stream: bool = False
```

**修改点**：`GrokChatService.chat()` - 移除配置回退

```python
# 移除这段
if stream is None:
    stream = get_config("grok.stream", True)
```

**修改点**：`GrokChatService.chat_openai()` - 移除配置回退

```python
# 修改前
stream = (
    request.stream
    if request.stream is not None
    else get_config("grok.stream", True)
)

# 修改后
stream = True if request.stream is True else False
```

**修改点**：`ChatService.completions()` - 移除配置回退

```python
# 修改前
is_stream = stream if stream is not None else get_config("grok.stream", True)

# 修改后
is_stream = True if stream is True else False
```

**修改点**：`CollectProcessor` 初始化添加 `think` 参数

```python
# 修改前
result = await CollectProcessor(model_name, token).process(response)

# 修改后
result = await CollectProcessor(model_name, token, think).process(response)
```

---

### 4. app/services/grok/processor.py

**新增**：`ThinkTagParser` 类（状态机，支持跨 token 边界）

```python
class ThinkTagParser:
    """<think> 标签解析状态机"""
    OPEN_TAG = "<think>"
    CLOSE_TAG = "</think>"

    def __init__(self):
        self._in_think = False
        self._pending = ""

    def feed(self, text: str) -> list[tuple[bool, str]]:
        """返回 [(is_reasoning, segment_text), ...]"""
        # 实现跨 token 边界的标签识别
        ...

    def flush(self) -> list[tuple[bool, str]]:
        """流结束时输出 pending"""
        ...
```

**新增**：辅助函数

```python
def _strip_think_tags(text: str) -> str:
    """移除 <think> 标签但保留文本"""

def _split_think_content(text: str) -> tuple[str, str]:
    """拆分为 (content, reasoning_content)"""
```

**修改点**：`BaseProcessor._sse()` - 添加 `reasoning_content` 参数

```python
def _sse(
    self,
    content: str = "",
    role: str = None,
    finish: str = None,
    reasoning_content: str = None,  # 新增
) -> str:
```

**修改点**：`StreamProcessor`

- 移除 `self.think_opened` 状态
- 新增 `self._think_parser = ThinkTagParser()`
- 图像生成进度改用 `reasoning_content` 输出
- 普通 token 通过 `_think_parser.feed()` 分流

**修改点**：`CollectProcessor`

- `__init__` 添加 `think` 参数
- `process()` 调用 `_split_think_content()` 分离内容
- 返回结果添加 `message.reasoning_content` 字段

**修改点**：`VideoStreamProcessor`

- 移除 `self.think_opened` 状态
- 进度信息改用 `reasoning_content` 输出

---

### 5. app/static/common/batch-sse.js

**修改点**：`cancelBatchTask` 函数

```javascript
// 修改前
headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : undefined

// 修改后
const authValue = apiKey && apiKey.startsWith('Bearer ') ? apiKey : `Bearer ${apiKey}`;
headers: apiKey ? { Authorization: authValue } : undefined
```

---

## 测试验证

### 1. stream 参数测试

```bash
# 默认非流式
curl -X POST /v1/chat/completions -d '{"model":"grok-3","messages":[...]}'
# 预期：返回完整 JSON

# 显式流式
curl -X POST /v1/chat/completions -d '{"model":"grok-3","messages":[...],"stream":true}'
# 预期：返回 SSE 流
```

### 2. reasoning_content 测试

```bash
# 启用 thinking
curl -X POST /v1/chat/completions -d '{"model":"grok-3","messages":[...],"thinking":"enabled"}'
# 预期：响应包含 reasoning_content 字段，content 不包含 <think> 标签
```

### 3. 取消任务测试

```bash
# 管理后台触发批量任务 -> 点击取消
# 预期：任务正常取消，无 401 错误
```

---

## 风险评估

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| 旧客户端未传 stream 时行为变化 | 中 | 发布说明明确默认值变更 |
| reasoning_content 新字段 | 低 | 仅在 thinking=enabled 时输出 |
| 跨 token 标签误判 | 低 | 状态机实现边界处理 |

---

## 实施顺序

1. ✅ processor.py - 添加 ThinkTagParser 和辅助函数
2. ✅ processor.py - 修改 StreamProcessor/CollectProcessor/VideoStreamProcessor
3. ✅ chat.py (service) - 移除配置回退，传递 think 参数
4. ✅ chat.py (api) - 修改 stream 默认值和 validator
5. ✅ image.py - 同上
6. ✅ batch-sse.js - 修复 Bearer 重复
7. 🧪 集成测试验证
