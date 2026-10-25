"""Dependency injection container: configuration, presets, database, job runner and the research service."""

from dcc_backend_common.usage_tracking import UsageTrackingService
from dependency_injector import containers, providers

from bebbi_deep_research.engine import load_config
from bebbi_deep_research.services.db import DB
from bebbi_deep_research.services.jobs import JobRunner, make_agent_factory
from bebbi_deep_research.services.research_service import ResearchService
from bebbi_deep_research.utils.app_config import AppConfig


class Container(containers.DeclarativeContainer):
    """Application container; tests override `config`, `agent_factory` and `http_factory`."""

    wiring_config = containers.WiringConfiguration(packages=["bebbi_deep_research.routers"])

    config = providers.Singleton(AppConfig.from_env)
    presets = providers.Singleton(load_config, config.provided.presets_path)
    db = providers.Singleton(DB, config.provided.database_url)
    agent_factory = providers.Singleton(make_agent_factory, presets, config)
    http_factory = providers.Object(None)
    usage = providers.Singleton(UsageTrackingService, config.provided.hmac_secret)
    job_runner = providers.Singleton(JobRunner, config, db, presets, agent_factory, http_factory)
    research_service = providers.Singleton(ResearchService, config, presets, db, agent_factory, usage, job_runner)
