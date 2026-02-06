"""
OpenAI 响应格式处理器
"""

import asyncio
import time
import uuid
import random
import orjson
from typing import Any, AsyncGenerator, Optional, AsyncIterable, List, TypeVar

from curl_cffi.requests.errors import RequestsError

from app.core.config import get_config
from app.core.logger import logger
from app.core.exceptions import UpstreamException
from app.services.grok.assets import DownloadService


def _is_http2_stream_error(e: Exception) -> bool:
    """检查是否为 HTTP/2 流错误"""
    err_str = str(e).lower()
    return "http/2" in err_str or "curl: (92)" in err_str or "stream" in err_str


T = TypeVar("T")


class StreamIdleTimeoutError(Exception):
    """流空闲超时错误"""

    def __init__(self, idle_seconds: float):
        self.idle_seconds = idle_seconds
        super().__init__(f"Stream idle timeout after {idle_seconds}s")


async def _with_idle_timeout(
    iterable: AsyncIterable[T], idle_timeout: float, model: str = ""
) -> AsyncGenerator[T, None]:
    """
    包装异步迭代器，添加空闲超时检测

    Args:
        iterable: 原始异步迭代器
        idle_timeout: 空闲超时时间(秒)，0 表示禁用
        model: 模型名称(用于日志)

    Yields:
        原始迭代器的元素

    Raises:
        StreamIdleTimeoutError: 当空闲超时时
    """
    if idle_timeout <= 0:
        async for item in iterable:
            yield item
        return

    iterator = iterable.__aiter__()
    while True:
        try:
            item = await asyncio.wait_for(iterator.__anext__(), timeout=idle_timeout)
            yield item
        except asyncio.TimeoutError:
            logger.warning(
                f"Stream idle timeout after {idle_timeout}s",
                extra={"model": model, "idle_timeout": idle_timeout},
            )
            raise StreamIdleTimeoutError(idle_timeout)
        except StopAsyncIteration:
            break


ASSET_URL = "https://assets.grok.com/"


class BaseProcessor:
    """基础处理器"""

    def __init__(self, model: str, token: str = ""):
        self.model = model
        self.token = token
        self.created = int(time.time())
        self.app_url = get_config("app.app_url", "")
        self._dl_service: Optional[DownloadService] = None

    def _get_dl(self) -> DownloadService:
        """获取下载服务实例（复用）"""
        if self._dl_service is None:
            self._dl_service = DownloadService()
        return self._dl_service

    async def close(self):
        """释放下载服务资源"""
        if self._dl_service:
            await self._dl_service.close()
            self._dl_service = None

    async def process_url(self, path: str, media_type: str = "image") -> str:
        """处理资产 URL"""
        # 处理可能的绝对路径
        if path.startswith("http"):
            from urllib.parse import urlparse

            path = urlparse(path).path

        if not path.startswith("/"):
            path = f"/{path}"

        if self.app_url:
            dl_service = self._get_dl()
            await dl_service.download(path, self.token, media_type)
            return f"{self.app_url.rstrip('/')}/v1/files/{media_type}{path}"
        else:
            return f"{ASSET_URL.rstrip('/')}{path}"

    @staticmethod
    def _normalize_chatcmpl_id(raw_id: str = "") -> str:
        """标准化 OpenAI Chat Completion ID 格式"""
        rid = str(raw_id or "").strip()
        if not rid:
            return f"chatcmpl-{uuid.uuid4().hex[:24]}"
        if rid.startswith("chatcmpl-"):
            return rid
        return f"chatcmpl-{rid}"

    @staticmethod
    def _is_valid_generated_url(url: str) -> bool:
        """校验上游返回的生成资源 URL 是否有效"""
        raw = str(url or "").strip()
        if not raw or raw == "/":
            return False
        if raw.startswith("http"):
            from urllib.parse import urlparse

            parsed_path = urlparse(raw).path
            return bool(parsed_path and parsed_path != "/")
        return True

    @staticmethod
    def _extract_image_id(url: str) -> str:
        """从资源 URL 中提取图片 ID"""
        raw = str(url or "").strip()
        if raw.startswith("http"):
            from urllib.parse import urlparse

            raw = urlparse(raw).path
        parts = [part for part in raw.split("/") if part]
        return parts[-2] if len(parts) >= 2 else "image"

    def _sse(self, content: str = "", role: str = None, finish: str = None) -> str:
        """构建 SSE 响应 (StreamProcessor 通用)"""
        if not hasattr(self, "response_id"):
            self.response_id = None
        if not hasattr(self, "fingerprint"):
            self.fingerprint = ""
        if not self.response_id:
            self.response_id = self._normalize_chatcmpl_id()

        delta = {}
        if role:
            delta["role"] = role
            delta["content"] = ""
        elif content:
            delta["content"] = content

        chunk = {
            "id": self.response_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": self.fingerprint
            if hasattr(self, "fingerprint")
            else "",
            "choices": [
                {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}
            ],
        }
        return f"data: {orjson.dumps(chunk).decode()}\n\n"


