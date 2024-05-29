import json
import logging
import math
import random
import re
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterable, List, Optional, Type

import cron_descriptor
from apscheduler.job import Job
from asgiref.sync import sync_to_async
from django.contrib.sessions.backends.db import SessionStore
from django.db import models
from django.db.models import Q
from django.db.models.manager import BaseManager
from django.db.utils import IntegrityError
from django_apscheduler.models import DjangoJob, DjangoJobExecution
from fastapi import HTTPException
from pgvector.django import CosineDistance
from torch import Tensor

from khoj.database.models import (
    Agent,
    ChatModelOptions,
    ClientApplication,
    Conversation,
    Entry,
    GithubConfig,
    GithubRepoConfig,
    GoogleUser,
    KhojApiUser,
    KhojUser,
    NotionConfig,
    OpenAIProcessorConversationConfig,
    ProcessLock,
    PublicConversation,
    ReflectiveQuestion,
    SearchModelConfig,
    ServerChatSettings,
    SpeechToTextModelOptions,
    Subscription,
    TextToImageModelConfig,
    UserConversationConfig,
    UserRequests,
    UserSearchModelConfig,
)
from khoj.processor.conversation import prompts
from khoj.search_filter.date_filter import DateFilter
from khoj.search_filter.file_filter import FileFilter
from khoj.search_filter.word_filter import WordFilter
from khoj.utils import state
from khoj.utils.config import OfflineChatProcessorModel
from khoj.utils.helpers import generate_random_name, is_none_or_empty, timer

logger = logging.getLogger(__name__)


class SubscriptionState(Enum):
    TRIAL = "trial"
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"
    EXPIRED = "expired"
    INVALID = "invalid"


async def set_notion_config(token: str, user: KhojUser):
    notion_config = await NotionConfig.objects.filter(user=user).afirst()
    if not notion_config:
        notion_config = await NotionConfig.objects.acreate(token=token, user=user)
    else:
        notion_config.token = token
        await notion_config.asave()
    return notion_config


def create_khoj_token(user: KhojUser, name=None):
    "Create Khoj API key for user"
    token = f"kk-{secrets.token_urlsafe(32)}"
    name = name or f"{generate_random_name().title()}"
    return KhojApiUser.objects.create(token=token, user=user, name=name)


async def acreate_khoj_token(user: KhojUser, name=None):
    "Create Khoj API key for user"
    token = f"kk-{secrets.token_urlsafe(32)}"
    name = name or f"{generate_random_name().title()}"
    return await KhojApiUser.objects.acreate(token=token, user=user, name=name)


def get_khoj_tokens(user: KhojUser):
    "Get all Khoj API keys for user"
    return list(KhojApiUser.objects.filter(user=user))


async def delete_khoj_token(user: KhojUser, token: str):
    "Delete Khoj API Key for user"
    await KhojApiUser.objects.filter(token=token, user=user).adelete()


async def get_or_create_user(token: dict) -> KhojUser:
    user = await get_user_by_token(token)
    if not user:
        user = await create_user_by_google_token(token)
    return user


async def aget_or_create_user_by_phone_number(phone_number: str) -> KhojUser:
    if is_none_or_empty(phone_number):
        return None
    user = await aget_user_by_phone_number(phone_number)
    if not user:
        user = await acreate_user_by_phone_number(phone_number)
    return user


async def aset_user_phone_number(user: KhojUser, phone_number: str) -> KhojUser:
    if is_none_or_empty(phone_number):
        return None
    phone_number = phone_number.strip()
    if not phone_number.startswith("+"):
        phone_number = f"+{phone_number}"
    existing_user_with_phone_number = await aget_user_by_phone_number(phone_number)
    if existing_user_with_phone_number and existing_user_with_phone_number.id != user.id:
        if is_none_or_empty(existing_user_with_phone_number.email):
            # Transfer conversation history to the new user. If they don't have an associated email, they are effectively a new user
            async for conversation in Conversation.objects.filter(user=existing_user_with_phone_number).aiterator():
                conversation.user = user
                await conversation.asave()

            await existing_user_with_phone_number.adelete()
        else:
            raise HTTPException(status_code=400, detail="Phone number already exists")

    user.phone_number = phone_number
    await user.asave()
    return user


async def aremove_phone_number(user: KhojUser) -> KhojUser:
    user.phone_number = None
    user.verified_phone_number = False
    await user.asave()
    return user


async def acreate_user_by_phone_number(phone_number: str) -> KhojUser:
    if is_none_or_empty(phone_number):
        return None
    user, _ = await KhojUser.objects.filter(phone_number=phone_number).aupdate_or_create(
        defaults={"username": phone_number, "phone_number": phone_number}
    )
    await user.asave()

    await Subscription.objects.acreate(user=user, type="trial")

    return user


