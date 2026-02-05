"""
OpenAI Responses API 兼容端点

实现 POST /v1/responses，兼容 OpenAI Responses API 规范
"""

from typing import List, Dict, Any, Union, Optional, AsyncGenerator
from pydantic import BaseModel, Field, field_validator
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
import uuid
import time
import orjson

from app.services.grok.chat import ChatService
from app.api.v1._openai_compat import (
    coerce_stream,
    responses_input_to_messages,
    chat_completion_to_response,
)
from app.core.logger import logger


router = APIRouter()


class ResponsesRequest(BaseModel):
    """Responses API 请求模型"""

    model: str = Field(..., description="模型名称")
    input: Union[str, List[Dict[str, Any]]] = Field(
        ..., description="输入内容（字符串或消息数组）"
    )
    instructions: Optional[str] = Field(None, description="系统指令")
    stream: bool = Field(False, description="是否流式输出")
    thinking: Optional[str] = Field(None, description="思考模式: enabled/disabled/None")

    @field_validator("stream", mode="before")
    @classmethod
    def validate_stream(cls, v):
        """确保 stream 参数被正确解析为布尔值"""
        return coerce_stream(v)

    model_config = {"extra": "ignore"}


async def generate_responses_sse(
    chat_stream: AsyncGenerator,
    response_id: str,
    model: str
) -> AsyncGenerator[str, None]:
    """
    将 Chat Completions SSE 流转换为 Responses API SSE 流

    Args:
        chat_stream: Chat Completions 的 SSE 生成器
        response_id: Response ID
        model: 模型名称

    Yields:
        str: Responses API 格式的 SSE 事件
    """
    msg_id = f"msg-{response_id[5:]}" if response_id.startswith("resp-") else "msg-unknown"
    created = int(time.time())
    full_text = ""
    role_sent = False

    def sse_event(event_type: str, data: dict) -> str:
        """构建 SSE 事件"""
        return f"event: {event_type}\ndata: {orjson.dumps(data).decode()}\n\n"

    try:
        # 1. 发送 response.created
        yield sse_event("response.created", {
            "type": "response.created",
            "response": {
                "id": response_id,
                "object": "response",
                "status": "in_progress",
                "model": model,
                "created": created,
                "output": []
            }
        })

        # 2. 发送 response.output_item.added
        yield sse_event("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": []
            }
        })

        # 3. 发送 response.content_part.added
        yield sse_event("response.content_part.added", {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "part": {
                "type": "output_text",
                "text": ""
            }
        })

        # 4. 解析 Chat SSE 流并发送增量
        async for line in chat_stream:
            # 过滤掉 [DONE] 标记
            if "data: [DONE]" in line:
                continue

            # 解析 data: {...} 行
            if line.startswith("data: "):
                try:
                    json_str = line[6:].strip()
                    chunk = orjson.loads(json_str)
                except (orjson.JSONDecodeError, ValueError):
                    continue

                # 提取 delta 内容
                choices = chunk.get("choices", [])
                if not choices:
                    continue

                first_choice = choices[0]
                delta = first_choice.get("delta", {})

                # 提取 role（第一次）
                if not role_sent and delta.get("role"):
                    role_sent = True
                    continue

                # 提取 content
                content = delta.get("content")
                if content:
                    full_text += content
                    # 发送 response.output_text.delta
                    yield sse_event("response.output_text.delta", {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": content
                    })

                # 检查 finish_reason
                finish_reason = first_choice.get("finish_reason")
                if finish_reason:
                    # 流结束，跳出循环
                    break

        # 5. 发送 response.output_text.done
        yield sse_event("response.output_text.done", {
            "type": "response.output_text.done",
            "output_index": 0,
            "content_index": 0,
            "text": full_text
        })

        # 6. 发送 response.completed
        yield sse_event("response.completed", {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "created": created,
                "model": model,
                "status": "completed",
                "output": [
                    {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": full_text
                            }
                        ]
                    }
                ],
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0
                },
                "output_text": full_text
            }
        })

    except Exception as e:
        # 发送错误事件
        logger.error(f"Responses SSE error: {e}")
        yield sse_event("response.error", {
            "type": "response.error",
            "error": {
                "type": "internal_error",
                "message": str(e)
            }
        })


@router.post("/responses")
async def create_response(request: ResponsesRequest):
    """
    创建 Response（OpenAI Responses API 兼容）

    支持非流式和流式两种模式：
    - stream=false（默认）：返回完整 Response 对象
    - stream=true：返回 SSE 事件流

    Examples:
        非流式:
        ```bash
        curl http://localhost:8000/v1/responses \\
          -H "Authorization: Bearer $KEY" \\
          -H "Content-Type: application/json" \\
          -d '{
            "model": "grok-4",
            "input": "你好"
          }'
        ```

        流式:
        ```bash
        curl -N http://localhost:8000/v1/responses \\
          -H "Authorization: Bearer $KEY" \\
          -H "Content-Type: application/json" \\
          -d '{
            "model": "grok-4",
            "input": "你好",
            "stream": true
          }'
        ```
    """
    logger.debug(
        f"Responses request: model={request.model}, stream={request.stream}, "
        f"input_type={type(request.input).__name__}"
    )

    # 1. 转换 input -> messages
    try:
        messages = responses_input_to_messages(
            request.input,
            instructions=request.instructions
        )
    except ValueError as e:
        from app.core.exceptions import ValidationException
        raise ValidationException(
            message=str(e),
            param="input",
            code="invalid_input"
        )

    logger.debug(f"Converted to {len(messages)} messages")

    # 2. 调用 ChatService.completions
    result = await ChatService.completions(
        model=request.model,
        messages=messages,
        stream=request.stream,
        thinking=request.thinking
    )

    # 3. 根据 stream 模式返回结果
    if request.stream:
        # 流式：转换为 Responses SSE
        response_id = f"resp-{uuid.uuid4().hex[:24]}"

        return StreamingResponse(
            generate_responses_sse(result, response_id, request.model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )
    else:
        # 非流式：转换为 Response 对象
        response = chat_completion_to_response(result)
        logger.debug(f"Response created: {response['id']}")
        return response