class StreamProcessor(BaseProcessor):
    """流式响应处理器"""

    def __init__(self, model: str, token: str = "", think: bool = None):
        super().__init__(model, token)
        self.response_id: Optional[str] = None
        self.fingerprint: str = ""
        self.think_opened: bool = False
        self.role_sent: bool = False
        self.filter_tags = get_config("grok.filter_tags", [])
        self.image_format = get_config("app.image_format", "url")
        # 用于过滤跨 token 的标签
        self._tag_buffer: str = ""
        self._in_filter_tag: bool = False
        self._emitted_text: str = ""
        self._dedupe_tail_limit: int = 8192
        self._final_token_sent: bool = False
        self._image_generation_seen: bool = False
        self._image_output_emitted: bool = False
        self._pending_image_tokens: List[str] = []

        if think is None:
            self.show_think = get_config("grok.thinking", False)
        else:
            self.show_think = think

    def _filter_token(self, token: str) -> str:
        """
        过滤 token 中的特殊标签（如 <grok:render>...</grok:render>）
        支持跨 token 的标签过滤
        """
        if not self.filter_tags:
            return token

        result = []
        i = 0
        while i < len(token):
            char = token[i]

            # 如果在过滤标签内
            if self._in_filter_tag:
                self._tag_buffer += char
                # 检查是否到达结束标签
                if char == ">":
                    # 检查是否是自闭合标签
                    if "/>" in self._tag_buffer:
                        self._in_filter_tag = False
                        self._tag_buffer = ""
                    else:
                        # 检查是否是结束标签 </{tag}>
                        for tag in self.filter_tags:
                            if f"</{tag}>" in self._tag_buffer:
                                self._in_filter_tag = False
                                self._tag_buffer = ""
                                break
                        # 如果不是结束标签，检查是否是开始标签结束（非自闭合）
                        # 继续等待结束标签
                i += 1
                continue

            # 检查是否开始一个过滤标签
            if char == "<":
                # 查看后续字符
                remaining = token[i:]
                tag_started = False
                for tag in self.filter_tags:
                    if remaining.startswith(f"<{tag}"):
                        tag_started = True
                        break
                    # 部分匹配（可能跨 token）
                    if len(remaining) < len(tag) + 1:
                        for j in range(1, len(remaining) + 1):
                            if f"<{tag}".startswith(remaining[:j]):
                                tag_started = True
                                break

                if tag_started:
                    self._in_filter_tag = True
                    self._tag_buffer = char
                    i += 1
                    continue

            result.append(char)
            i += 1

        return "".join(result)

    def _is_replayed_token(self, token: str) -> bool:
        """检测上游回放的整段 token，避免末尾重复输出"""
        if not token or not self._emitted_text:
            return False

        normalized = token.rstrip("\r\n")
        if len(normalized) < 12:
            return False

        return self._emitted_text.rstrip("\r\n").endswith(normalized)

    def _record_emitted_text(self, text: str) -> None:
        """记录已输出正文，用于回放去重"""
        if not text:
            return
        self._emitted_text += text
        if len(self._emitted_text) > self._dedupe_tail_limit:
            self._emitted_text = self._emitted_text[-self._dedupe_tail_limit :]

    async def process(
        self, response: AsyncIterable[bytes]
    ) -> AsyncGenerator[str, None]:
        """处理流式响应"""
        # 获取空闲超时配置
        idle_timeout = get_config("grok.stream_idle_timeout", 45.0)

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                # 元数据
                if (llm := resp.get("llmInfo")) and not self.fingerprint:
                    self.fingerprint = llm.get("modelHash", "")
                if rid := resp.get("responseId"):
                    if not self.role_sent:
                        self.response_id = self._normalize_chatcmpl_id(rid)

                # 首次发送 role
                if not self.role_sent:
                    yield self._sse(role="assistant")
                    self.role_sent = True

                # 图像生成进度
                if img := resp.get("streamingImageGenerationResponse"):
                    self._image_generation_seen = True
                    if self.show_think:
                        if not self.think_opened:
                            yield self._sse("<think>\n")
                            self.think_opened = True
                        idx = img.get("imageIndex", 0) + 1
                        progress = img.get("progress", 0)
                        yield self._sse(
                            f"正在生成第{idx}张图片中，当前进度{progress}%\n"
                        )
                    continue

                # modelResponse
                if mr := resp.get("modelResponse"):
                    if self.think_opened and self.show_think:
                        yield self._sse("</think>\n")
                        self.think_opened = False

                    # 处理生成的图片
                    emitted_images = 0
                    for url in mr.get("generatedImageUrls", []):
                        clean_url = str(url or "").strip()
                        if not self._is_valid_generated_url(clean_url):
                            logger.warning(
                                "Skip invalid generated image url",
                                extra={"model": self.model, "url": clean_url},
                            )
                            continue

                        img_id = self._extract_image_id(clean_url)

                        if self.image_format == "base64":
                            dl_service = self._get_dl()
                            base64_data = await dl_service.to_base64(
                                clean_url, self.token, "image"
                            )
                            if base64_data:
                                yield self._sse(f"![{img_id}]({base64_data})\n")
                            else:
                                final_url = await self.process_url(clean_url, "image")
                                yield self._sse(f"![{img_id}]({final_url})\n")
                        else:
                            final_url = await self.process_url(clean_url, "image")
                            yield self._sse(f"![{img_id}]({final_url})\n")
                        emitted_images += 1

                    if emitted_images > 0:
                        self._image_output_emitted = True
                        self._pending_image_tokens.clear()
                    elif self._pending_image_tokens and not self._final_token_sent:
                        for pending_token in self._pending_image_tokens:
                            if self._is_replayed_token(pending_token):
                                continue
                            yield self._sse(pending_token)
                            self._record_emitted_text(pending_token)
                            self._final_token_sent = True
                        self._pending_image_tokens.clear()

                    # 文本流优先使用 token 增量；若上游未提供 final token，且无图片产出时才回退 modelResponse.message
                    if not self._final_token_sent and emitted_images == 0:
                        if msg := mr.get("message"):
                            filtered_msg = self._filter_token(msg)
                            if filtered_msg and not self._is_replayed_token(filtered_msg):
                                yield self._sse(filtered_msg)
                                self._record_emitted_text(filtered_msg)
                                self._final_token_sent = True

                    if (
                        (meta := mr.get("metadata", {}))
                        .get("llm_info", {})
                        .get("modelHash")
                    ):
                        self.fingerprint = meta["llm_info"]["modelHash"]
                    continue

                # 普通 token
                if (token := resp.get("token")) is not None:
                    if token:
                        message_tag = str(resp.get("messageTag", "")).lower()
                        is_reasoning = bool(resp.get("isThinking")) or message_tag in {
                            "header",
                            "summary",
                        }

                        filtered = self._filter_token(token)
                        if filtered:
                            if is_reasoning:
                                if self.show_think:
                                    if not self.think_opened:
                                        yield self._sse("<think>\n")
                                        self.think_opened = True
                                    yield self._sse(filtered)
                                continue

                            if self.think_opened and self.show_think:
                                yield self._sse("</think>\n")
                                self.think_opened = False

                            if (
                                self._image_generation_seen
                                and not self._image_output_emitted
                            ):
                                self._pending_image_tokens.append(filtered)
                                continue

                            if self._is_replayed_token(filtered):
                                logger.debug(
                                    "Skip replayed token chunk",
                                    extra={
                                        "model": self.model,
                                        "token_preview": filtered[:80],
                                    },
                                )
                                continue
                            yield self._sse(filtered)
                            self._record_emitted_text(filtered)
                            self._final_token_sent = True

            if (
                self._pending_image_tokens
                and not self._image_output_emitted
                and not self._final_token_sent
            ):
                for pending_token in self._pending_image_tokens:
                    if self._is_replayed_token(pending_token):
                        continue
                    yield self._sse(pending_token)
                    self._record_emitted_text(pending_token)
                    self._final_token_sent = True
                self._pending_image_tokens.clear()

            if self.think_opened:
                yield self._sse("</think>\n")
            yield self._sse(finish="stop")
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            # 客户端断开连接，静默处理
            logger.debug("Stream cancelled by client", extra={"model": self.model})
        except StreamIdleTimeoutError as e:
            # 流空闲超时
            raise UpstreamException(
                message=f"Stream idle timeout after {e.idle_seconds}s",
                status_code=504,
                details={
                    "error": str(e),
                    "type": "stream_idle_timeout",
                    "idle_seconds": e.idle_seconds,
                },
            )
        except RequestsError as e:
            # HTTP/2 流错误转换为 UpstreamException
            if _is_http2_stream_error(e):
                logger.warning(f"HTTP/2 stream error: {e}", extra={"model": self.model})
                raise UpstreamException(
                    message="Upstream connection closed unexpectedly",
                    status_code=502,
                    details={"error": str(e), "type": "http2_stream_error"},
                )
            logger.error(f"Stream request error: {e}", extra={"model": self.model})
            raise UpstreamException(
                message=f"Upstream request failed: {e}",
                status_code=502,
                details={"error": str(e)},
            )
        except Exception as e:
            logger.error(
                f"Stream processing error: {e}",
                extra={"model": self.model, "error_type": type(e).__name__},
            )
            raise
        finally:
            await self.close()