async def get_or_create_user_by_email(email: str) -> KhojUser:
    user, _ = await KhojUser.objects.filter(email=email).aupdate_or_create(defaults={"username": email, "email": email})
    await user.asave()

    user_subscription = await Subscription.objects.filter(user=user).afirst()
    if not user_subscription:
        await Subscription.objects.acreate(user=user, type="trial")

    return user


async def create_user_by_google_token(token: dict) -> KhojUser:
    user, _ = await KhojUser.objects.filter(email=token.get("email")).aupdate_or_create(
        defaults={"username": token.get("email"), "email": token.get("email")}
    )
    await user.asave()

    await GoogleUser.objects.acreate(
        sub=token.get("sub"),
        azp=token.get("azp"),
        email=token.get("email"),
        name=token.get("name"),
        given_name=token.get("given_name"),
        family_name=token.get("family_name"),
        picture=token.get("picture"),
        locale=token.get("locale"),
        user=user,
    )

    await Subscription.objects.acreate(user=user, type="trial")

    return user


def set_user_name(user: KhojUser, first_name: str, last_name: str) -> KhojUser:
    user.first_name = first_name
    user.last_name = last_name
    user.save()
    return user


def get_user_name(user: KhojUser):
    full_name = user.get_full_name()
    if not is_none_or_empty(full_name):
        return full_name
    google_profile: GoogleUser = GoogleUser.objects.filter(user=user).first()
    if google_profile:
        return google_profile.given_name

    return None


def get_user_photo(user: KhojUser):
    google_profile: GoogleUser = GoogleUser.objects.filter(user=user).first()
    if google_profile:
        return google_profile.picture

    return None


def get_user_subscription(email: str) -> Optional[Subscription]:
    return Subscription.objects.filter(user__email=email).first()


async def set_user_subscription(
    email: str, is_recurring=None, renewal_date=None, type="standard"
) -> Optional[Subscription]:
    # Get or create the user object and their subscription
    user = await get_or_create_user_by_email(email)
    user_subscription = await Subscription.objects.filter(user=user).afirst()

    # Update the user subscription state
    user_subscription.type = type
    if is_recurring is not None:
        user_subscription.is_recurring = is_recurring
    if renewal_date is False:
        user_subscription.renewal_date = None
    elif renewal_date is not None:
        user_subscription.renewal_date = renewal_date
    await user_subscription.asave()
    return user_subscription


def subscription_to_state(subscription: Subscription) -> str:
    if not subscription:
        return SubscriptionState.INVALID.value
    elif subscription.type == Subscription.Type.TRIAL:
        # Trial subscription is valid for 7 days
        if datetime.now(tz=timezone.utc) - subscription.created_at > timedelta(days=14):
            return SubscriptionState.EXPIRED.value

        return SubscriptionState.TRIAL.value
    elif subscription.is_recurring and subscription.renewal_date >= datetime.now(tz=timezone.utc):
        return SubscriptionState.SUBSCRIBED.value
    elif not subscription.is_recurring and subscription.renewal_date is None:
        return SubscriptionState.EXPIRED.value
    elif not subscription.is_recurring and subscription.renewal_date >= datetime.now(tz=timezone.utc):
        return SubscriptionState.UNSUBSCRIBED.value
    elif not subscription.is_recurring and subscription.renewal_date < datetime.now(tz=timezone.utc):
        return SubscriptionState.EXPIRED.value
    return SubscriptionState.INVALID.value


def get_user_subscription_state(email: str) -> str:
    """Get subscription state of user
    Valid state transitions: trial -> subscribed <-> unsubscribed OR expired
    """
    user_subscription = Subscription.objects.filter(user__email=email).first()
    return subscription_to_state(user_subscription)


async def aget_user_subscription_state(user: KhojUser) -> str:
    """Get subscription state of user
    Valid state transitions: trial -> subscribed <-> unsubscribed OR expired
    """
    user_subscription = await Subscription.objects.filter(user=user).afirst()
    return subscription_to_state(user_subscription)


async def get_user_by_email(email: str) -> KhojUser:
    return await KhojUser.objects.filter(email=email).afirst()


async def aget_user_by_uuid(uuid: str) -> KhojUser:
    return await KhojUser.objects.filter(uuid=uuid).afirst()


async def get_user_by_token(token: dict) -> KhojUser:
    google_user = await GoogleUser.objects.filter(sub=token.get("sub")).select_related("user").afirst()
    if not google_user:
        return None
    return google_user.user


