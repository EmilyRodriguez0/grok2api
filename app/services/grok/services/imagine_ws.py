"""
Grok Imagine WebSocket 生图服务
"""

import asyncio
import base64
import json
import re
import ssl
import time
import uuid
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional, Any

import aiofiles
import orjson

from app.core.config import get_config
from app.core.exceptions import UpstreamException
from app.core.logger import logger
from app.services.grok.utils.headers import apply_statsig, build_sso_cookie

try:
    import aiohttp
    from aiohttp_socks import ProxyConnector
except Exception:  # pragma: no cover
    aiohttp = None
    ProxyConnector = None


WS_API = "wss://grok.com/ws/imagine/listen"
TIMEOUT = 120.0
WS_HEARTBEAT = 20
WS_POLL_TIMEOUT = 5.0


class ImagineWSService:
    """WS 生图服务（用于 grok-imagine 模型）"""

    def __init__(self, proxy: str = None):
        self.proxy = proxy or get_config("grok.base_proxy_url", "")
        self.timeout = float(get_config("grok.timeout", TIMEOUT))
        self.app_url = str(get_config("app.app_url", "")).rstrip("/")
        self.image_dir = Path(__file__).parent.parent.parent.parent / "data" / "tmp" / "image"
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self._ssl_context = ssl.create_default_context()
        self._url_pattern = re.compile(r"/images/([a-f0-9-]+)\.(png|jpg|jpeg|webp)", re.IGNORECASE)

    def _build_headers(self, token: str) -> Dict[str, str]:
        headers = {
            "Origin": "https://grok.com",
            "Referer": "https://grok.com/imagine",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        apply_statsig(headers)
        headers["Cookie"] = build_sso_cookie(token, include_rw=True)
        return headers

    def _build_connector(self):
        if aiohttp is None:
            raise UpstreamException(
                "WebSocket image service unavailable: missing aiohttp dependency"
            )

        if self.proxy and ProxyConnector is not None:
            try:
                return ProxyConnector.from_url(self.proxy, ssl=self._ssl_context)
            except Exception as e:
                logger.warning(f"Invalid WS proxy '{self.proxy}', fallback to direct: {e}")

        return aiohttp.TCPConnector(ssl=self._ssl_context)

    def _extract_image_id(self, url: str) -> str:
        clean = str(url or "").strip()
        match = self._url_pattern.search(clean)
        if match:
            return match.group(1)
        return uuid.uuid4().hex

    @staticmethod
    def _stage_rank(stage: str) -> int:
        if stage == "final":
            return 3
        if stage == "medium":
            return 2
        return 1

    @staticmethod
    def _stage_progress(stage: str) -> int:
        if stage == "final":
            return 100
        if stage == "medium":
            return 66
        return 33

    @staticmethod
    def _classify_stage(url: str, blob_size: int) -> str:
        lower = str(url or "").lower()
        if (lower.endswith(".jpg") or lower.endswith(".jpeg")) and blob_size > 100000:
            return "final"
        if blob_size > 30000:
            return "medium"
        return "preview"

    def _build_message(
        self,
        prompt: str,
        enable_nsfw: Optional[bool],
        aspect_ratio: str,
    ) -> Dict[str, Any]:
        nsfw_enabled = True if enable_nsfw is None else bool(enable_nsfw)
        return {
            "type": "conversation.item.create",
            "timestamp": int(time.time() * 1000),
            "item": {
                "type": "message",
                "content": [
                    {
                        "requestId": str(uuid.uuid4()),
                        "text": prompt,
                        "type": "input_text",
                        "properties": {
                            "section_count": 0,
                            "is_kids_mode": not nsfw_enabled,
                            "enable_nsfw": nsfw_enabled,
                            "skip_upsampler": False,
                            "is_initial": False,
                            "aspect_ratio": aspect_ratio,
                        },
                    }
                ],
            },
        }

    def _public_url(self, filename: str) -> str:
        if self.app_url:
            return f"{self.app_url}/v1/files/image/{filename}"
        return f"/v1/files/image/{filename}"

    async def _save_image(self, image_id: str, blob: str, source_url: str, stage: str) -> tuple[str, str]:
        raw_b64 = str(blob or "")
        if "," in raw_b64:
            raw_b64 = raw_b64.split(",", 1)[1]

        data = base64.b64decode(raw_b64)
        ext = Path(str(source_url or "")).suffix.lower().lstrip(".")
        if not ext:
            ext = "jpg" if stage == "final" else "png"

        filename = f"{image_id}.{ext}"
        file_path = self.image_dir / filename

        async with aiofiles.open(file_path, "wb") as f:
            await f.write(data)

        return self._public_url(filename), raw_b64

    async def generate_events(
        self,
        token: str,
        prompt: str,
        aspect_ratio: str = "1:1",
        max_images: int = 1,
        enable_nsfw: Optional[bool] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """生成事件流（progress/result）"""
        max_images = max(1, int(max_images or 1))

        connector = self._build_connector()
        headers = self._build_headers(token)
        payload = self._build_message(prompt, enable_nsfw, aspect_ratio)

        image_states: Dict[str, Dict[str, Any]] = {}
        image_order: Dict[str, int] = {}
        final_ids: List[str] = []

        start_time = time.monotonic()

        try:
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.ws_connect(
                    WS_API,
                    headers=headers,
                    heartbeat=WS_HEARTBEAT,
                    receive_timeout=None,
                ) as ws:
                    await ws.send_json(payload)

                    while time.monotonic() - start_time < self.timeout:
                        try:
                            msg = await asyncio.wait_for(
                                ws.receive(), timeout=WS_POLL_TIMEOUT
                            )
                        except asyncio.TimeoutError:
                            if len(final_ids) >= max_images:
                                break
                            continue

                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                            except Exception:
                                continue

                            msg_type = data.get("type")
                            if msg_type == "image":
                                blob = data.get("blob", "")
                                url = str(data.get("url", "")).strip()
                                if not blob or not url:
                                    continue

                                image_id = self._extract_image_id(url)
                                blob_size = len(blob)
                                stage = self._classify_stage(url, blob_size)
                                rank = self._stage_rank(stage)

                                old_state = image_states.get(image_id)
                                old_rank = old_state["rank"] if old_state else 0
                                if rank < old_rank:
                                    continue

                                index = image_order.setdefault(image_id, len(image_order))
                                image_states[image_id] = {
                                    "image_id": image_id,
                                    "blob": blob,
                                    "url": url,
                                    "stage": stage,
                                    "rank": rank,
                                    "blob_size": blob_size,
                                    "index": index,
                                }

                                yield {
                                    "type": "progress",
                                    "index": index,
                                    "progress": self._stage_progress(stage),
                                }

                                if stage == "final" and image_id not in final_ids:
                                    final_ids.append(image_id)
                                    if len(final_ids) >= max_images:
                                        break

                            elif msg_type == "error":
                                err_msg = str(data.get("err_msg", "WS image generation failed"))
                                err_code = str(data.get("err_code", "upstream_error"))
                                yield {
                                    "type": "result",
                                    "success": False,
                                    "error": err_msg,
                                    "error_code": err_code,
                                }
                                return

                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break

                        if len(final_ids) >= max_images:
                            break

            selected: List[Dict[str, Any]] = []
            for image_id in final_ids:
                state = image_states.get(image_id)
                if state:
                    selected.append(state)
                if len(selected) >= max_images:
                    break

            if len(selected) < max_images:
                candidates = sorted(
                    [
                        state
                        for img_id, state in image_states.items()
                        if img_id not in final_ids
                    ],
                    key=lambda item: (item["rank"], item["blob_size"]),
                    reverse=True,
                )
                for state in candidates:
                    selected.append(state)
                    if len(selected) >= max_images:
                        break

            urls: List[str] = []
            b64_list: List[str] = []

            for state in selected:
                try:
                    saved_url, saved_b64 = await self._save_image(
                        image_id=state["image_id"],
                        blob=state["blob"],
                        source_url=state["url"],
                        stage=state["stage"],
                    )
                    urls.append(saved_url)
                    b64_list.append(saved_b64)
                except Exception as e:
                    logger.warning(f"WS image save failed: {e}")

            if urls:
                yield {
                    "type": "result",
                    "success": True,
                    "urls": urls,
                    "b64_list": b64_list,
                }
                return

            yield {
                "type": "result",
                "success": False,
                "error": "No image generated from WS",
                "error_code": "empty_result",
            }

        except UpstreamException:
            raise
        except Exception as e:
            logger.error(f"WS image generation failed: {e}")
            yield {
                "type": "result",
                "success": False,
                "error": str(e),
                "error_code": "ws_failed",
            }

    async def generate(
        self,
        token: str,
        prompt: str,
        aspect_ratio: str = "1:1",
        max_images: int = 1,
        enable_nsfw: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """一次性生成（非流式）"""
        async for item in self.generate_events(
            token=token,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            max_images=max_images,
            enable_nsfw=enable_nsfw,
        ):
            if item.get("type") == "result":
                return item

        return {
            "type": "result",
            "success": False,
            "error": "WS generation ended unexpectedly",
            "error_code": "unexpected_end",
        }

    async def stream_sse(
        self,
        token: str,
        prompt: str,
        aspect_ratio: str,
        max_images: int,
        enable_nsfw: Optional[bool],
        response_format: str,
    ) -> AsyncGenerator[str, None]:
        """输出与 ImageStreamProcessor 兼容的 SSE 事件"""
        if response_format == "url":
            response_field = "url"
        elif response_format == "base64":
            response_field = "base64"
        else:
            response_field = "b64_json"

        async for item in self.generate_events(
            token=token,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            max_images=max_images,
            enable_nsfw=enable_nsfw,
        ):
            if item.get("type") == "progress":
                event = {
                    "type": "image_generation.partial_image",
                    response_field: "",
                    "index": item.get("index", 0),
                    "progress": item.get("progress", 0),
                }
                yield f"event: image_generation.partial_image\ndata: {orjson.dumps(event).decode()}\n\n"
                continue

            if item.get("type") == "result":
                if not item.get("success"):
                    err_event = {
                        "type": "image_generation.error",
                        "error": item.get("error", "WS image generation failed"),
                        "code": item.get("error_code", "upstream_error"),
                    }
                    yield f"event: image_generation.error\ndata: {orjson.dumps(err_event).decode()}\n\n"
                    return

                values = item.get("urls", []) if response_format == "url" else item.get("b64_list", [])
                for idx, value in enumerate(values):
                    completed = {
                        "type": "image_generation.completed",
                        response_field: value,
                        "index": idx,
                        "usage": {
                            "total_tokens": 50,
                            "input_tokens": 25,
                            "output_tokens": 25,
                            "input_tokens_details": {
                                "text_tokens": 5,
                                "image_tokens": 20,
                            },
                        },
                    }
                    yield f"event: image_generation.completed\ndata: {orjson.dumps(completed).decode()}\n\n"
                return


__all__ = ["ImagineWSService"]
