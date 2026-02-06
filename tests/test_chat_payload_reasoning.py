import importlib.util
import json
import sys
import types
from pathlib import Path


def _install_stubs(config_values):
    orjson_stub = types.ModuleType("orjson")
    orjson_stub.dumps = lambda obj: json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.modules["orjson"] = orjson_stub

    curl_mod = types.ModuleType("curl_cffi")
    curl_req_mod = types.ModuleType("curl_cffi.requests")

    class AsyncSession:
        def __init__(self, *args, **kwargs):
            pass

    curl_req_mod.AsyncSession = AsyncSession
    curl_mod.requests = curl_req_mod
    sys.modules["curl_cffi"] = curl_mod
    sys.modules["curl_cffi.requests"] = curl_req_mod

    class _Logger:
        def debug(self, *args, **kwargs):
            pass

        info = warning = error = debug

    logger_mod = types.ModuleType("app.core.logger")
    logger_mod.logger = _Logger()
    sys.modules["app.core.logger"] = logger_mod

    def get_config(key, default=None):
        return config_values.get(key, default)

    config_mod = types.ModuleType("app.core.config")
    config_mod.get_config = get_config
    sys.modules["app.core.config"] = config_mod

    class AppException(Exception):
        pass

    class UpstreamException(Exception):
        pass

    class ValidationException(Exception):
        pass

    class ErrorType:
        SERVER = types.SimpleNamespace(value="server_error")

    exc_mod = types.ModuleType("app.core.exceptions")
    exc_mod.AppException = AppException
    exc_mod.UpstreamException = UpstreamException
    exc_mod.ValidationException = ValidationException
    exc_mod.ErrorType = ErrorType
    sys.modules["app.core.exceptions"] = exc_mod

    statsig_mod = types.ModuleType("app.services.grok.statsig")

    class StatsigService:
        @staticmethod
        def gen_id():
            return "stub-statsig"

    statsig_mod.StatsigService = StatsigService
    sys.modules["app.services.grok.statsig"] = statsig_mod

    model_mod = types.ModuleType("app.services.grok.model")

    class ModelService:
        pass

    model_mod.ModelService = ModelService
    sys.modules["app.services.grok.model"] = model_mod

    assets_mod = types.ModuleType("app.services.grok.assets")

    class UploadService:
        pass

    assets_mod.UploadService = UploadService
    sys.modules["app.services.grok.assets"] = assets_mod

    processor_mod = types.ModuleType("app.services.grok.processor")

    class StreamProcessor:
        pass

    class CollectProcessor:
        pass

    processor_mod.StreamProcessor = StreamProcessor
    processor_mod.CollectProcessor = CollectProcessor
    sys.modules["app.services.grok.processor"] = processor_mod

    retry_mod = types.ModuleType("app.services.grok.retry")
    retry_mod.retry_on_status = lambda fn, extract_status=None: fn()
    sys.modules["app.services.grok.retry"] = retry_mod

    token_mod = types.ModuleType("app.services.token")

    class EffortType:
        LOW = types.SimpleNamespace(value="low")
        HIGH = types.SimpleNamespace(value="high")

    async def get_token_manager():
        return None

    token_mod.get_token_manager = get_token_manager
    token_mod.EffortType = EffortType
    sys.modules["app.services.token"] = token_mod


def _load_chat_module(config_values):
    _install_stubs(config_values)
    file_path = Path(__file__).resolve().parents[1] / "app/services/grok/chat.py"
    spec = importlib.util.spec_from_file_location("chat_under_test", str(file_path))
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def main():
    mod_false = _load_chat_module({"grok.thinking": False, "grok.temporary": True})
    payload_false = mod_false.ChatRequestBuilder.build_payload(
        "hi", "grok-4", "MODEL_MODE_FAST", think=False
    )
    assert payload_false["isReasoning"] is False, payload_false

    payload_true = mod_false.ChatRequestBuilder.build_payload(
        "hi", "grok-4", "MODEL_MODE_FAST", think=True
    )
    assert payload_true["isReasoning"] is True, payload_true

    mod_default_true = _load_chat_module(
        {"grok.thinking": True, "grok.temporary": True}
    )
    payload_default = mod_default_true.ChatRequestBuilder.build_payload(
        "hi", "grok-4", "MODEL_MODE_FAST", think=None
    )
    assert payload_default["isReasoning"] is True, payload_default

    print("PASS: chat payload reasoning flag checks")


if __name__ == "__main__":
    main()
