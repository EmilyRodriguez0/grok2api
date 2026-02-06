import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path


def _install_stubs():
    orjson_stub = types.ModuleType("orjson")
    orjson_stub.dumps = lambda obj: json.dumps(obj, ensure_ascii=False).encode("utf-8")
    orjson_stub.loads = lambda b: json.loads(
        b.decode("utf-8") if isinstance(b, (bytes, bytearray)) else b
    )
    orjson_stub.JSONDecodeError = json.JSONDecodeError
    sys.modules["orjson"] = orjson_stub

    curl_mod = types.ModuleType("curl_cffi")
    curl_req_mod = types.ModuleType("curl_cffi.requests")
    curl_err_mod = types.ModuleType("curl_cffi.requests.errors")

    class RequestsError(Exception):
        pass

    curl_err_mod.RequestsError = RequestsError
    curl_req_mod.errors = curl_err_mod
    curl_mod.requests = curl_req_mod
    sys.modules["curl_cffi"] = curl_mod
    sys.modules["curl_cffi.requests"] = curl_req_mod
    sys.modules["curl_cffi.requests.errors"] = curl_err_mod

    config_mod = types.ModuleType("app.core.config")
    config_mod.get_config = lambda key, default=None: default
    sys.modules["app.core.config"] = config_mod

    class _Logger:
        def debug(self, *args, **kwargs):
            pass

        info = warning = error = debug

    logger_mod = types.ModuleType("app.core.logger")
    logger_mod.logger = _Logger()
    sys.modules["app.core.logger"] = logger_mod

    class UpstreamException(Exception):
        def __init__(self, message="", status_code=502, details=None):
            self.status_code = status_code
            self.details = details
            super().__init__(message)

    exceptions_mod = types.ModuleType("app.core.exceptions")
    exceptions_mod.UpstreamException = UpstreamException
    sys.modules["app.core.exceptions"] = exceptions_mod

    class DownloadService:
        async def close(self):
            return None

        async def download(self, *args, **kwargs):
            return None

        async def to_base64(self, *args, **kwargs):
            return None

    assets_mod = types.ModuleType("app.services.grok.assets")
    assets_mod.DownloadService = DownloadService
    sys.modules["app.services.grok.assets"] = assets_mod


def _load_processor_module():
    _install_stubs()
    file_path = Path(__file__).resolve().parents[1] / "app/services/grok/processor.py"
    spec = importlib.util.spec_from_file_location("processor_under_test", str(file_path))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _make_stream(events):
    async def _gen():
        for event in events:
            yield json.dumps(event, ensure_ascii=False).encode("utf-8")

    return _gen()


def _parse_sse_output(lines):
    ids, contents = [], []
    finish_count, done_count = 0, 0
    for line in lines:
        if line.strip() == "data: [DONE]":
            done_count += 1
            continue
        if not line.startswith("data: "):
            continue
        chunk = json.loads(line[6:].strip())
        ids.append(chunk.get("id"))
        choice = chunk.get("choices", [{}])[0]
        if choice.get("finish_reason"):
            finish_count += 1
        content = choice.get("delta", {}).get("content")
        if content:
            contents.append(content)
    return {
        "ids": ids,
        "contents": contents,
        "finish_count": finish_count,
        "done_count": done_count,
    }


async def _collect(module, events, think=True, app_url=None):
    processor = module.StreamProcessor("grok-4.1-thinking", "", think=think)
    if app_url is not None:
        processor.app_url = app_url
    lines = []
    async for item in processor.process(_make_stream(events)):
        lines.append(item)
    return _parse_sse_output(lines)


async def _collect_non_stream(module, events, app_url=None):
    processor = module.CollectProcessor("grok-4.1-thinking", "")
    if app_url is not None:
        processor.app_url = app_url
    return await processor.process(_make_stream(events))