async def aget_user_by_phone_number(phone_number: str) -> KhojUser:
    if is_none_or_empty(phone_number):
        return None
    matched_user = await KhojUser.objects.filter(phone_number=phone_number).prefetch_related("subscription").afirst()

    if not matched_user:
        return None

    # If the user with this phone number does not have an email account with Khoj, return the user
    if is_none_or_empty(matched_user.email):
        return matched_user

    # If the user has an email account with Khoj and a verified number, return the user
    if matched_user.verified_phone_number:
        return matched_user

    return None


async def retrieve_user(session_id: str) -> KhojUser:
    session = SessionStore(session_key=session_id)
    if not await sync_to_async(session.exists)(session_key=session_id):
        raise HTTPException(status_code=401, detail="Invalid session")
    session_data = await sync_to_async(session.load)()
    user = await KhojUser.objects.filter(id=session_data.get("_auth_user_id")).afirst()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid user")
    return user


def get_all_users() -> BaseManager[KhojUser]:
    return KhojUser.objects.all()


def get_user_github_config(user: KhojUser):
    config = GithubConfig.objects.filter(user=user).prefetch_related("githubrepoconfig").first()
    return config


def get_user_notion_config(user: KhojUser):
    config = NotionConfig.objects.filter(user=user).first()
    return config


def delete_user_requests(window: timedelta = timedelta(days=1)):
    return UserRequests.objects.filter(created_at__lte=datetime.now(tz=timezone.utc) - window).delete()


async def aget_user_name(user: KhojUser):
    full_name = user.get_full_name()
    if not is_none_or_empty(full_name):
        return full_name
    google_profile: GoogleUser = await GoogleUser.objects.filter(user=user).afirst()
    if google_profile:
        return google_profile.given_name

    return None


async def set_text_content_config(user: KhojUser, object: Type[models.Model], updated_config):
    deduped_files = list(set(updated_config.input_files)) if updated_config.input_files else None
    deduped_filters = list(set(updated_config.input_filter)) if updated_config.input_filter else None
    await object.objects.filter(user=user).adelete()
    await object.objects.acreate(
        input_files=deduped_files,
        input_filter=deduped_filters,
        index_heading_entries=updated_config.index_heading_entries,
        user=user,
    )


async def set_user_github_config(user: KhojUser, pat_token: str, repos: list):
    config = await GithubConfig.objects.filter(user=user).afirst()

    if not config:
        config = await GithubConfig.objects.acreate(pat_token=pat_token, user=user)
    else:
        config.pat_token = pat_token
        await config.asave()
        await config.githubrepoconfig.all().adelete()

    for repo in repos:
        await GithubRepoConfig.objects.acreate(
            name=repo["name"], owner=repo["owner"], branch=repo["branch"], github_config=config
        )
    return config


def get_user_search_model_or_default(user=None):
    if user and UserSearchModelConfig.objects.filter(user=user).exists():
        return UserSearchModelConfig.objects.filter(user=user).first().setting

    if SearchModelConfig.objects.filter(name="default").exists():
        return SearchModelConfig.objects.filter(name="default").first()
    else:
        SearchModelConfig.objects.create()

    return SearchModelConfig.objects.first()


def get_or_create_search_models():
    search_models = SearchModelConfig.objects.all()
    if search_models.count() == 0:
        SearchModelConfig.objects.create()
        search_models = SearchModelConfig.objects.all()

    return search_models


async def aset_user_search_model(user: KhojUser, search_model_config_id: int):
    config = await SearchModelConfig.objects.filter(id=search_model_config_id).afirst()
    if not config:
        return None
    new_config, _ = await UserSearchModelConfig.objects.aupdate_or_create(user=user, defaults={"setting": config})
    return new_config


async def aget_user_search_model(user: KhojUser):
    config = await UserSearchModelConfig.objects.filter(user=user).prefetch_related("setting").afirst()
    if not config:
        return None
    return config.setting


