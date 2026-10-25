import datetime
import logging
import mimetypes
import os
import struct
import tempfile
import time
from typing import Any

import requests
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from ..config import IMAGE_GENERATION_MODEL, OPEN_WEATHER_API_KEY, VOICE_MODEL
from ..db import get_history_from_db
from ..helpers import sanitize_filename

logger = logging.getLogger(__name__)

# Per-chat cache of the most recently uploaded user photo (bytes, mime_type) for image editing
_latest_user_photo_cache: dict[int, tuple[bytes, str]] = {}


def set_latest_user_photo(chat_id: int, image_bytes: bytes, mime_type: str) -> None:
    """Caches the latest uploaded photo for a chat so image editing tools can reference it."""
    _latest_user_photo_cache[chat_id] = (image_bytes, mime_type)
    logger.info(
        f"Cached latest user photo for chat_id {chat_id} ({len(image_bytes)} bytes, {mime_type})."
    )


def get_latest_user_photo(chat_id: int) -> tuple[bytes, str] | None:
    """Retrieves the latest cached photo for a chat, if any."""
    return _latest_user_photo_cache.get(chat_id)


def clear_latest_user_photo(chat_id: int) -> None:
    """Clears the cached photo for a chat."""
    _latest_user_photo_cache.pop(chat_id, None)


def get_current_time() -> str:
    """Returns the current date and time."""
    return f"The current date and time is {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}."


def _fetch_weather_api(
    base_url: str, params: dict[str, Any], city: str, weather_type: str
) -> dict[str, Any]:
    """Helper function to fetch weather data from OpenWeatherMap API."""
    try:
        response = requests.get(base_url, params=params, timeout=10)
        response.raise_for_status()
        return {"status": "success", "data": response.json()}
    except requests.exceptions.HTTPError as http_err:
        status_code = (
            http_err.response.status_code if http_err.response is not None else 500
        )
        log_message = f"OpenWeatherMap API key error for {city} ({weather_type}, status {status_code}): {http_err}"
        user_message = f"Failed to retrieve {weather_type} weather data due to an HTTP error ({status_code})."
        match status_code:
            case 401:
                logger.error(
                    f"OpenWeatherMap API key is invalid or not authorized ({weather_type}). {http_err}"
                )
                user_message = "Weather service authorization failed. Please check the bot admin's API key."
            case 404:
                logger.warning(
                    f"City '{city}' not found by OpenWeatherMap ({weather_type}). {http_err}"
                )
                user_message = f"Sorry, I couldn't find {weather_type} weather information for '{city}'."
            case _:
                logger.error(log_message)
        return {"status": "error", "error_message": user_message}
    except requests.exceptions.ConnectionError as conn_err:
        logger.error(
            f"Connection error fetching {weather_type} weather for {city}: {conn_err}"
        )
        return {
            "status": "error",
            "error_message": f"Failed to connect to the weather service for {weather_type} weather. Please check your internet connection.",
        }
    except requests.exceptions.Timeout as timeout_err:
        logger.error(
            f"Timeout fetching {weather_type} weather for {city}: {timeout_err}"
        )
        return {
            "status": "error",
            "error_message": f"The {weather_type} weather service took too long to respond. Please try again later.",
        }
    except Exception as e:
        logger.error(
            f"An unexpected error occurred in _fetch_weather_api ({weather_type}) for {city}: {e}",
            exc_info=True,
        )
        return {
            "status": "error",
            "error_message": f"An unexpected error occurred while fetching {weather_type} weather data.",
        }


