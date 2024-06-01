import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from langchain.schema import ChatMessage

from khoj.database.models import Agent
from khoj.processor.conversation import prompts
from khoj.processor.conversation.openai.utils import (
    chat_completion_with_backoff,
    completion_with_backoff,
)
from khoj.processor.conversation.utils import generate_chatml_messages_with_context
from khoj.utils.helpers import ConversationCommand, is_none_or_empty
from khoj.utils.rawconfig import LocationData

logger = logging.getLogger(__name__)


def extract_questions(
    text,
    model: Optional[str] = "gpt-4-turbo-preview",
    conversation_log={},
    api_key=None,
    api_base_url=None,
    temperature=0,
    max_tokens=100,
    location_data: LocationData = None,
):
    """
    Infer search queries to retrieve relevant notes to answer user query
    """
    location = f"{location_data.city}, {location_data.region}, {location_data.country}" if location_data else "Unknown"

    # Extract Past User Message and Inferred Questions from Conversation Log
    chat_history = "".join(
        [
            f'Q: {chat["intent"]["query"]}\nKhoj: {{"queries": {chat["intent"].get("inferred-queries") or list([chat["intent"]["query"]])}}}\nA: {chat["message"]}\n\n'
            for chat in conversation_log.get("chat", [])[-4:]
            if chat["by"] == "khoj" and "text-to-image" not in chat["intent"].get("type")
        ]
    )

    # Get dates relative to today for prompt creation
    today = datetime.today()
    current_new_year = today.replace(month=1, day=1)
    last_new_year = current_new_year.replace(year=today.year - 1)

    prompt = prompts.extract_questions.format(
        current_date=today.strftime("%Y-%m-%d"),
        day_of_week=today.strftime("%A"),
        last_new_year=last_new_year.strftime("%Y"),
        last_new_year_date=last_new_year.strftime("%Y-%m-%d"),
        current_new_year_date=current_new_year.strftime("%Y-%m-%d"),
        bob_tom_age_difference={current_new_year.year - 1984 - 30},
        bob_age={current_new_year.year - 1984},
        chat_history=chat_history,
        text=text,
        yesterday_date=(today - timedelta(days=1)).strftime("%Y-%m-%d"),
        location=location,
    )
    messages = [ChatMessage(content=prompt, role="user")]

    # Get Response from GPT
    response = completion_with_backoff(
        messages=messages,
        model=model,
        temperature=temperature,
        api_base_url=api_base_url,
        model_kwargs={"response_format": {"type": "json_object"}},
        openai_api_key=api_key,
    )

    # Extract, Clean Message from GPT's Response
    try:
        response = response.strip()
        response = json.loads(response)
        response = [q.strip() for q in response["queries"] if q.strip()]
        if not isinstance(response, list) or not response:
            logger.error(f"Invalid response for constructing subqueries: {response}")
            return [text]
        return response
    except:
        logger.warning(f"GPT returned invalid JSON. Falling back to using user message as search query.\n{response}")
        questions = [text]

    logger.debug(f"Extracted Questions by GPT: {questions}")
    return questions


def send_message_to_model(messages, api_key, model, response_type="text", api_base_url=None):
    """
    Send message to model
    """

    # Get Response from GPT
    return completion_with_backoff(
        messages=messages,
        model=model,
        openai_api_key=api_key,
        api_base_url=api_base_url,
        model_kwargs={"response_format": {"type": response_type}},
    )


def converse(
    references,
    user_query,
    online_results: Optional[Dict[str, Dict]] = None,
    conversation_log={},
    model: str = "gpt-3.5-turbo",
    api_key: Optional[str] = None,
    api_base_url: Optional[str] = None,
    temperature: float = 0.2,
    completion_func=None,
    conversation_commands=[ConversationCommand.Default],
    max_prompt_size=None,
    tokenizer_name=None,
    location_data: LocationData = None,
    user_name: str = None,
    agent: Agent = None,
):
    """
    Converse with user using OpenAI's ChatGPT
    """
    # Initialize Variables
    current_date = datetime.now().strftime("%Y-%m-%d")
    compiled_references = "\n\n".join({f"# {item['compiled']}" for item in references})

    conversation_primer = prompts.query_prompt.format(query=user_query)

    if agent and agent.personality:
        system_prompt = prompts.custom_personality.format(
            name=agent.name, bio=agent.personality, current_date=current_date
        )
    else:
        system_prompt = prompts.personality.format(current_date=current_date)

    if location_data:
        location = f"{location_data.city}, {location_data.region}, {location_data.country}"
        location_prompt = prompts.user_location.format(location=location)
        system_prompt = f"{system_prompt}\n{location_prompt}"

    if user_name:
        user_name_prompt = prompts.user_name.format(name=user_name)
        system_prompt = f"{system_prompt}\n{user_name_prompt}"

    # Get Conversation Primer appropriate to Conversation Type
    if conversation_commands == [ConversationCommand.Notes] and is_none_or_empty(compiled_references):
        completion_func(chat_response=prompts.no_notes_found.format())
        return iter([prompts.no_notes_found.format()])
    elif conversation_commands == [ConversationCommand.Online] and is_none_or_empty(online_results):
        completion_func(chat_response=prompts.no_online_results_found.format())
        return iter([prompts.no_online_results_found.format()])

    if ConversationCommand.Online in conversation_commands or ConversationCommand.Webpage in conversation_commands:
        conversation_primer = (
            f"{prompts.online_search_conversation.format(online_results=str(online_results))}\n{conversation_primer}"
        )
    if not is_none_or_empty(compiled_references):
        conversation_primer = f"{prompts.notes_conversation.format(query=user_query, references=compiled_references)}\n\n{conversation_primer}"

    # Setup Prompt with Primer or Conversation History
    messages = generate_chatml_messages_with_context(
        conversation_primer,
        system_prompt,
        conversation_log,
        model_name=model,
        max_prompt_size=max_prompt_size,
        tokenizer_name=tokenizer_name,
    )
    truncated_messages = "\n".join({f"{message.content[:70]}..." for message in messages})
    logger.debug(f"Conversation Context for GPT: {truncated_messages}")

    # Get Response from GPT
    return chat_completion_with_backoff(
        messages=messages,
        compiled_references=references,
        online_results=online_results,
        model_name=model,
        temperature=temperature,
        openai_api_key=api_key,
        api_base_url=api_base_url,
        completion_func=completion_func,
        model_kwargs={"stop": ["Notes:\n["]},
    )