class ProcessLockAdapters:
    @staticmethod
    def get_process_lock(process_name: str):
        return ProcessLock.objects.filter(name=process_name).first()

    @staticmethod
    def set_process_lock(process_name: str, max_duration_in_seconds: int = 600):
        return ProcessLock.objects.create(name=process_name, max_duration_in_seconds=max_duration_in_seconds)

    @staticmethod
    def is_process_locked(process_name: str):
        process_lock = ProcessLock.objects.filter(name=process_name).first()
        if not process_lock:
            return False
        if process_lock.started_at + timedelta(seconds=process_lock.max_duration_in_seconds) < datetime.now(
            tz=timezone.utc
        ):
            process_lock.delete()
            logger.info(f"🔓 Deleted stale {process_name} process lock on timeout")
            return False
        return True

    @staticmethod
    def remove_process_lock(process_lock: ProcessLock):
        return process_lock.delete()

    @staticmethod
    def run_with_lock(func: Callable, operation: ProcessLock.Operation, max_duration_in_seconds: int = 600, **kwargs):
        # Exit early if process lock is already taken
        if ProcessLockAdapters.is_process_locked(operation):
            logger.debug(f"🔒 Skip executing {func} as {operation} lock is already taken")
            return

        success = False
        process_lock = None
        try:
            # Set process lock
            process_lock = ProcessLockAdapters.set_process_lock(operation, max_duration_in_seconds)
            logger.info(f"🔐 Locked {operation} to execute {func}")

            # Execute Function
            with timer(f"🔒 Run {func} with {operation} process lock", logger):
                func(**kwargs)
            success = True
        except IntegrityError as e:
            logger.debug(f"⚠️ Unable to create the process lock for {func} with {operation}: {e}")
            success = False
        except Exception as e:
            logger.error(f"🚨 Error executing {func} with {operation} process lock: {e}", exc_info=True)
            success = False
        finally:
            # Remove Process Lock
            if process_lock:
                ProcessLockAdapters.remove_process_lock(process_lock)
                logger.info(
                    f"🔓 Unlocked {operation} process after executing {func} {'Succeeded' if success else 'Failed'}"
                )
            else:
                logger.debug(f"Skip removing {operation} process lock as it was not set")


def run_with_process_lock(*args, **kwargs):
    """Wrapper function used for scheduling jobs.
    Required as APScheduler can't discover the `ProcessLockAdapter.run_with_lock' method on its own.
    """
    return ProcessLockAdapters.run_with_lock(*args, **kwargs)


class ClientApplicationAdapters:
    @staticmethod
    async def aget_client_application_by_id(client_id: str, client_secret: str):
        return await ClientApplication.objects.filter(client_id=client_id, client_secret=client_secret).afirst()


class AgentAdapters:
    DEFAULT_AGENT_NAME = "Khoj"
    DEFAULT_AGENT_AVATAR = "https://khoj-web-bucket.s3.amazonaws.com/lamp-128.png"
    DEFAULT_AGENT_SLUG = "khoj"

    @staticmethod
    async def aget_agent_by_slug(agent_slug: str, user: KhojUser):
        return await Agent.objects.filter(
            (Q(slug__iexact=agent_slug.lower())) & (Q(public=True) | Q(creator=user))
        ).afirst()

    @staticmethod
    def get_agent_by_slug(slug: str, user: KhojUser = None):
        if user:
            return Agent.objects.filter((Q(slug__iexact=slug.lower())) & (Q(public=True) | Q(creator=user))).first()
        return Agent.objects.filter(slug__iexact=slug.lower(), public=True).first()

    @staticmethod
    def get_all_accessible_agents(user: KhojUser = None):
        if user:
            return Agent.objects.filter(Q(public=True) | Q(creator=user)).distinct().order_by("created_at")
        return Agent.objects.filter(public=True).order_by("created_at")

    @staticmethod
    async def aget_all_accessible_agents(user: KhojUser = None) -> List[Agent]:
        agents = await sync_to_async(AgentAdapters.get_all_accessible_agents)(user)
        return await sync_to_async(list)(agents)

    @staticmethod
    def get_conversation_agent_by_id(agent_id: int):
        agent = Agent.objects.filter(id=agent_id).first()
        if agent == AgentAdapters.get_default_agent():
            # If the agent is set to the default agent, then return None and let the default application code be used
            return None
        return agent

    @staticmethod
    def get_default_agent():
        return Agent.objects.filter(name=AgentAdapters.DEFAULT_AGENT_NAME).first()

    @staticmethod
    def create_default_agent():
        default_conversation_config = ConversationAdapters.get_default_conversation_config()
        if default_conversation_config is None:
            logger.info("No default conversation config found, skipping default agent creation")
            return None
        default_personality = prompts.personality.format(current_date="placeholder")

        agent = Agent.objects.filter(name=AgentAdapters.DEFAULT_AGENT_NAME).first()

        if agent:
            agent.personality = default_personality
            agent.chat_model = default_conversation_config
            agent.slug = AgentAdapters.DEFAULT_AGENT_SLUG
            agent.name = AgentAdapters.DEFAULT_AGENT_NAME
            agent.save()
        else:
            # The default agent is public and managed by the admin. It's handled a little differently than other agents.
            agent = Agent.objects.create(
                name=AgentAdapters.DEFAULT_AGENT_NAME,
                public=True,
                managed_by_admin=True,
                chat_model=default_conversation_config,
                personality=default_personality,
                tools=["*"],
                avatar=AgentAdapters.DEFAULT_AGENT_AVATAR,
                slug=AgentAdapters.DEFAULT_AGENT_SLUG,
            )
            Conversation.objects.filter(agent=None).update(agent=agent)

        return agent

    @staticmethod
    async def aget_default_agent():
        return await Agent.objects.filter(name=AgentAdapters.DEFAULT_AGENT_NAME).afirst()


