"""Chat do assistente hídrico (Web RAG com tool calling no Claude).

POST /api/chat recebe a mensagem e o histórico da conversa; o Claude decide
quando buscar na web e cita as fontes.

Busca:
  - com BING_SUBSCRIPTION_KEY definida: ferramenta cliente `bing_search`
    (Bing Web Search v7, chamada daqui com httpx);
  - sem a chave: busca web nativa do Claude (server tool `web_search`), já que
    a Bing Search API v7 foi descontinuada pela Microsoft em ago/2025.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Literal

import anthropic
import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

from . import rag

# --- variáveis de ambiente (injete no .env / docker-compose / App Service) ---
BING_SUBSCRIPTION_KEY = os.environ.get("BING_SUBSCRIPTION_KEY", "")
BING_SEARCH_URL = os.environ.get("BING_SEARCH_URL", "https://api.bing.microsoft.com/v7.0/search")

SYSTEM = (
    "Você é um assistente de gestão hídrica para a República Democrática do Congo (RDC). Sempre que questionado sobre dados, "
    "legislação ou hidrogeologia, use a ferramenta de busca do Bing para encontrar informações recentes e "
    "cite a fonte na resposta. Responda em português, de forma objetiva, e termine listando as URLs usadas."
)
MAX_HISTORY = 20
MAX_STEPS = 6
log = logging.getLogger("hidro.chat")
router = APIRouter()


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatTurn] = []


def bing_search(query: str, count: int = 5) -> list[dict]:
    r = httpx.get(BING_SEARCH_URL, params={"q": query, "count": count, "textDecorations": False},
                  headers={"Ocp-Apim-Subscription-Key": BING_SUBSCRIPTION_KEY}, timeout=10)
    r.raise_for_status()
    return [{"title": v.get("name", ""), "url": v.get("url", ""), "snippet": v.get("snippet", "")}
            for v in r.json().get("webPages", {}).get("value", [])]


BING_TOOL = {
    "name": "bing_search",
    "description": "Pesquisa na web (Bing) e devolve título, URL e trecho dos resultados. Use para dados, "
                   "clima, hidrogeologia e legislação recentes da República Democrática do Congo (RDC).",
    "strict": True,
    "input_schema": {"type": "object", "properties": {"query": {"type": "string", "description": "termos da busca"}},
                     "required": ["query"], "additionalProperties": False},
}
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}


def _mode() -> str:
    if rag.status() != "claude":
        return "sem_ia"
    return "bing" if BING_SUBSCRIPTION_KEY else "web_search"


def _add_source(sources: dict, url: str, title: str) -> None:
    if url and url not in sources:
        sources[url] = {"title": title or url, "url": url}


def _run_agent(messages: list[dict], tools: list[dict]) -> tuple[str, dict]:
    sources: dict[str, dict] = {}
    for _ in range(MAX_STEPS):
        resp = rag._client().beta.messages.create(
            model=rag.MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},
            system=SYSTEM,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "refusal":
            return "Não posso ajudar com essa pergunta.", sources
        messages.append({"role": "assistant", "content": resp.content})

        for b in resp.content:
            if b.type == "web_search_tool_result" and isinstance(b.content, list):
                for res in b.content:
                    _add_source(sources, res.url, res.title)
            elif b.type == "text":
                for c in b.citations or []:
                    _add_source(sources, getattr(c, "url", ""), getattr(c, "title", ""))

        if resp.stop_reason == "pause_turn":
            continue
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip(), sources

        results = []
        for b in resp.content:
            if b.type != "tool_use" or b.name != "bing_search":
                continue
            try:
                hits = bing_search(b.input["query"])
                for h in hits:
                    _add_source(sources, h["url"], h["title"])
                text = "\n\n".join(f"{h['title']}\n{h['url']}\n{h['snippet']}" for h in hits) or "Nenhum resultado."
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": text})
            except (httpx.HTTPError, KeyError, ValueError) as e:
                results.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                                "content": f"falha na busca: {e}"})
        messages.append({"role": "user", "content": results})
    return "Não consegui concluir a pesquisa a tempo; tente reformular a pergunta.", sources


@router.post("/api/chat")
def chat(req: ChatRequest):
    mode = _mode()
    if mode == "sem_ia":
        return {"mode": mode, "answer": "O assistente está desativado: defina ANTHROPIC_API_KEY no servidor.",
                "sources": []}
    messages = [{"role": t.role, "content": t.content} for t in req.history[-MAX_HISTORY:] if t.content.strip()]
    while messages and messages[0]["role"] != "user":  # a conversa precisa começar pelo usuário
        messages.pop(0)
    messages.append({"role": "user", "content": req.message})
    try:
        answer, sources = _run_agent(messages, [BING_TOOL] if mode == "bing" else [WEB_SEARCH_TOOL])
    except anthropic.APIError as e:
        log.warning("falha no Claude: %s", e)
        return {"mode": mode, "answer": "O assistente está indisponível no momento. Tente novamente.",
                "sources": [], "error": str(e)}
    return {"mode": mode, "model": rag.MODEL, "answer": re.sub(r"\n{3,}", "\n\n", answer),
            "sources": list(sources.values())}