class CollectProcessor(BaseProcessor):
    """非流式响应处理器"""

    # 需要过滤的标签
    FILTER_TAGS = ["grok:render", "xaiartifact", "xai:tool_usage_card"]

    def __init__(self, model: str, token: str = ""):
        super().__init__(model, token)
        self.image_format = get_config("app.image_format", "url")
        self.filter_tags = get_config("grok.filter_tags", self.FILTER_TAGS)

    def _filter_content(self, content: str) -> str:
        """过滤内容中的特殊标签"""
        import re

        if not content or not self.filter_tags:
            return content

        result = content
        for tag in self.filter_tags:
            # 匹配 <tag ...>...</tag> 或 <tag ... />，re.DOTALL 使 . 匹配换行符
            pattern = rf"<{re.escape(tag)}[^>]*>.*?</{re.escape(tag)}>|<{re.escape(tag)}[^>]*/>"
            result = re.sub(pattern, "", result, flags=re.DOTALL)

        return result

    async def process(self, response: AsyncIterable[bytes]) -> dict[str, Any]:
        """处理并收集完整响应"""
        response_id = ""
        fingerprint = ""
        content = ""
        # 获取空闲超时配置
        idle_timeout = get_config("grok.stream_idle_timeout", 45.0)

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                if (llm := resp.get("llmInfo")) and not fingerprint:
                    fingerprint = llm.get("modelHash", "")

                if mr := resp.get("modelResponse"):
                    response_id = self._normalize_chatcmpl_id(
                        mr.get("responseId", response_id)
                    )
                    content = mr.get("message", "")

                    if urls := mr.get("generatedImageUrls"):
                        image_contents: List[str] = []
                        for url in urls:
                            clean_url = str(url or "").strip()
                            if not self._is_valid_generated_url(clean_url):
                                logger.warning(
                                    "Skip invalid generated image url in collect",
                                    extra={"model": self.model, "url": clean_url},
                                )
                                continue

                            img_id = self._extract_image_id(clean_url)

                            if self.image_format == "base64":
                                dl_service = self._get_dl()
                                base64_data = await dl_service.to_base64(
                                    clean_url, self.token, "image"
                                )
                                if base64_data:
                                    image_contents.append(f"![{img_id}]({base64_data})\n")
                                else:
                                    final_url = await self.process_url(clean_url, "image")
                                    image_contents.append(f"![{img_id}]({final_url})\n")
                            else:
                                final_url = await self.process_url(clean_url, "image")
                                image_contents.append(f"![{img_id}]({final_url})\n")

                        if image_contents:
                            # 图片结果优先，避免输出类似 "I generated images with the prompt..." 的冗余正文
                            content = "".join(image_contents)

                    if (
                        (meta := mr.get("metadata", {}))
                        .get("llm_info", {})
                        .get("modelHash")
                    ):
                        fingerprint = meta["llm_info"]["modelHash"]

        except asyncio.CancelledError:
            logger.debug("Collect cancelled by client", extra={"model": self.model})
        except StreamIdleTimeoutError as e:
            logger.warning(f"Collect idle timeout: {e}", extra={"model": self.model})
            # 非流式模式下，超时后返回已收集的内容
        except RequestsError as e:
            if _is_http2_stream_error(e):
                logger.warning(
                    f"HTTP/2 stream error in collect: {e}", extra={"model": self.model}
                )
            else:
                logger.error(f"Collect request error: {e}", extra={"model": self.model})
        except Exception as e:
            logger.error(
                f"Collect processing error: {e}",
                extra={"model": self.model, "error_type": type(e).__name__},
            )
        finally:
            await self.close()

        # 过滤特殊标签
        content = self._filter_content(content)

        return {
            "id": self._normalize_chatcmpl_id(response_id),
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "system_fingerprint": fingerprint,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "refusal": None,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


class VideoStreamProcessor(BaseProcessor):
    """视频流式响应处理器"""

    def __init__(self, model: str, token: str = "", think: bool = None):
        super().__init__(model, token)
        self.response_id: Optional[str] = None
        self.think_opened: bool = False
        self.role_sent: bool = False
        self.video_format = str(get_config("app.video_format", "html")).lower()

        if think is None:
            self.show_think = get_config("grok.thinking", False)
        else:
            self.show_think = think

    def _build_video_html(self, video_url: str, thumbnail_url: str = "") -> str:
        """构建视频 HTML 标签"""
        import html

        safe_video_url = html.escape(video_url)
        safe_thumbnail_url = html.escape(thumbnail_url)
        poster_attr = f' poster="{safe_thumbnail_url}"' if safe_thumbnail_url else ""
        return f'''<video id="video" controls="" preload="none"{poster_attr}>
  <source id="mp4" src="{safe_video_url}" type="video/mp4">
</video>'''

    async def process(
        self, response: AsyncIterable[bytes]
    ) -> AsyncGenerator[str, None]:
        """处理视频流式响应"""
        # 视频生成使用更长的空闲超时
        idle_timeout = get_config("grok.video_idle_timeout", 90.0)

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                if rid := resp.get("responseId"):
                    self.response_id = self._normalize_chatcmpl_id(rid)

                # 首次发送 role
                if not self.role_sent:
                    yield self._sse(role="assistant")
                    self.role_sent = True

                # 视频生成进度
                if video_resp := resp.get("streamingVideoGenerationResponse"):
                    progress = video_resp.get("progress", 0)

                    if self.show_think:
                        if not self.think_opened:
                            yield self._sse("<think>\n")
                            self.think_opened = True
                        yield self._sse(f"正在生成视频中，当前进度{progress}%\n")

                    if progress == 100:
                        video_url = video_resp.get("videoUrl", "")
                        thumbnail_url = video_resp.get("thumbnailImageUrl", "")

                        if self.think_opened and self.show_think:
                            yield self._sse("</think>\n")
                            self.think_opened = False

                        if video_url:
                            final_video_url = await self.process_url(video_url, "video")
                            final_thumbnail_url = ""
                            if thumbnail_url:
                                final_thumbnail_url = await self.process_url(
                                    thumbnail_url, "image"
                                )

                            if self.video_format == "url":
                                yield self._sse(final_video_url)
                            else:
                                video_html = self._build_video_html(
                                    final_video_url, final_thumbnail_url
                                )
                                yield self._sse(video_html)

                            logger.info(f"Video generated: {video_url}")
                    continue

            if self.think_opened:
                yield self._sse("</think>\n")
            yield self._sse(finish="stop")
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            logger.debug(
                "Video stream cancelled by client", extra={"model": self.model}
            )
        except StreamIdleTimeoutError as e:
            raise UpstreamException(
                message=f"Video stream idle timeout after {e.idle_seconds}s",
                status_code=504,
                details={
                    "error": str(e),
                    "type": "stream_idle_timeout",
                    "idle_seconds": e.idle_seconds,
                },
            )
        except RequestsError as e:
            if _is_http2_stream_error(e):
                logger.warning(
                    f"HTTP/2 stream error in video: {e}", extra={"model": self.model}
                )
                raise UpstreamException(
                    message="Upstream connection closed unexpectedly",
                    status_code=502,
                    details={"error": str(e), "type": "http2_stream_error"},
                )
            logger.error(
                f"Video stream request error: {e}", extra={"model": self.model}
            )
            raise UpstreamException(
                message=f"Upstream request failed: {e}",
                status_code=502,
                details={"error": str(e)},
            )
        except Exception as e:
            logger.error(
                f"Video stream processing error: {e}",
                extra={"model": self.model, "error_type": type(e).__name__},
            )
        finally:
            await self.close()


class VideoCollectProcessor(BaseProcessor):
    """视频非流式响应处理器"""

    def __init__(self, model: str, token: str = ""):
        super().__init__(model, token)
        self.video_format = str(get_config("app.video_format", "html")).lower()

    def _build_video_html(self, video_url: str, thumbnail_url: str = "") -> str:
        poster_attr = f' poster="{thumbnail_url}"' if thumbnail_url else ""
        return f'''<video id="video" controls="" preload="none"{poster_attr}>
  <source id="mp4" src="{video_url}" type="video/mp4">
</video>'''

    async def process(self, response: AsyncIterable[bytes]) -> dict[str, Any]:
        """处理并收集视频响应"""
        response_id = ""
        content = ""
        # 视频生成使用更长的空闲超时
        idle_timeout = get_config("grok.video_idle_timeout", 90.0)

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                if video_resp := resp.get("streamingVideoGenerationResponse"):
                    if video_resp.get("progress") == 100:
                        response_id = self._normalize_chatcmpl_id(
                            resp.get("responseId", response_id)
                        )
                        video_url = video_resp.get("videoUrl", "")
                        thumbnail_url = video_resp.get("thumbnailImageUrl", "")

                        if video_url:
                            final_video_url = await self.process_url(video_url, "video")
                            final_thumbnail_url = ""
                            if thumbnail_url:
                                final_thumbnail_url = await self.process_url(
                                    thumbnail_url, "image"
                                )

                            if self.video_format == "url":
                                content = final_video_url
                            else:
                                content = self._build_video_html(
                                    final_video_url, final_thumbnail_url
                                )
                            logger.info(f"Video generated: {video_url}")

        except asyncio.CancelledError:
            logger.debug(
                "Video collect cancelled by client", extra={"model": self.model}
            )
        except StreamIdleTimeoutError as e:
            logger.warning(
                f"Video collect idle timeout: {e}", extra={"model": self.model}
            )
        except RequestsError as e:
            if _is_http2_stream_error(e):
                logger.warning(
                    f"HTTP/2 stream error in video collect: {e}",
                    extra={"model": self.model},
                )
            else:
                logger.error(
                    f"Video collect request error: {e}", extra={"model": self.model}
                )
        except Exception as e:
            logger.error(
                f"Video collect processing error: {e}",
                extra={"model": self.model, "error_type": type(e).__name__},
            )
        finally:
            await self.close()

        return {
            "id": self._normalize_chatcmpl_id(response_id),
            "object": "chat.completion",
            "created": self.created,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "refusal": None,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }


class ImageStreamProcessor(BaseProcessor):
    """图片生成流式响应处理器"""

    def __init__(self, model: str, token: str = "", n: int = 1):
        super().__init__(model, token)
        self.partial_index = 0
        self.n = n
        self.target_index = random.randint(0, 1) if n == 1 else None

    def _sse(self, event: str, data: dict) -> str:
        """构建 SSE 响应 (覆盖基类)"""
        return f"event: {event}\ndata: {orjson.dumps(data).decode()}\n\n"

    async def process(
        self, response: AsyncIterable[bytes]
    ) -> AsyncGenerator[str, None]:
        """处理流式响应"""
        final_images = []
        # 图片生成使用标准空闲超时
        idle_timeout = get_config("grok.stream_idle_timeout", 45.0)

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                # 图片生成进度
                if img := resp.get("streamingImageGenerationResponse"):
                    image_index = img.get("imageIndex", 0)
                    progress = img.get("progress", 0)

                    if self.n == 1 and image_index != self.target_index:
                        continue

                    out_index = 0 if self.n == 1 else image_index

                    yield self._sse(
                        "image_generation.partial_image",
                        {
                            "type": "image_generation.partial_image",
                            "b64_json": "",
                            "index": out_index,
                            "progress": progress,
                        },
                    )
                    continue

                # modelResponse
                if mr := resp.get("modelResponse"):
                    if urls := mr.get("generatedImageUrls"):
                        for url in urls:
                            clean_url = str(url or "").strip()
                            if not self._is_valid_generated_url(clean_url):
                                logger.warning(
                                    "Skip invalid generated image url in image stream",
                                    extra={"model": self.model, "url": clean_url},
                                )
                                continue

                            dl_service = self._get_dl()
                            base64_data = await dl_service.to_base64(
                                clean_url, self.token, "image"
                            )
                            if base64_data:
                                if "," in base64_data:
                                    b64 = base64_data.split(",", 1)[1]
                                else:
                                    b64 = base64_data
                                final_images.append(b64)
                    continue

            for index, b64 in enumerate(final_images):
                if self.n == 1:
                    if index != self.target_index:
                        continue
                    out_index = 0
                else:
                    out_index = index

                yield self._sse(
                    "image_generation.completed",
                    {
                        "type": "image_generation.completed",
                        "b64_json": b64,
                        "index": out_index,
                        "usage": {
                            "total_tokens": 50,
                            "input_tokens": 25,
                            "output_tokens": 25,
                            "input_tokens_details": {
                                "text_tokens": 5,
                                "image_tokens": 20,
                            },
                        },
                    },
                )
        except asyncio.CancelledError:
            logger.debug("Image stream cancelled by client")
        except StreamIdleTimeoutError as e:
            raise UpstreamException(
                message=f"Image stream idle timeout after {e.idle_seconds}s",
                status_code=504,
                details={
                    "error": str(e),
                    "type": "stream_idle_timeout",
                    "idle_seconds": e.idle_seconds,
                },
            )
        except RequestsError as e:
            if _is_http2_stream_error(e):
                logger.warning(f"HTTP/2 stream error in image: {e}")
                raise UpstreamException(
                    message="Upstream connection closed unexpectedly",
                    status_code=502,
                    details={"error": str(e), "type": "http2_stream_error"},
                )
            logger.error(f"Image stream request error: {e}")
            raise UpstreamException(
                message=f"Upstream request failed: {e}",
                status_code=502,
                details={"error": str(e)},
            )
        except Exception as e:
            logger.error(
                f"Image stream processing error: {e}",
                extra={"error_type": type(e).__name__},
            )
            raise
        finally:
            await self.close()


class ImageCollectProcessor(BaseProcessor):
    """图片生成非流式响应处理器"""

    def __init__(self, model: str, token: str = ""):
        super().__init__(model, token)

    async def process(self, response: AsyncIterable[bytes]) -> List[str]:
        """处理并收集图片"""
        images = []
        # 图片生成使用标准空闲超时
        idle_timeout = get_config("grok.stream_idle_timeout", 45.0)

        try:
            async for line in _with_idle_timeout(response, idle_timeout, self.model):
                if not line:
                    continue
                try:
                    data = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue

                resp = data.get("result", {}).get("response", {})

                if mr := resp.get("modelResponse"):
                    if urls := mr.get("generatedImageUrls"):
                        for url in urls:
                            clean_url = str(url or "").strip()
                            if not self._is_valid_generated_url(clean_url):
                                logger.warning(
                                    "Skip invalid generated image url in image collect",
                                    extra={"model": self.model, "url": clean_url},
                                )
                                continue

                            dl_service = self._get_dl()
                            base64_data = await dl_service.to_base64(
                                clean_url, self.token, "image"
                            )
                            if base64_data:
                                if "," in base64_data:
                                    b64 = base64_data.split(",", 1)[1]
                                else:
                                    b64 = base64_data
                                images.append(b64)

        except asyncio.CancelledError:
            logger.debug("Image collect cancelled by client")
        except StreamIdleTimeoutError as e:
            logger.warning(f"Image collect idle timeout: {e}")
        except RequestsError as e:
            if _is_http2_stream_error(e):
                logger.warning(f"HTTP/2 stream error in image collect: {e}")
            else:
                logger.error(f"Image collect request error: {e}")
        except Exception as e:
            logger.error(
                f"Image collect processing error: {e}",
                extra={"error_type": type(e).__name__},
            )
        finally:
            await self.close()

        return images


__all__ = [
    "StreamProcessor",
    "CollectProcessor",
    "VideoStreamProcessor",
    "VideoCollectProcessor",
    "ImageStreamProcessor",
    "ImageCollectProcessor",
]