class PublicConversationAdapters:
    @staticmethod
    def get_public_conversation_by_slug(slug: str):
        return PublicConversation.objects.filter(slug=slug).first()

    @staticmethod
    def get_public_conversation_url(public_conversation: PublicConversation):
        # Public conversations are viewable by anyone, but not editable.
        return f"/share/chat/{public_conversation.slug}/"


class ConversationAdapters:
    @staticmethod
    def make_public_conversation_copy(conversation: Conversation):
        return PublicConversation.objects.create(
            source_owner=conversation.user,
            agent=conversation.agent,
            conversation_log=conversation.conversation_log,
            slug=conversation.slug,
            title=conversation.title,
        )

    @staticmethod
    def get_conversation_by_user(
        user: KhojUser, client_application: ClientApplication = None, conversation_id: int = None
    ) -> Optional[Conversation]:
        if conversation_id:
            conversation = (
                Conversation.objects.filter(user=user, client=client_application, id=conversation_id)
                .order_by("-updated_at")
                .first()
            )
        else:
            agent = AgentAdapters.get_default_agent()
            conversation = (
                Conversation.objects.filter(user=user, client=client_application).order_by("-updated_at").first()
            ) or Conversation.objects.create(user=user, client=client_application, agent=agent)

        return conversation

    @staticmethod
    def get_conversation_sessions(user: KhojUser, client_application: ClientApplication = None):
        return Conversation.objects.filter(user=user, client=client_application).order_by("-updated_at")

    @staticmethod
    async def aset_conversation_title(
        user: KhojUser, client_application: ClientApplication, conversation_id: int, title: str
    ):
        conversation = await Conversation.objects.filter(
            user=user, client=client_application, id=conversation_id
        ).afirst()
        if conversation:
            conversation.title = title
            await conversation.asave()
            return conversation
        return None

    @staticmethod
    def get_conversation_by_id(conversation_id: int):
        return Conversation.objects.filter(id=conversation_id).first()

    @staticmethod
    async def acreate_conversation_session(
        user: KhojUser, client_application: ClientApplication = None, agent_slug: str = None
    ):
        if agent_slug:
            agent = await AgentAdapters.aget_agent_by_slug(agent_slug, user)
            if agent is None:
                raise HTTPException(status_code=400, detail="No such agent currently exists.")
            return await Conversation.objects.acreate(user=user, client=client_application, agent=agent)
        agent = await AgentAdapters.aget_default_agent()
        return await Conversation.objects.acreate(user=user, client=client_application, agent=agent)

    @staticmethod
    async def aget_conversation_by_user(
        user: KhojUser, client_application: ClientApplication = None, conversation_id: int = None, title: str = None
    ) -> Optional[Conversation]:
        if conversation_id:
            return await Conversation.objects.filter(user=user, client=client_application, id=conversation_id).afirst()
        elif title:
            return await Conversation.objects.filter(user=user, client=client_application, title=title).afirst()
        else:
            conversation = Conversation.objects.filter(user=user, client=client_application).order_by("-updated_at")

        if await conversation.aexists():
            return await conversation.prefetch_related("agent").afirst()

        return await (
            Conversation.objects.filter(user=user, client=client_application).order_by("-updated_at").afirst()
        ) or await Conversation.objects.acreate(user=user, client=client_application)

    @staticmethod
    async def adelete_conversation_by_user(
        user: KhojUser, client_application: ClientApplication = None, conversation_id: int = None
    ):
        if conversation_id:
            return await Conversation.objects.filter(user=user, client=client_application, id=conversation_id).adelete()
        return await Conversation.objects.filter(user=user, client=client_application).adelete()

    @staticmethod
    def has_any_conversation_config(user: KhojUser):
        return ChatModelOptions.objects.filter(user=user).exists()

    @staticmethod
    def get_openai_conversation_config():
        return OpenAIProcessorConversationConfig.objects.filter().first()

    @staticmethod
    def has_valid_openai_conversation_config():
        return OpenAIProcessorConversationConfig.objects.filter().exists()

    @staticmethod
    async def aset_user_conversation_processor(user: KhojUser, conversation_processor_config_id: int):
        config = await ChatModelOptions.objects.filter(id=conversation_processor_config_id).afirst()
        if not config:
            return None
        new_config = await UserConversationConfig.objects.aupdate_or_create(user=user, defaults={"setting": config})
        return new_config

    @staticmethod
    def get_conversation_config(user: KhojUser):
        config = UserConversationConfig.objects.filter(user=user).first()
        if not config:
            return None
        return config.setting

    @staticmethod
    async def aget_conversation_config(user: KhojUser):
        config = await UserConversationConfig.objects.filter(user=user).prefetch_related("setting").afirst()
        if not config:
            return None
        return config.setting

    @staticmethod
    def get_default_conversation_config():
        server_chat_settings = ServerChatSettings.objects.first()
        if server_chat_settings is None or server_chat_settings.default_model is None:
            return ChatModelOptions.objects.filter().first()
        return server_chat_settings.default_model

    @staticmethod
    async def aget_default_conversation_config():
        server_chat_settings: ServerChatSettings = (
            await ServerChatSettings.objects.filter()
            .prefetch_related("default_model", "default_model__openai_config")
            .afirst()
        )
        if server_chat_settings is None or server_chat_settings.default_model is None:
            return await ChatModelOptions.objects.filter().prefetch_related("openai_config").afirst()
        return server_chat_settings.default_model

    @staticmethod
    async def aget_summarizer_conversation_config():
        server_chat_settings: ServerChatSettings = (
            await ServerChatSettings.objects.filter()
            .prefetch_related(
                "summarizer_model", "default_model", "default_model__openai_config", "summarizer_model__openai_config"
            )
            .afirst()
        )
        if server_chat_settings is None or (
            server_chat_settings.summarizer_model is None and server_chat_settings.default_model is None
        ):
            return await ChatModelOptions.objects.filter().afirst()
        return server_chat_settings.summarizer_model or server_chat_settings.default_model

    @staticmethod
    def create_conversation_from_public_conversation(
        user: KhojUser, public_conversation: PublicConversation, client_app: ClientApplication
    ):
        return Conversation.objects.create(
            user=user,
            conversation_log=public_conversation.conversation_log,
            client=client_app,
            slug=public_conversation.slug,
            title=public_conversation.title,
            agent=public_conversation.agent,
        )

    @staticmethod
    def save_conversation(
        user: KhojUser,
        conversation_log: dict,
        client_application: ClientApplication = None,
        conversation_id: int = None,
        user_message: str = None,
    ):
        slug = user_message.strip()[:200] if user_message else None
        if conversation_id:
            conversation = Conversation.objects.filter(user=user, client=client_application, id=conversation_id).first()
        else:
            conversation = (
                Conversation.objects.filter(user=user, client=client_application).order_by("-updated_at").first()
            )

        if conversation:
            conversation.conversation_log = conversation_log
            conversation.slug = slug
            conversation.updated_at = datetime.now(tz=timezone.utc)
            conversation.save()
        else:
            Conversation.objects.create(
                user=user, conversation_log=conversation_log, client=client_application, slug=slug
            )

    @staticmethod
    def get_conversation_processor_options():
        return ChatModelOptions.objects.all()

    @staticmethod
    def set_conversation_processor_config(user: KhojUser, new_config: ChatModelOptions):
        user_conversation_config, _ = UserConversationConfig.objects.get_or_create(user=user)
        user_conversation_config.setting = new_config
        user_conversation_config.save()

    @staticmethod
    async def aget_user_conversation_config(user: KhojUser):
        config = (
            await UserConversationConfig.objects.filter(user=user).prefetch_related("setting__openai_config").afirst()
        )
        if not config:
            return None
        return config.setting

    @staticmethod
    async def get_speech_to_text_config():
        return await SpeechToTextModelOptions.objects.filter().afirst()

    @staticmethod
    async def aget_conversation_starters(user: KhojUser, max_results=3):
        all_questions = []
        if await ReflectiveQuestion.objects.filter(user=user).aexists():
            all_questions = await sync_to_async(ReflectiveQuestion.objects.filter(user=user).values_list)(
                "question", flat=True
            )

        all_questions = await sync_to_async(ReflectiveQuestion.objects.filter(user=None).values_list)(
            "question", flat=True
        )

        all_questions = await sync_to_async(list)(all_questions)  # type: ignore
        if len(all_questions) < max_results:
            return all_questions

        return random.sample(all_questions, max_results)

    @staticmethod
    def get_valid_conversation_config(user: KhojUser, conversation: Conversation):
        agent: Agent = conversation.agent if AgentAdapters.get_default_agent() != conversation.agent else None
        if agent and agent.chat_model:
            conversation_config = conversation.agent.chat_model
        else:
            conversation_config = ConversationAdapters.get_conversation_config(user)

        if conversation_config is None:
            conversation_config = ConversationAdapters.get_default_conversation_config()

        if conversation_config.model_type == "offline":
            if state.offline_chat_processor_config is None or state.offline_chat_processor_config.loaded_model is None:
                chat_model = conversation_config.chat_model
                max_tokens = conversation_config.max_prompt_size
                state.offline_chat_processor_config = OfflineChatProcessorModel(chat_model, max_tokens)

            return conversation_config

        if (
            conversation_config.model_type == "openai" or conversation_config.model_type == "anthropic"
        ) and conversation_config.openai_config:
            return conversation_config

        else:
            raise ValueError("Invalid conversation config - either configure offline chat or openai chat")

    @staticmethod
    async def aget_text_to_image_model_config():
        return await TextToImageModelConfig.objects.filter().afirst()


