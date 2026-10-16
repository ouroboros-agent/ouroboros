"""Shared LLM entry for live routes, separate from text-provider defaults.

OpenAI uses the same direct target resolver as image and text calls. Google Live
reads the provider credential registry with the same task-scoped settings reader;
a Live credential alone never invents a text route or changes Main's selection.
"""
from __future__ import annotations

from dataclasses import replace

from ouroboros.gateways.realtime_engine import EngineRoute, LiveEngine, SessionConfig
from ouroboros.gateways.realtime_transport import LiveEngineRefusal
from ouroboros.provider_models import PROVIDER_ENV_KEYS
from ouroboros.settings_integrity import runtime_setting


def make_live_engine(client, route: EngineRoute) -> LiveEngine:
    from ouroboros.gateways.realtime_openai import OpenAIRealtimeEngine
    from ouroboros.gateways.realtime_gemini import GeminiLiveEngine

    if route.engine not in ("openai", "google"):
        raise LiveEngineRefusal("engine", "Select the openai or google live engine.")
    if not route.model.strip() or "::" in route.model:
        raise LiveEngineRefusal("model", "A live route requires the provider's unqualified model ID.")
    if route.engine == "openai":
        target = client.resolve_remote_target(f"openai::{route.model}")
        key = str(target.get("api_key") or "").strip()
        engine = OpenAIRealtimeEngine(api_key=key)
    else:
        key = str(runtime_setting(PROVIDER_ENV_KEYS[route.engine], "") or "").strip()
        engine = GeminiLiveEngine(api_key=key)
    if not key:
        raise LiveEngineRefusal("missing_credential", f"Configure {PROVIDER_ENV_KEYS[route.engine]} for this live route.")
    engine.route = replace(route, input_rate=16000 if route.engine == "google" else 24000, output_rate=24000)
    return engine


async def open_configured_engine(engine: LiveEngine, route: EngineRoute, config: SessionConfig) -> LiveEngine:
    if (route.engine, route.model, route.voice) != (config.route.engine, config.route.model, config.route.voice):
        raise LiveEngineRefusal("route_mismatch", "Session configuration must use the selected live route.")
    await engine.open(config)
    return engine
