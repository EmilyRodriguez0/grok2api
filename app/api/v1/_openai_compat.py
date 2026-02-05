"""
OpenAI API 兼容性工具模块

提供 Chat Completions 和 Responses API 的公共函数
"""

from typing import Any, Dict, List, Union


def coerce_stream(v: Any) -> bool:
    """
    统一 stream 参数解析逻辑

    用于 Pydantic validator，确保 stream 参数被正确解析为布尔值。
    默认值：False（非流式）

    Args:
        v: 输入值（可能是 None, bool, str, int 等）

    Returns:
        bool: 解析后的布尔值

    Raises:
        ValueError: 当值无法解析为布尔值时

    Examples:
        >>> coerce_stream(None)
        False
        >>> coerce_stream(True)
        True
        >>> coerce_stream("true")
        True
        >>> coerce_stream("false")
        False
        >>> coerce_stream(1)
        True
    """
    # None 默认为 False（非流式）
    if v is None:
        return False

    # 已经是布尔值
    if isinstance(v, bool):
        return v

    # 字符串类型解析
    if isinstance(v, str):
        lower_v = v.lower()
        if lower_v in ("true", "1", "yes"):
            return True
        if lower_v in ("false", "0", "no"):
            return False
        raise ValueError(
            f"Invalid stream value '{v}'. Must be a boolean or one of: "
            "true, false, 1, 0, yes, no"
        )

    # 数字类型（0 = False, 非0 = True）
    if isinstance(v, (int, float)):
        return bool(v)

    # 其他类型无法解析
    raise ValueError(
        f"Invalid stream value type '{type(v).__name__}'. "
        "Must be a boolean, string, or number."
    )


def responses_input_to_messages(
    input_data: Union[str, List[Dict[str, Any]]],
    instructions: str = None
) -> List[Dict[str, Any]]:
    """
    将 Responses API 的 input 转换为 Chat Completions 的 messages 格式

    Args:
        input_data: Responses API 的 input 参数
            - 字符串：单条 user 消息
            - 数组：Responses message 结构
        instructions: 可选的系统指令（会作为 system message 置于最前）

    Returns:
        List[Dict]: Chat Completions messages 数组

    Examples:
        >>> responses_input_to_messages("你好")
        [{"role": "user", "content": "你好"}]

        >>> responses_input_to_messages("你好", instructions="你是一个助手")
        [{"role": "system", "content": "你是一个助手"}, {"role": "user", "content": "你好"}]
    """
    messages = []

    # 1. 添加 instructions（如果有）
    if instructions:
        messages.append({
            "role": "system",
            "content": instructions
        })

    # 2. 处理 input
    if isinstance(input_data, str):
        # 字符串输入：直接作为 user message
        messages.append({
            "role": "user",
            "content": input_data
        })
    elif isinstance(input_data, list):
        # 数组输入：尝试兼容 Responses message 结构
        for item in input_data:
            if not isinstance(item, dict):
                # 忽略非字典元素
                continue

            # 提取 role 和 content
            role = item.get("role", "user")
            content = item.get("content")

            if content is None:
                continue

            # 处理 content
            if isinstance(content, str):
                # 字符串 content：直接使用
                messages.append({
                    "role": role,
                    "content": content
                })
            elif isinstance(content, list):
                # 数组 content：映射 Responses parts 到 Chat parts
                mapped_content = []
                for part in content:
                    if not isinstance(part, dict):
                        continue

                    part_type = part.get("type")

                    # 映射 Responses part types 到 Chat part types
                    if part_type == "input_text":
                        # input_text → text
                        mapped_content.append({
                            "type": "text",
                            "text": part.get("text", "")
                        })
                    elif part_type == "input_image":
                        # input_image → image_url
                        image_url = part.get("image_url")
                        if isinstance(image_url, str):
                            # 简单字符串 URL
                            mapped_content.append({
                                "type": "image_url",
                                "image_url": {"url": image_url}
                            })
                        elif isinstance(image_url, dict):
                            # 已经是 {url: ...} 格式
                            mapped_content.append({
                                "type": "image_url",
                                "image_url": image_url
                            })
                    elif part_type == "input_audio":
                        # input_audio → input_audio（保持不变）
                        mapped_content.append(part)
                    elif part_type == "file":
                        # file → file（保持不变）
                        mapped_content.append(part)
                    else:
                        # 未知 part type：尝试保持原样（兼容模式）
                        # 或者可以选择忽略/报错
                        mapped_content.append(part)

                if mapped_content:
                    messages.append({
                        "role": role,
                        "content": mapped_content
                    })
    else:
        # 既不是字符串也不是数组：抛出错误
        raise ValueError(
            f"Invalid input type: {type(input_data).__name__}. "
            "Must be a string or array."
        )

    return messages


def chat_completion_to_response(
    completion: Dict[str, Any],
    response_id: str = None
) -> Dict[str, Any]:
    """
    将 Chat Completion 对象转换为 Response 对象

    Args:
        completion: Chat Completion 对象（非流式）
        response_id: 可选的自定义 response ID

    Returns:
        Dict: Response 对象

    Example:
        >>> chat_completion = {
        ...     "id": "chatcmpl-xxx",
        ...     "object": "chat.completion",
        ...     "created": 1730000000,
        ...     "model": "grok-4",
        ...     "choices": [{
        ...         "index": 0,
        ...         "message": {
        ...             "role": "assistant",
        ...             "content": "你好！"
        ...         },
        ...         "finish_reason": "stop"
        ...     }],
        ...     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        ... }
        >>> result = chat_completion_to_response(chat_completion, "resp_123")
        >>> result["object"]
        'response'
        >>> result["output"][0]["type"]
        'message'
    """
    # 提取原始数据
    chat_id = completion.get("id", "")
    created = completion.get("created", 0)
    model = completion.get("model", "")
    choices = completion.get("choices", [])
    usage = completion.get("usage", {})

    # 生成 response_id
    if not response_id:
        # 将 chatcmpl- 前缀替换为 resp-
        if chat_id.startswith("chatcmpl-"):
            response_id = "resp-" + chat_id[9:]
        else:
            response_id = "resp-" + chat_id

    # 提取 assistant 消息内容
    text_content = ""
    if choices:
        first_choice = choices[0]
        message = first_choice.get("message", {})
        text_content = message.get("content", "")

    # 生成 message ID
    msg_id = f"msg-{response_id[5:]}" if response_id.startswith("resp-") else "msg-unknown"

    # 构建 Response 对象
    response = {
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
                        "text": text_content
                    }
                ]
            }
        ],
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0)
        },
        "output_text": text_content
    }

    return response


__all__ = [
    "coerce_stream",
    "responses_input_to_messages",
    "chat_completion_to_response",
]
