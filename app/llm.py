"""All LLM calls go through llm(task, schema, prompt)."""
import typing
from typing import Literal, get_args, get_origin

from pydantic import BaseModel
from pydantic_ai import Agent, BinaryContent, NativeOutput

from app import config

_agents: dict[tuple, Agent] = {}


def _placeholder(schema: type[BaseModel]) -> BaseModel:
    """Schema-valid dummy used when OFFLINE=1."""
    def val(t):
        o = get_origin(t)
        if o is Literal:
            return get_args(t)[0]
        if o in (typing.Union, getattr(__import__("types"), "UnionType")):
            return None
        if o is list:
            return []
        if o is dict or t is dict:
            return {}
        if t is bool:
            return True
        if t is int:
            return 0
        if t is float:
            return 0.9
        if t is str:
            return "placeholder"
        if isinstance(t, type) and issubclass(t, BaseModel):
            return _placeholder(t)
        return None
    data = {n: val(f.annotation) for n, f in schema.model_fields.items() if f.is_required()}
    return schema.model_validate(data)


def llm(task: str, schema: type[BaseModel], prompt: str,
        images: list[bytes] | None = None, model: str | None = None) -> BaseModel:
    if config.settings.OFFLINE:
        return _placeholder(schema)
    name = model or config.settings.LLM_MODEL
    key = (schema, name)
    if key not in _agents:
        _agents[key] = Agent(name, output_type=NativeOutput(schema),
                             system_prompt=f"You are a step in a feedback triage pipeline. Task: {task}.")
    parts: list = [prompt] + [BinaryContent(data=b, media_type="image/png") for b in images or []]
    return _agents[key].run_sync(parts).output