def get_weather(city: str, day_offset: int = 0) -> dict[str, Any]:
    """Retrieves the current weather or a forecast for a specified city using OpenWeatherMap API.
    Args:
        city (str): The name of the city for which to retrieve the weather.
        day_offset (int, optional): 0 for current weather (default).
                                    1 for tomorrow's forecast.
                                    2 for the day after tomorrow's forecast, and so on (up to 5 days).
    Returns:
        dict: status and result or error msg.
    """
    api_key = OPEN_WEATHER_API_KEY
    if not api_key:
        logger.error("OpenWeatherMap API key (OPEN_WEATHER_API_KEY) not found.")
        return {
            "status": "error",
            "error_message": "Weather service is not configured (API key missing).",
        }

    if not 0 <= day_offset <= 5:
        return {
            "status": "error",
            "error_message": "Invalid day_offset. Must be between 0 (today) and 5.",
        }

    if day_offset == 0:  # Current Weather
        base_url = "https://api.openweathermap.org/data/2.5/weather"
        params = {"q": city, "appid": api_key, "units": "metric"}
        weather_type = "current"
        api_response = _fetch_weather_api(base_url, params, city, weather_type)
        if api_response["status"] == "error":
            return api_response
        weather_data = api_response["data"]
        if weather_data.get("cod") != 200 and weather_data.get(
            "message"
        ):  # Check for API error code
            logger.warning(
                f"OpenWeatherMap API error for {city} ({weather_type}): {weather_data.get('message')}"
            )
            return {
                "status": "error",
                "error_message": f"Could not retrieve {weather_type} weather for '{city}': {weather_data.get('message')}",
            }
        main_weather = weather_data.get("weather", [{}])[0]
        description = main_weather.get("description", "not available")
        temp_data = weather_data.get("main", {})
        report = (
            f"Current weather in {weather_data.get('name', city).title()}:\n"
            f"- Condition: {description.capitalize()}\n"
            f"- Temperature: {temp_data.get('temp', 'N/A')}°C (feels like {temp_data.get('feels_like', 'N/A')}°C)\n"
            f"- Humidity: {temp_data.get('humidity', 'N/A')}%\n"
            f"- Wind: {weather_data.get('wind', {}).get('speed', 'N/A')} m/s"
        )
        logger.info(
            f"Successfully fetched {weather_type} weather for {city}: {description}"
        )
        return {"status": "success", "report": report}
    else:  # Forecast
        base_url = "https://api.openweathermap.org/data/2.5/forecast"
        params = {"q": city, "appid": api_key, "units": "metric", "cnt": "40"}
        weather_type = "forecast"
        api_response = _fetch_weather_api(base_url, params, city, weather_type)
        if api_response["status"] == "error":
            return api_response
        forecast_data = api_response["data"]
        if str(forecast_data.get("cod")) != "200":
            logger.warning(
                f"OpenWeatherMap API error for {city} ({weather_type}): {forecast_data.get('message')}"
            )
            return {
                "status": "error",
                "error_message": f"Could not retrieve {weather_type} for '{city}': {forecast_data.get('message')}",
            }

        target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
        found_forecast_for_day = None

        for forecast_entry in forecast_data.get("list", []):
            entry_datetime = datetime.datetime.fromtimestamp(
                forecast_entry.get("dt", 0), tz=datetime.timezone.utc
            )
            if entry_datetime.date() == target_date and 11 <= entry_datetime.hour <= 14:
                found_forecast_for_day = forecast_entry
                break

        if not found_forecast_for_day:
            for forecast_entry in forecast_data.get("list", []):
                entry_datetime = datetime.datetime.fromtimestamp(
                    forecast_entry.get("dt", 0), tz=datetime.timezone.utc
                )
                if entry_datetime.date() == target_date:
                    found_forecast_for_day = forecast_entry
                    break

        if not found_forecast_for_day:
            date_str_log = target_date.strftime("%Y-%m-%d")
            logger.warning(f"No forecast data found for {city} on {date_str_log}.")
            return {
                "status": "error",
                "error_message": f"Could not find forecast for '{city}' on {date_str_log}. Forecast might be >5 days or data unavailable.",
            }

        main_weather = found_forecast_for_day.get("weather", [{}])[0]
        description = main_weather.get("description", "not available")
        temp_data = found_forecast_for_day.get("main", {})
        pop = found_forecast_for_day.get("pop", 0) * 100
        date_str_report = target_date.strftime("%A, %B %d, %Y")
        report = (
            f"Forecast for {forecast_data.get('city', {}).get('name', city).title()} on {date_str_report}:\n"
            f"- Condition: {description.capitalize()}\n"
            f"- Temperature: Approximately {temp_data.get('temp', 'N/A')}°C (feels like {temp_data.get('feels_like', 'N/A')}°C)\n"
            f"- Humidity: {temp_data.get('humidity', 'N/A')}%\n"
            f"- Wind: {found_forecast_for_day.get('wind', {}).get('speed', 'N/A')} m/s\n"
            f"- Chance of Precipitation: {pop:.0f}%"
        )
        logger.info(
            f"Successfully fetched {weather_type} for {city} on {date_str_report}: {description}"
        )
        return {"status": "success", "report": report}


