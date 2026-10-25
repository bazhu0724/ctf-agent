"""Pydantic Settings — credentials from .env file + environment variables."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Competition platform
    ctf_platform: str = "ctfd"

    # CTFd
    ctfd_url: str = "http://localhost:8000"
    ctfd_user: str = "admin"
    ctfd_pass: str = "admin"
    ctfd_token: str = ""

    # Ret2Shell (RET2SHELL_TOKEN falls back to CTFD_TOKEN for compatibility)
    ret2shell_token: str = ""
    ret2shell_game_id: int = 0
    ret2shell_auto_start_instances: bool = True

    # CTF+ (CTFPLUS_TOKEN falls back to CTFD_TOKEN for compatibility)
    ctfplus_token: str = ""
    ctfplus_competition_id: str = ""
    ctfplus_problem_bank_id: str = ""

    # API Keys
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"

    # Provider-specific (optional, for Bedrock/Azure/Zen fallback)
    aws_region: str = "us-east-1"
    aws_bearer_token: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    opencode_zen_api_key: str = ""

    # Magpie — local OpenAI-compatible gateway (free worker pool)
    magpie_base_url: str = "http://127.0.0.1:3425/v1"
    magpie_api_key: str = "magpie-codex"
    magpie_max_tokens: int = Field(default=8192, ge=1)
    solver_request_limit: int = Field(default=100, ge=1)
    solver_max_bumps: int = Field(default=2, ge=0)

    # Infra
    sandbox_image: str = "ctf-sandbox"
    max_concurrent_challenges: int = 3
    split_models_across_challenges: bool = True
    prefetch_all_challenges: bool = True
    challenge_prefetch_concurrency: int = 6
    max_attempts_per_challenge: int = 3
    container_memory_limit: str = "16g"
    generate_writeups: bool = True
    writeups_dir: str = "writeups"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
