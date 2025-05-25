import logging

from google.adk.agents import Agent
from google.adk.models.google_llm import Gemini
from google.genai import Client as GenAIClient

from ..config import DEFAULT_MODEL_NAME, GOOGLE_API_KEY
from .prompt import TELEGRAM_BOT_SYSTEM_INSTRUCTION
from .tools import (
    generate_image_impl,
    generate_speech_impl,
    get_chat_history_impl,
    get_current_time,
    get_weather,
)

logger = logging.getLogger(__name__)


class TelegramBotAgent:

    def __init__(
        self,
        chat_id: int,
        model_name: str = DEFAULT_MODEL_NAME,
        api_key: str | None = None,
    ):
        self.chat_id = chat_id
        self.raw_model_name = model_name
        self.current_model_name = model_name.replace("models/", "")
        self.effective_google_api_key = (
            api_key if api_key is not None else GOOGLE_API_KEY
        )

        api_key_source_log = "unknown"
        if self.effective_google_api_key:
            if api_key is not None and GOOGLE_API_KEY and api_key != GOOGLE_API_KEY:
                api_key_source_log = "user-provided"
            elif api_key is None and GOOGLE_API_KEY:
                api_key_source_log = "bot-default"
            elif api_key is not None and not GOOGLE_API_KEY:
                api_key_source_log = "user-provided (no bot-default to compare)"
            elif api_key is not None and api_key == GOOGLE_API_KEY:
                api_key_source_log = "user-provided (matches bot-default)"
            else:
                api_key_source_log = "None (user key None, bot default None)"
        else:
            api_key_source_log = "None (resolved to no key)"

        logger.info(
            f"Initializing TelegramBotAgent instance for chat_id: {self.chat_id} with model_name: {self.current_model_name} (raw: {self.raw_model_name}). "
            f"API key source for ADK Agent's LLM: {api_key_source_log}."
        )

        if not self.effective_google_api_key:
            logger.warning(
                f"No API key explicitly available for TelegramBotAgent instance (chat {self.chat_id}). "
                "Gemini LLM will use its default client initialization."
            )
            self.gemini_llm_for_adk = Gemini(model=self.current_model_name)
        else:
            try:
                configured_genai_client = GenAIClient(
                    api_key=self.effective_google_api_key
                )
                self.gemini_llm_for_adk = Gemini(model=self.current_model_name)
                self.gemini_llm_for_adk.api_client = configured_genai_client
                logger.info(
                    f"TelegramBotAgent instance for chat {self.chat_id} will use Gemini LLM with a GenAIClient configured with API key ending ...{self.effective_google_api_key[-4:] if self.effective_google_api_key and len(self.effective_google_api_key) > 3 else 'N/A'}."
                )
            except Exception as e_client_assign:
                logger.error(
                    f"Error assigning configured GenAIClient to Gemini LLM for TelegramBotAgent instance (chat {self.chat_id}): {e_client_assign}. "
                    "Instance's ADK Agent will use Gemini's default client initialization."
                )
                self.gemini_llm_for_adk = Gemini(model=self.current_model_name)

        self._tools = self._create_tool_list()

        self.root_agent = Agent(
            name=f"TelegramBotAgent_Chat{self.chat_id}",
            description="A conversational AI assistant for Telegram chats that can use tools for specific tasks like getting time, weather, chat history, generating speech, or creating images.",
            model=self.gemini_llm_for_adk,
            tools=self._tools,
            instruction=TELEGRAM_BOT_SYSTEM_INSTRUCTION,
        )
        logger.info(
            f"Instance ADK Root Agent '{self.root_agent.name}' initialized with Gemini LLM instance (model: {self.gemini_llm_for_adk.model if hasattr(self.gemini_llm_for_adk, 'model') else 'N/A'}) and {len(self.root_agent.tools)} tools."
        )

    # --- Wrapper Methods for Tools ---
    async def get_chat_history(self) -> str:
        """Retrieves recent chat history for the current conversation."""
        return await get_chat_history_impl(chat_id=self.chat_id)

    async def generate_speech(self, text_to_speak: str) -> dict:
        """Generates speech from text using Google's Text-to-Speech API."""
        if self.effective_google_api_key is None:
            logger.error(
                f"Cannot generate speech for chat {self.chat_id}: effective_google_api_key is None."
            )
            return {
                "status": "error",
                "message": "API key not available for speech generation.",
            }
        return await generate_speech_impl(
            text_to_speak=text_to_speak, api_key_for_tool=self.effective_google_api_key
        )

    async def generate_image(self, prompt: str) -> dict:
        """Generates an image based on a prompt using Google's Imagen API."""
        if self.effective_google_api_key is None:
            logger.error(
                f"Cannot generate image for chat {self.chat_id}: effective_google_api_key is None."
            )
            return {
                "status": "error",
                "message": "API key not available for image generation.",
            }
        return await generate_image_impl(
            prompt=prompt, api_key_for_tool=self.effective_google_api_key
        )

    def _create_tool_list(self) -> list:
        """Helper to create the list of tools, using wrapper methods."""

        tools_list = [
            get_current_time,
            self.get_chat_history,
            get_weather,
            self.generate_speech,
            self.generate_image,
        ]
        return tools_list

    def update_model(self, new_model_name: str, new_api_key: str | None) -> bool:
        try:
            processed_new_model_name = new_model_name.replace("models/", "")
            new_effective_api_key = (
                new_api_key if new_api_key is not None else GOOGLE_API_KEY
            )

            logger.info(
                f"Attempting to update agent instance {self.root_agent.name}: "
                f"new model_name='{processed_new_model_name}' (raw: {new_model_name}), "
                f"new API key source='{'user-provided' if new_api_key else ('bot-default' if GOOGLE_API_KEY else 'None')}'."
            )

            new_gemini_llm_instance_for_adk: Gemini  # type: ignore[no-any-unimported]
            if not new_effective_api_key:
                logger.warning(
                    f"No API key explicitly available for updating agent instance {self.root_agent.name}. "
                    "Gemini LLM will use its default client initialization."
                )
                new_gemini_llm_instance_for_adk = Gemini(model=processed_new_model_name)
            else:
                try:
                    new_configured_genai_client = GenAIClient(
                        api_key=new_effective_api_key
                    )
                    new_gemini_llm_instance_for_adk = Gemini(
                        model=processed_new_model_name
                    )
                    new_gemini_llm_instance_for_adk.api_client = (
                        new_configured_genai_client
                    )
                    logger.info(
                        f"Update for agent instance {self.root_agent.name}: New Gemini LLM will use GenAIClient "
                        f"with API key ending ...{new_effective_api_key[-4:] if new_effective_api_key and len(new_effective_api_key) > 3 else 'N/A'}."
                    )
                except Exception as e_client_setup:
                    logger.error(
                        f"Failed to set up new Gemini LLM with client during update for agent instance {self.root_agent.name}: {e_client_setup}. "
                        "Falling back to default Gemini client init for the update."
                    )
                    new_gemini_llm_instance_for_adk = Gemini(
                        model=processed_new_model_name
                    )

            self.current_model_name = processed_new_model_name
            self.raw_model_name = new_model_name
            self.effective_google_api_key = new_effective_api_key
            self.gemini_llm_for_adk = new_gemini_llm_instance_for_adk

            self._tools = self._create_tool_list()

            self.root_agent = Agent(
                name=self.root_agent.name,
                description=self.root_agent.description,
                model=self.gemini_llm_for_adk,
                tools=self._tools,
                instruction=TELEGRAM_BOT_SYSTEM_INSTRUCTION,
            )

            logger.info(
                f"Successfully updated agent instance {self.root_agent.name} to model '{self.current_model_name}' "
                f"using API key ending ...{self.effective_google_api_key[-4:] if self.effective_google_api_key and len(self.effective_google_api_key) > 3 else 'N/A'} for its LLM client."
            )
            return True
        except Exception as e:
            logger.error(
                f"Failed to update model for agent instance {self.root_agent.name} to {new_model_name}: {e}",
                exc_info=True,
            )
            return False