class EntryAdapters:
    word_filer = WordFilter()
    file_filter = FileFilter()
    date_filter = DateFilter()

    @staticmethod
    def does_entry_exist(user: KhojUser, hashed_value: str) -> bool:
        return Entry.objects.filter(user=user, hashed_value=hashed_value).exists()

    @staticmethod
    def delete_entry_by_file(user: KhojUser, file_path: str):
        deleted_count, _ = Entry.objects.filter(user=user, file_path=file_path).delete()
        return deleted_count

    @staticmethod
    def delete_all_entries_by_type(user: KhojUser, file_type: str = None):
        if file_type is None:
            deleted_count, _ = Entry.objects.filter(user=user).delete()
        else:
            deleted_count, _ = Entry.objects.filter(user=user, file_type=file_type).delete()
        return deleted_count

    @staticmethod
    def delete_all_entries(user: KhojUser, file_source: str = None):
        if file_source is None:
            deleted_count, _ = Entry.objects.filter(user=user).delete()
        else:
            deleted_count, _ = Entry.objects.filter(user=user, file_source=file_source).delete()
        return deleted_count

    @staticmethod
    async def adelete_all_entries(user: KhojUser, file_source: str = None):
        if file_source is None:
            return await Entry.objects.filter(user=user).adelete()
        return await Entry.objects.filter(user=user, file_source=file_source).adelete()

    @staticmethod
    def get_existing_entry_hashes_by_file(user: KhojUser, file_path: str):
        return Entry.objects.filter(user=user, file_path=file_path).values_list("hashed_value", flat=True)

    @staticmethod
    def delete_entry_by_hash(user: KhojUser, hashed_values: List[str]):
        Entry.objects.filter(user=user, hashed_value__in=hashed_values).delete()

    @staticmethod
    def get_entries_by_date_filter(entry: BaseManager[Entry], start_date: date, end_date: date):
        return entry.filter(
            entrydates__date__gte=start_date,
            entrydates__date__lte=end_date,
        )

    @staticmethod
    def user_has_entries(user: KhojUser):
        return Entry.objects.filter(user=user).exists()

    @staticmethod
    async def auser_has_entries(user: KhojUser):
        return await Entry.objects.filter(user=user).aexists()

    @staticmethod
    async def adelete_entry_by_file(user: KhojUser, file_path: str):
        return await Entry.objects.filter(user=user, file_path=file_path).adelete()

    @staticmethod
    def get_all_filenames_by_source(user: KhojUser, file_source: str):
        return (
            Entry.objects.filter(user=user, file_source=file_source)
            .distinct("file_path")
            .values_list("file_path", flat=True)
        )

    @staticmethod
    def get_size_of_indexed_data_in_mb(user: KhojUser):
        entries = Entry.objects.filter(user=user).iterator()
        total_size = sum(sys.getsizeof(entry.compiled) for entry in entries)
        return total_size / 1024 / 1024

    @staticmethod
    def apply_filters(user: KhojUser, query: str, file_type_filter: str = None):
        q_filter_terms = Q()

        explicit_word_terms = EntryAdapters.word_filer.get_filter_terms(query)
        file_filters = EntryAdapters.file_filter.get_filter_terms(query)
        date_filters = EntryAdapters.date_filter.get_query_date_range(query)

        if len(explicit_word_terms) == 0 and len(file_filters) == 0 and len(date_filters) == 0:
            return Entry.objects.filter(user=user)

        for term in explicit_word_terms:
            if term.startswith("+"):
                q_filter_terms &= Q(raw__icontains=term[1:])
            elif term.startswith("-"):
                q_filter_terms &= ~Q(raw__icontains=term[1:])

        q_file_filter_terms = Q()

        if len(file_filters) > 0:
            for term in file_filters:
                q_file_filter_terms |= Q(file_path__regex=term)

            q_filter_terms &= q_file_filter_terms

        if len(date_filters) > 0:
            min_date, max_date = date_filters
            if min_date is not None:
                # Convert the min_date timestamp to yyyy-mm-dd format
                formatted_min_date = date.fromtimestamp(min_date).strftime("%Y-%m-%d")
                q_filter_terms &= Q(embeddings_dates__date__gte=formatted_min_date)
            if max_date is not None:
                # Convert the max_date timestamp to yyyy-mm-dd format
                formatted_max_date = date.fromtimestamp(max_date).strftime("%Y-%m-%d")
                q_filter_terms &= Q(embeddings_dates__date__lte=formatted_max_date)

        relevant_entries = Entry.objects.filter(user=user).filter(
            q_filter_terms,
        )
        if file_type_filter:
            relevant_entries = relevant_entries.filter(file_type=file_type_filter)
        return relevant_entries

    @staticmethod
    def search_with_embeddings(
        user: KhojUser,
        embeddings: Tensor,
        max_results: int = 10,
        file_type_filter: str = None,
        raw_query: str = None,
        max_distance: float = math.inf,
    ):
        relevant_entries = EntryAdapters.apply_filters(user, raw_query, file_type_filter)
        relevant_entries = relevant_entries.filter(user=user).annotate(
            distance=CosineDistance("embeddings", embeddings)
        )
        relevant_entries = relevant_entries.filter(distance__lte=max_distance)

        if file_type_filter:
            relevant_entries = relevant_entries.filter(file_type=file_type_filter)
        relevant_entries = relevant_entries.order_by("distance")
        return relevant_entries[:max_results]

    @staticmethod
    def get_unique_file_types(user: KhojUser):
        return Entry.objects.filter(user=user).values_list("file_type", flat=True).distinct()

    @staticmethod
    def get_unique_file_sources(user: KhojUser):
        return Entry.objects.filter(user=user).values_list("file_source", flat=True).distinct().all()


