"""Buffer streamed Chat Completions to survive proxy idle timeouts."""

import os

from .env import litellm_model, require_openai_env


async def acompletion(**kwargs):
    import litellm

    require_openai_env()
    # Legacy LiteLLM proxy settings must not override the three shared settings.
    kwargs.update(model=litellm_model(), api_base=os.environ["OPENAI_BASE_URL"],
                  api_key=os.environ["OPENAI_API_KEY"])
    # Preserve callers which explicitly consume a stream themselves.
    if kwargs.get("stream"):
        return await litellm.acompletion(**kwargs)
    options = {**kwargs.get("stream_options", {}), "include_usage": True}
    stream = await litellm.acompletion(**{**kwargs, "stream": True, "stream_options": options})
    chunks = []
    try:
        async for chunk in stream:
            chunks.append(chunk)
    finally:
        await stream.aclose()
    # LiteLLM synthesizes a "stop" chunk on EOF, even for truncated streams.
    if not stream.received_finish_reason:
        raise litellm.APIConnectionError(
            message="Chat completion stream ended without a finish reason",
            llm_provider="openai", model=kwargs.get("model", ""))
    response = litellm.stream_chunk_builder(chunks, messages=kwargs.get("messages"))
    if response is None:
        raise litellm.APIConnectionError(
            message="Empty chat completion stream", llm_provider="openai",
            model=kwargs.get("model", ""))
    return response