_agent_instances_cache: dict[tuple[int, str, str | None], TelegramBotAgent] = {}


def get_or_create_agent(
    chat_id: int, model_name: str, api_key: str | None
) -> TelegramBotAgent | None:
    """
    Retrieves an existing TelegramBotAgent instance or creates a new one.
    """
    global _agent_instances_cache

    normalized_model_name = model_name.replace("models/", "")
    agent_cache_key = (chat_id, normalized_model_name, api_key)

    if agent_cache_key in _agent_instances_cache:
        agent_instance = _agent_instances_cache[agent_cache_key]
        new_request_effective_api_key = (
            api_key if api_key is not None else GOOGLE_API_KEY
        )
        if (
            agent_instance.current_model_name != normalized_model_name
            or agent_instance.effective_google_api_key != new_request_effective_api_key
        ):
            logger.warning(
                f"Cached TelegramBotAgent instance found for key {agent_cache_key} but with internal configuration mismatch. "
                f"Attempting to update the agent instance. Cached model: {agent_instance.current_model_name}, New: {normalized_model_name}. "
                f"Cached API key ends: ...{agent_instance.effective_google_api_key[-4:] if agent_instance.effective_google_api_key else 'N/A'}, "
                f"New effective API key ends: ...{new_request_effective_api_key[-4:] if new_request_effective_api_key else 'N/A'}."
            )
            if not agent_instance.update_model(model_name, api_key):
                logger.error(
                    f"Failed to update mismatched cached TelegramBotAgent instance {agent_instance.root_agent.name}. Removing from cache and re-creating."
                )
                try:
                    del _agent_instances_cache[agent_cache_key]
                except KeyError:
                    pass
            else:
                logger.debug(
                    f"Reusing (after internal update due to mismatch) cached TelegramBotAgent instance for key: {agent_cache_key}"
                )
                return agent_instance
        else:
            logger.debug(
                f"Reusing cached TelegramBotAgent instance for key: {agent_cache_key}"
            )
            return agent_instance

    try:
        logger.info(
            f"Creating new TelegramBotAgent instance for cache key: {agent_cache_key}"
        )
        agent_instance = TelegramBotAgent(
            chat_id=chat_id, model_name=model_name, api_key=api_key
        )
        _agent_instances_cache[agent_cache_key] = agent_instance
        logger.info(
            f"TelegramBotAgent instance for chat_id {chat_id} created successfully: model='{agent_instance.current_model_name}', "
            f"API key for its ADK Agent's LLM ends with '...{agent_instance.effective_google_api_key[-4:] if agent_instance.effective_google_api_key and len(agent_instance.effective_google_api_key) > 3 else 'N/A'}'."
        )
        return agent_instance
    except Exception as e:
        logger.error(
            f"Failed to create TelegramBotAgent instance for chat_id {chat_id}, model {model_name}, "
            f"requested_api_key '...{api_key[-4:] if api_key and len(api_key) > 3 else ('None' if api_key is None else 'BotDefaultUsed')}': {e}",
            exc_info=True,
        )
        return None