class AutomationAdapters:
    @staticmethod
    def get_automations(user: KhojUser) -> Iterable[Job]:
        all_automations: Iterable[Job] = state.scheduler.get_jobs()
        for automation in all_automations:
            if automation.id.startswith(f"automation_{user.uuid}_"):
                yield automation

    @staticmethod
    def get_automation_metadata(user: KhojUser, automation: Job):
        # Perform validation checks
        # Check if user is allowed to delete this automation id
        if not automation.id.startswith(f"automation_{user.uuid}_"):
            raise ValueError("Invalid automation id")

        automation_metadata = json.loads(automation.name)
        crontime = automation_metadata["crontime"]
        timezone = automation.next_run_time.strftime("%Z")
        schedule = f"{cron_descriptor.get_description(crontime)} {timezone}"
        return {
            "id": automation.id,
            "subject": automation_metadata["subject"],
            "query_to_run": re.sub(r"^/automated_task\s*", "", automation_metadata["query_to_run"]),
            "scheduling_request": automation_metadata["scheduling_request"],
            "schedule": schedule,
            "crontime": crontime,
            "next": automation.next_run_time.strftime("%Y-%m-%d %I:%M %p %Z"),
        }

    @staticmethod
    def get_job_last_run(user: KhojUser, automation: Job):
        # Perform validation checks
        # Check if user is allowed to delete this automation id
        if not automation.id.startswith(f"automation_{user.uuid}_"):
            raise ValueError("Invalid automation id")

        django_job = DjangoJob.objects.filter(id=automation.id).first()
        execution = DjangoJobExecution.objects.filter(job=django_job, status="Executed")

        last_run_time = None

        if execution.exists():
            last_run_time = execution.latest("run_time").run_time

        return last_run_time.strftime("%Y-%m-%d %I:%M %p %Z") if last_run_time else None

    @staticmethod
    def get_automations_metadata(user: KhojUser):
        for automation in AutomationAdapters.get_automations(user):
            yield AutomationAdapters.get_automation_metadata(user, automation)

    @staticmethod
    def get_automation(user: KhojUser, automation_id: str) -> Job:
        # Perform validation checks
        # Check if user is allowed to delete this automation id
        if not automation_id.startswith(f"automation_{user.uuid}_"):
            raise ValueError("Invalid automation id")
        # Check if automation with this id exist
        automation: Job = state.scheduler.get_job(job_id=automation_id)
        if not automation:
            raise ValueError("Invalid automation id")

        return automation

    @staticmethod
    def delete_automation(user: KhojUser, automation_id: str):
        # Get valid, user-owned automation
        automation: Job = AutomationAdapters.get_automation(user, automation_id)

        # Collate info about user automation to be deleted
        automation_metadata = AutomationAdapters.get_automation_metadata(user, automation)

        automation.remove()
        return automation_metadata