async def main():
    module = _load_processor_module()
    assert module.BaseProcessor._normalize_chatcmpl_id("abc") == "chatcmpl-abc"
    assert module.BaseProcessor._normalize_chatcmpl_id("chatcmpl-xyz") == "chatcmpl-xyz"
    assert module.BaseProcessor._normalize_chatcmpl_id().startswith("chatcmpl-")
    replay_text = "你好！我是Grok，有什麼可以幫你的？"

    replay_case = await _collect(
        module,
        [{"result": {"response": {"token": ch}}} for ch in replay_text]
        + [{"result": {"response": {"token": replay_text}}}],
    )
    assert "".join(replay_case["contents"]) == replay_text, replay_case
    assert replay_case["finish_count"] == 1, replay_case
    assert replay_case["done_count"] == 1, replay_case
    assert len(set(replay_case["ids"])) == 1, replay_case

    reasoning_case = await _collect(
        module,
        [
            {
                "result": {
                    "response": {
                        "token": "Thinking about user request",
                        "isThinking": True,
                        "messageTag": "header",
                    }
                }
            },
            {
                "result": {
                    "response": {
                        "token": "Step summary",
                        "isThinking": True,
                        "messageTag": "summary",
                    }
                }
            },
            {
                "result": {
                    "response": {
                        "token": "你",
                        "isThinking": False,
                        "messageTag": "final",
                    }
                }
            },
            {
                "result": {
                    "response": {
                        "token": "好",
                        "isThinking": False,
                        "messageTag": "final",
                    }
                }
            },
        ],
    )
    assert reasoning_case["contents"] == [
        "<think>\n",
        "Thinking about user request",
        "Step summary",
        "</think>\n",
        "你",
        "好",
    ], reasoning_case

    reasoning_hidden_case = await _collect(
        module,
        [
            {
                "result": {
                    "response": {
                        "token": "Thinking hidden",
                        "isThinking": True,
                        "messageTag": "header",
                    }
                }
            },
            {"result": {"response": {"token": "A", "isThinking": False}}},
        ],
        think=False,
    )
    assert reasoning_hidden_case["contents"] == ["A"], reasoning_hidden_case

    repeat_short_case = await _collect(
        module,
        [
            {"result": {"response": {"token": "ha"}}},
            {"result": {"response": {"token": "ha"}}},
        ],
    )
    assert repeat_short_case["contents"] == ["ha", "ha"], repeat_short_case

    late_response_id_case = await _collect(
        module,
        [
            {"result": {"response": {"token": "A"}}},
            {"result": {"response": {"responseId": "rid-late", "token": "B"}}},
        ],
    )
    first_id = late_response_id_case["ids"][0]
    assert first_id.startswith("chatcmpl-"), late_response_id_case
    assert len(set(late_response_id_case["ids"])) == 1, late_response_id_case

    model_response_fallback_case = await _collect(
        module,
        [
            {
                "result": {
                    "response": {
                        "token": "思考中",
                        "isThinking": True,
                        "messageTag": "header",
                    }
                }
            },
            {
                "result": {
                    "response": {
                        "modelResponse": {
                            "message": "最终回答",
                            "generatedImageUrls": [],
                        }
                    }
                }
            },
        ],
    )
    assert model_response_fallback_case["contents"] == [
        "<think>\n",
        "思考中",
        "</think>\n",
        "最终回答",
    ], model_response_fallback_case

    model_response_no_duplicate_case = await _collect(
        module,
        [
            {"result": {"response": {"token": "你", "isThinking": False}}},
            {"result": {"response": {"token": "好", "isThinking": False}}},
            {
                "result": {
                    "response": {
                        "modelResponse": {
                            "message": "你好",
                            "generatedImageUrls": [],
                        }
                    }
                }
            },
        ],
    )
    assert model_response_no_duplicate_case["contents"] == ["你", "好"], (
        model_response_no_duplicate_case
    )

    empty_first_image_case = await _collect(
        module,
        [
            {
                "result": {
                    "response": {
                        "modelResponse": {
                            "generatedImageUrls": [
                                "",
                                "/users/test/generated/real-image-id/image.jpg",
                            ]
                        }
                    }
                }
            }
        ],
        app_url="https://grok.testdomain.xyz",
    )
    assert any(
        "real-image-id/image.jpg" in content
        for content in empty_first_image_case["contents"]
    ), empty_first_image_case
    assert all(
        "v1/files/image/)" not in content
        for content in empty_first_image_case["contents"]
    ), empty_first_image_case

    collect_empty_image_case = await _collect_non_stream(
        module,
        [
            {
                "result": {
                    "response": {
                        "modelResponse": {
                            "responseId": "rid",
                            "message": "done",
                            "generatedImageUrls": [
                                "",
                                "/users/test/generated/collect-image-id/image.jpg",
                            ],
                        }
                    }
                }
            }
        ],
        app_url="https://grok.testdomain.xyz",
    )
    collect_content = collect_empty_image_case["choices"][0]["message"]["content"]
    assert "collect-image-id/image.jpg" in collect_content, collect_empty_image_case
    assert "v1/files/image/)" not in collect_content, collect_empty_image_case

    image_message_suppressed_case = await _collect(
        module,
        [
            {
                "result": {
                    "response": {
                        "modelResponse": {
                            "message": "I generated images with the prompt: cute kitten",
                            "generatedImageUrls": [
                                "/users/test/generated/a1/image.jpg",
                                "/users/test/generated/a2/image.jpg",
                            ],
                        }
                    }
                }
            }
        ],
        app_url="https://grok.testdomain.xyz",
    )
    assert all(
        "I generated images with the prompt" not in content
        for content in image_message_suppressed_case["contents"]
    ), image_message_suppressed_case
    assert any(
        "generated/a1/image.jpg" in content
        for content in image_message_suppressed_case["contents"]
    ), image_message_suppressed_case
    assert any(
        "generated/a2/image.jpg" in content
        for content in image_message_suppressed_case["contents"]
    ), image_message_suppressed_case

    collect_image_message_suppressed_case = await _collect_non_stream(
        module,
        [
            {
                "result": {
                    "response": {
                        "modelResponse": {
                            "responseId": "rid2",
                            "message": "I generated images with the prompt: cute kitten",
                            "generatedImageUrls": [
                                "/users/test/generated/b1/image.jpg",
                                "/users/test/generated/b2/image.jpg",
                            ],
                        }
                    }
                }
            }
        ],
        app_url="https://grok.testdomain.xyz",
    )
    collect_image_content = collect_image_message_suppressed_case["choices"][0][
        "message"
    ]["content"]
    assert "I generated images with the prompt" not in collect_image_content, (
        collect_image_message_suppressed_case
    )
    assert "generated/b1/image.jpg" in collect_image_content, (
        collect_image_message_suppressed_case
    )
    assert "generated/b2/image.jpg" in collect_image_content, (
        collect_image_message_suppressed_case
    )

    long_non_suffix_case = await _collect(
        module,
        [
            {"result": {"response": {"token": "这是一个比较长的片段内容"}}},
            {"result": {"response": {"token": "尾巴"}}},
            {"result": {"response": {"token": "这是一个比较长的片段内容"}}},
        ],
    )
    assert long_non_suffix_case["contents"] == [
        "这是一个比较长的片段内容",
        "尾巴",
        "这是一个比较长的片段内容",
    ], long_non_suffix_case

    print("PASS: stream dedupe and thinking routing checks")


if __name__ == "__main__":
    asyncio.run(main())
