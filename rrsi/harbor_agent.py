"""Keep Harbor's parsing and usage accounting while buffering SSE responses."""

import harbor.llms.lite_llm as harbor_llm
import litellm

from rrsi.chat_transport import acompletion


class _StreamingLiteLLM:
    acompletion = staticmethod(acompletion)

    def __getattr__(self, name):
        return getattr(litellm, name)


# Harbor rejects streams. Adapt only its module-local client, leaving the
# candidate harness and the process-wide LiteLLM module unchanged.
harbor_llm.litellm = _StreamingLiteLLM()

from harbor_terminus2 import terminus_2  # noqa: E402
from rrsi.coding_session import PreparedTmuxSession  # noqa: E402

# Setup is infrastructure, shared by baseline and evolved harnesses.
terminus_2.TmuxSession = PreparedTmuxSession

from harbor_terminus2 import AgentHarness  # noqa: E402, F401
