from typing import Any, Literal, Optional, TypedDict

from google.adk.runners import Runner
from google.genai import client as genai_client
from google.genai import types as genai_types

HistoryTurn = genai_types.Content


class UserSettings(TypedDict):
    gemini_api_key: Optional[str]
    selected_model: str
    message_count: int


class ModelInfo(TypedDict):
    name: str
    description: str
    input_token_limit: Optional[int]
    output_token_limit: Optional[int]
    supported_actions: list[str]


class AIInteractionContext(TypedDict):
    chat_id: int
    user_id_for_agent: str
    session_id_for_agent: str
    model_for_agent: str
    urls_found: list[str]
    active_genai_client: genai_client.Client
    runner: Runner  # type: ignore[no-any-unimported]


class UserSettingsTableRowUpsert(TypedDict):
    chat_id: int
    gemini_api_key: Optional[str]
    selected_model: str
    message_count: Optional[int]


class SerializedFunctionCall(TypedDict):
    name: str
    args: dict[str, Any]


class SerializedFunctionResponse(TypedDict):
    name: str
    response: dict[str, Any]


class SerializedFileData(TypedDict):
    mime_type: str
    file_uri: str


class SerializedPart(TypedDict, total=False):
    type: Literal["text", "image", "function_call", "function_response", "file_data"]
    text: str  # For type="text"
    mime_type: str  # For type="image" (also in SerializedFileData)
    data_placeholder: str  # For type="image"
    # caption: Optional[str] # Not directly saved as part of 'image' type dict, handled by separate text part
    function_call: SerializedFunctionCall  # For type="function_call"
    function_response: SerializedFunctionResponse  # For type="function_response"
    file_data: SerializedFileData  # For type="file_data"