async def get_chat_history_impl(chat_id: int) -> str:
    """Retrieves recent chat history for the current conversation."""
    if not isinstance(chat_id, int):
        logger.error(
            f"get_chat_history_impl tool: invalid chat_id type: {type(chat_id)} provided."
        )
        return "Error: Could not determine the chat to fetch history for (internal id issue)."
    logger.info(f"Tool: get_chat_history_impl called for chat_id: {chat_id}")
    history_turns = await get_history_from_db(chat_id)
    if not history_turns:
        return "No history found for this chat yet."

    formatted_history = "Recent chat history (most recent last):\n"
    for turn_content_obj in history_turns[-10:]:
        role = getattr(turn_content_obj, "role", "unknown") or "unknown"
        actual_parts: list[genai_types.Part] | None = getattr(
            turn_content_obj, "parts", None
        )
        logger.debug(
            f"Chat History Turn: role='{role}', actual_parts='{actual_parts}' (type: {type(actual_parts)})"
        )

        current_turn_texts = []
        if actual_parts:
            for i, part_object in enumerate(actual_parts):
                logger.debug(f"Processing Part {i} from Content object: {part_object}")
                if hasattr(part_object, "text") and part_object.text is not None:
                    current_turn_texts.append(str(part_object.text))
                elif (
                    hasattr(part_object, "function_call")
                    and part_object.function_call is not None
                ):
                    fc = part_object.function_call
                    current_turn_texts.append(
                        f"[Function Call: {fc.name} with args {fc.args}]"
                    )
                else:
                    logger.debug(
                        f"Part item {i} has no displayable text or recognized function call."
                    )
        else:
            logger.debug(f"Turn role='{role}' had no 'parts' attribute or it was None.")

        content = (
            " ".join(filter(None, current_turn_texts))
            if current_turn_texts
            else "[no text content in parts]"
        )
        logger.debug(f"Formatted content for turn: '{content}'")
        formatted_history += f"- {role.capitalize()}: {content}\n"
    return formatted_history


def _parse_audio_mime_type_params_for_tools(mime_type: str) -> dict[str, int]:
    bits_per_sample = 16
    rate = 24000
    parts = mime_type.lower().split(";")
    main_type_part = parts[0].strip()
    if main_type_part.startswith("audio/l"):
        try:
            bits_str = main_type_part.split("l", 1)[1]
            if bits_str:
                bits_per_sample = int(bits_str)
        except (ValueError, IndexError):
            logger.warning(
                f"Could not parse bits_per_sample from {main_type_part}, defaulting to {bits_per_sample}."
            )
            pass
    for param in parts[1:]:
        param = param.strip()
        if param.startswith("rate="):
            try:
                rate_str = param.split("=", 1)[1]
                rate = int(rate_str)
            except (ValueError, IndexError):
                logger.warning(
                    f"Could not parse rate from {param}, defaulting to {rate}."
                )
                pass
    logger.debug(
        f"Parsed MIME '{mime_type}': bits_per_sample={bits_per_sample}, rate={rate}"
    )
    return {"bits_per_sample": bits_per_sample, "rate": rate}


def _convert_raw_to_wav_bytes_for_tools(
    audio_data: bytes,
    bits_per_sample: int,
    sample_rate: int,
    num_channels: int = 1,
) -> bytes:
    data_size = len(audio_data)
    bytes_per_sample = bits_per_sample // 8
    block_align = num_channels * bytes_per_sample
    byte_rate = sample_rate * block_align
    header = b"RIFF"
    header += struct.pack("<I", 36 + data_size)  # ChunkSize
    header += b"WAVE"
    header += b"fmt "  # Subchunk1ID
    header += struct.pack("<I", 16)  # Subchunk1Size (16 for PCM)
    header += struct.pack("<H", 1)  # AudioFormat (1 for PCM)
    header += struct.pack("<H", num_channels)
    header += struct.pack("<I", sample_rate)
    header += struct.pack("<I", byte_rate)
    header += struct.pack("<H", block_align)
    header += struct.pack("<H", bits_per_sample)
    header += b"data"  # Subchunk2ID
    header += struct.pack("<I", data_size)  # Subchunk2Size
    return header + audio_data


