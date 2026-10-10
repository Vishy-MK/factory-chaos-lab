import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from app.rag.errors import RAGError

class ChatModelProvider:
    """Generates answers with any LangChain chat model."""
    configured = True

    def __init__(self, chat_model: BaseChatModel):
        self._chat_model = chat_model

    @property
    def chat_model(self) -> BaseChatModel:
        return self._chat_model

    async def generate(self, messages: list[BaseMessage]) -> str:
        try:
            result = await self.chat_model.ainvoke(messages)
        except RAGError:
            raise
        except openai.APITimeoutError as exc:
            raise RAGError("LLM provider timed out or could not be reached", 504) from exc
        except openai.APIConnectionError as exc:
            raise RAGError("LLM provider timed out or could not be reached", 504) from exc
        except (openai.RateLimitError, openai.InternalServerError) as exc:
            raise RAGError("LLM provider is temporarily unavailable", 502) from exc
        except openai.APIStatusError as exc:
            raise RAGError("LLM provider rejected the request; check its configuration", 502) from exc
        except Exception as exc:
            raise RAGError("LLM provider failed to generate a response", 502) from exc
        answer = result.content
        if not isinstance(answer, str) or not answer.strip():
            raise RAGError("LLM provider returned an empty or malformed response", 502)
        return answer.strip()

class OpenAICompatibleProvider(ChatModelProvider):
    """Lazily builds ChatOpenAI against any OpenAI-compatible endpoint."""
    def __init__(self, settings, http_async_client=None):
        self.settings = settings
        self._http_async_client = http_async_client
        self._chat_model = None

    @property
    def configured(self):
        return bool(self.settings.llm_base_url and self.settings.llm_model)

    @property
    def chat_model(self):
        if not self.configured:
            raise RAGError("Generation requires LLM_BASE_URL and LLM_MODEL (and LLM_API_KEY if required by your provider)")
        if self._chat_model is None:
            from langchain_openai import ChatOpenAI
            settings = self.settings
            # The OpenAI client requires a key; unauthenticated local servers ignore it.
            self._chat_model = ChatOpenAI(model=settings.llm_model, base_url=settings.llm_base_url.rstrip("/"), api_key=settings.llm_api_key.get_secret_value() or "not-required", timeout=settings.llm_timeout_seconds, max_retries=settings.llm_max_retries, temperature=0, http_async_client=self._http_async_client)
        return self._chat_model