async def generate_speech_impl(
    text_to_speak: str, api_key_for_tool: str
) -> dict[str, Any]:
    """Generates speech from text using Google's Gemini Text-to-Speech API."""
    logger.info(
        f"Tool: generate_speech_impl called for text: '{text_to_speak[:50]}...'"
    )
    if not text_to_speak.strip():
        return {"status": "error", "message": "Input text cannot be empty."}

    if not api_key_for_tool:
        logger.error("TTS Tool: No API key provided to generate_speech_impl.")
        return {
            "status": "error",
            "message": "TTS service not configured (API key missing).",
        }

    try:
        qualified_tts_model_name = VOICE_MODEL
        logger.info(
            f"TTS Async: Requesting TTS from model: {qualified_tts_model_name} using API key ending ...{api_key_for_tool[-4:] if len(api_key_for_tool) > 3 else 'N/A'}"
        )
        client = genai.Client(api_key=api_key_for_tool)
        speech_generation_config = genai_types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=genai_types.SpeechConfig(
                voice_config=genai_types.VoiceConfig(
                    prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                        voice_name="Kore"
                    )
                )
            ),
        )
        response = await client.aio.models.generate_content(
            model=qualified_tts_model_name,
            contents=text_to_speak,
            config=speech_generation_config,
        )

        inline_data = None
        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts or []:
                if part.inline_data and part.inline_data.data:
                    inline_data = part.inline_data
                    break

        if inline_data and inline_data.data:
            audio_data = inline_data.data
            original_mime_type = (
                inline_data.mime_type or "audio/L16;codec=pcm;rate=24000"
            )
            logger.info(
                f"TTS Async: Successfully received audio data. MIME: {original_mime_type}, Length: {len(audio_data)} bytes."
            )
            processed_audio_data, final_mime_type_for_response, file_extension = (
                audio_data,
                original_mime_type,
                ".audio",
            )
            if (
                original_mime_type.lower().startswith("audio/l")
                and "codec=pcm" in original_mime_type.lower()
            ):
                params = _parse_audio_mime_type_params_for_tools(original_mime_type)
                try:
                    processed_audio_data = _convert_raw_to_wav_bytes_for_tools(
                        audio_data, params["bits_per_sample"], params["rate"]
                    )
                    file_extension, final_mime_type_for_response = (".wav", "audio/wav")
                except Exception as e_convert:
                    logger.error(
                        f"Failed to convert raw audio to WAV: {e_convert}",
                        exc_info=True,
                    )
                    return {
                        "status": "error",
                        "message": f"Failed to process audio into WAV: {str(e_convert)[:100]}",
                    }
            else:
                guessed_extension = mimetypes.guess_extension(original_mime_type)
                if original_mime_type == "audio/mpeg" and not guessed_extension:
                    file_extension = ".mp3"
                elif original_mime_type == "audio/ogg" and not guessed_extension:
                    file_extension = ".ogg"
                elif guessed_extension:
                    file_extension = guessed_extension

            unique_id = str(int(time.time() * 1000))
            max_stem = 60 - len(unique_id) - 1
            sanitized_base = (
                sanitize_filename(
                    text_to_speak, max_length=max_stem if max_stem > 5 else 5
                )
                if max_stem > 5
                else "speech"
            )
            final_stem = f"{sanitized_base}_{unique_id}"
            temp_file_path = os.path.join(
                tempfile.gettempdir(), f"{final_stem}{file_extension}"
            )
            try:
                with open(temp_file_path, "wb") as f:
                    f.write(processed_audio_data)
                return {
                    "status": "success",
                    "file_path": temp_file_path,
                    "mime_type": final_mime_type_for_response,
                }
            except IOError as e_io:
                return {
                    "status": "error",
                    "message": f"Failed to save audio: {str(e_io)}",
                }
        elif response.prompt_feedback and response.prompt_feedback.block_reason:
            logger.warning(
                f"TTS Async: Blocked. Reason: {response.prompt_feedback.block_reason_message}"
            )
            return {
                "status": "error",
                "message": f"Could not generate audio due to safety filters: {response.prompt_feedback.block_reason_message}",
            }
        else:
            logger.warning("TTS Async: Failed: No audio data or unexpected structure.")
            return {
                "status": "error",
                "message": "Failed to generate audio: Model did not return audio data as expected.",
            }
    except Exception as e_async:
        logger.error(f"TTS Tool error: {e_async}", exc_info=True)
        err_code = getattr(e_async, "code", "N/A")
        msg = (
            str(e_async.message)[:100]
            if hasattr(e_async, "message") and e_async.message
            else str(e_async)[:100]
        )
        return {"status": "error", "message": f"TTS API Error ({err_code}) - {msg}"}


async def generate_image_impl(
    prompt: str,
    api_key_for_tool: str,
    chat_id: int | None = None,
    use_uploaded_photo: bool = False,
) -> dict[str, Any]:
    """Generates or edits an image based on a prompt (and optional user-uploaded photo) using Google's Gemini Image or Imagen API."""
    logger.info(
        f"Tool: generate_image_impl called for prompt: '{prompt[:100]}...', chat_id={chat_id}, use_uploaded_photo={use_uploaded_photo}"
    )
    if not prompt.strip():
        return {
            "status": "error",
            "message": "Image generation prompt cannot be empty.",
        }

    if not api_key_for_tool:
        logger.error("Image Tool: No API key provided to generate_image_impl.")
        return {
            "status": "error",
            "message": "Image generation service not configured: No API key provided.",
        }

    try:
        image_model_name = IMAGE_GENERATION_MODEL
        logger.info(
            f"IMAGE_TOOL: Requesting image from {image_model_name} using API key ending ...{api_key_for_tool[-4:] if len(api_key_for_tool) > 3 else 'N/A'}"
        )
        client = genai.Client(api_key=api_key_for_tool)

        image_bytes: bytes | None = None
        mime_type: str = "image/png"

        cached_photo = (
            get_latest_user_photo(chat_id)
            if (use_uploaded_photo and chat_id is not None)
            else None
        )

        if "imagen" in image_model_name.lower() and not cached_photo:
            config = genai_types.GenerateImagesConfig(
                number_of_images=1, output_mime_type="image/jpeg", aspect_ratio="1:1"
            )
            result = await client.aio.models.generate_images(
                model=image_model_name, prompt=prompt, config=config
            )
            if (
                result.generated_images
                and result.generated_images[0].image
                and result.generated_images[0].image.image_bytes
            ):
                image_bytes = result.generated_images[0].image.image_bytes
                mime_type = "image/jpeg"
        else:
            # Gemini native image generation & editing models (e.g., gemini-3.1-flash-image, gemini-2.5-flash-image, gemini-3-pro-image)
            effective_model = (
                "gemini-3.1-flash-image"
                if "imagen" in image_model_name.lower()
                else image_model_name
            )
            gen_config = genai_types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"]
            )
            contents_payload: Any
            if cached_photo:
                ref_bytes, ref_mime = cached_photo
                logger.info(
                    f"IMAGE_TOOL: Including user's uploaded photo ({len(ref_bytes)} bytes, {ref_mime}) for image editing in chat {chat_id}."
                )
                contents_payload = [
                    genai_types.Part.from_bytes(data=ref_bytes, mime_type=ref_mime),
                    genai_types.Part(text=prompt),
                ]
            else:
                contents_payload = prompt

            response = await client.aio.models.generate_content(
                model=effective_model, contents=contents_payload, config=gen_config
            )
            if response.candidates and response.candidates[0].content:
                for part in response.candidates[0].content.parts or []:
                    if part.inline_data and part.inline_data.data:
                        image_bytes = part.inline_data.data
                        mime_type = part.inline_data.mime_type or "image/png"
                        break
            if (
                not image_bytes
                and response.prompt_feedback
                and response.prompt_feedback.block_reason
            ):
                reason = (
                    response.prompt_feedback.block_reason_message
                    or response.prompt_feedback.block_reason
                )
                return {
                    "status": "error",
                    "message": f"Could not generate image due to safety filters: {reason}",
                }

        if image_bytes:
            logger.info(
                f"Image Tool: Received image data, length: {len(image_bytes)} bytes, mime_type: {mime_type}."
            )
            ext = ".jpg" if "jpeg" in mime_type else ".png"
            base_name = sanitize_filename(prompt, max_length=30)
            filename = f"{base_name}_{int(time.time()*1000)}{ext}"
            file_path = os.path.join(tempfile.gettempdir(), filename)
            try:
                with open(file_path, "wb") as f:
                    f.write(image_bytes)
                return {
                    "status": "success",
                    "file_path": file_path,
                    "mime_type": mime_type,
                }
            except IOError as e_io:
                return {
                    "status": "error",
                    "message": f"Failed to save image: {str(e_io)}",
                }

        logger.warning("Image Tool: Failed: No image data returned.")
        return {
            "status": "error",
            "message": "Image generation failed: Model did not return image data.",
        }

    except Exception as e_img:
        logger.error(f"Image Tool Error: {e_img}", exc_info=True)
        err_code = getattr(e_img, "code", "N/A")
        full_error_message = (
            str(e_img.message)
            if hasattr(e_img, "message") and e_img.message
            else str(e_img)
        )
        return {
            "status": "error",
            "message": f"Image generation failed with an API error. Details: {full_error_message}",
            "raw_error_details": {
                "code": err_code,
                "status_from_exception": (
                    getattr(e_img, "status", "N/A")
                    if isinstance(e_img, genai_errors.APIError)
                    else "N/A"
                ),
                "full_message": full_error_message,
            },
        }
