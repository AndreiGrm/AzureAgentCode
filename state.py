"""Stato degli work item mantenuto tramite tag custom su Azure Boards.

I tag vivono nel campo System.Tags del work item: lo stato e' quindi
condiviso e persistente tra run diversi di ingest_loop.py e review_loop.py,
senza bisogno di un datastore locale. Ogni operazione e' idempotente:
aggiungere un tag gia' presente, o rimuoverne uno assente, non ha effetto.
"""
from __future__ import annotations

import html
import logging
import re

from azure.devops.v7_1.work_item_tracking.work_item_tracking_client import (
    WorkItemTrackingClient,
)
from azure.devops.v7_1.work_item_tracking.models import JsonPatchOperation

from retry import retry_once

logger = logging.getLogger(__name__)

TAG_BRANCH_CREATED = "agent:branch-created"
TAG_IMPLEMENTED = "agent:implemented"
TAG_PR_OPEN = "agent:pr-open"
TAG_BLOCKED = "agent:blocked"
TAG_DECOMPOSED = "agent:decomposed"
TAG_PLAN_READY = "agent:plan-ready"
TAG_PLAN_APPROVED = "agent:plan-approved"
TAG_FIX_REQUESTED = "agent:fix-requested"
TAG_COMPLETED = "agent:completed"
TAG_ABANDONED = "agent:abandoned"

_TAG_SEPARATOR = "; "

_BLOCK_BREAK_RE = re.compile(r"(?i)</\s*(p|div|li|br|h[1-6]|tr)\s*/?>")
_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_ANCHOR_RE = re.compile(r'(?is)<a\s+[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>')
_FIGMA_URL_RE = re.compile(r"""https?://(?:www\.)?figma\.com/[^\s"'<>]+""", re.IGNORECASE)


def _replace_anchor(match: re.Match) -> str:
    """Un link cliccabile (<a href="URL">testo</a>) diventerebbe solo
    "testo" se si stripano i tag senza guardare l'href: qui lo teniamo come
    "testo (URL)" cosi' un link Figma/Confluence/Jira incorporato come testo
    non e' mai perso silenziosamente."""
    href, inner_html = match.group(1), match.group(2)
    inner_text = _TAG_RE.sub("", inner_html).strip()
    if not inner_text or inner_text == href:
        return href
    return f"{inner_text} ({href})"


def html_to_plain_text(raw_html: str) -> str:
    """Conversione minimale HTML->testo per Description/Acceptance Criteria
    di Azure DevOps (campi rich-text): nessuna gestione di immagini/allegati,
    solo testo leggibile per il pannello "cosa deve essere fatto" della
    dashboard. Gli href dei link cliccabili sono preservati (vedi _replace_anchor)."""
    if not raw_html:
        return ""
    raw_html = _ANCHOR_RE.sub(_replace_anchor, raw_html)
    text = _BLOCK_BREAK_RE.sub("\n", raw_html)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()


def extract_figma_urls(raw_html: str) -> list[str]:
    """Trova i link a Figma in un campo Azure DevOps (Description/Acceptance
    Criteria/piano), sia scritti come URL semplice sia come href di un link
    cliccabile (il regex lavora sull'HTML grezzo, quindi vede anche gli
    href prima che vengano eventualmente stripati)."""
    if not raw_html:
        return []
    urls = set()
    for match in _FIGMA_URL_RE.finditer(raw_html):
        url = match.group(0).rstrip("\"'<>).,;")
        urls.add(url)
    return sorted(urls)


@retry_once()
def _get_tags_field(wit_client: WorkItemTrackingClient, work_item_id: int) -> str:
    item = wit_client.get_work_item(work_item_id, fields=["System.Tags"])
    return item.fields.get("System.Tags", "") or ""


def get_tags(wit_client: WorkItemTrackingClient, work_item_id: int) -> set[str]:
    """Ritorna l'insieme dei tag correnti di un work item."""
    raw = _get_tags_field(wit_client, work_item_id)
    return {tag.strip() for tag in raw.split(";") if tag.strip()}


def has_tag(wit_client: WorkItemTrackingClient, work_item_id: int, tag: str) -> bool:
    return tag in get_tags(wit_client, work_item_id)


@retry_once()
def add_tag(wit_client: WorkItemTrackingClient, project: str, work_item_id: int, tag: str) -> None:
    """Aggiunge un tag al work item, senza duplicarlo se e' gia' presente.

    Usa op="replace" e non "add": Azure DevOps tratta un op "add" su
    System.Tags come un'UNIONE con i tag gia' presenti, non come una
    sostituzione del valore del campo. Con "add" non e' quindi possibile
    rimuovere un tag (vedi remove_tag), e "replace" e' l'unica opzione che
    scrive esattamente il valore fornito.
    """
    current = get_tags(wit_client, work_item_id)
    if tag in current:
        return
    current.add(tag)
    patch = [
        JsonPatchOperation(
            op="replace",
            path="/fields/System.Tags",
            value=_TAG_SEPARATOR.join(sorted(current)),
        )
    ]
    wit_client.update_work_item(patch, work_item_id, project=project)
    logger.info("Work item %s: aggiunto tag %s", work_item_id, tag)


@retry_once()
def remove_tag(wit_client: WorkItemTrackingClient, project: str, work_item_id: int, tag: str) -> None:
    """Rimuove un tag dal work item, senza effetto se non era presente."""
    current = get_tags(wit_client, work_item_id)
    if tag not in current:
        return
    current.discard(tag)
    patch = [
        JsonPatchOperation(
            op="replace",
            path="/fields/System.Tags",
            value=_TAG_SEPARATOR.join(sorted(current)),
        )
    ]
    wit_client.update_work_item(patch, work_item_id, project=project)
    logger.info("Work item %s: rimosso tag %s", work_item_id, tag)


@retry_once()
def add_note(wit_client: WorkItemTrackingClient, project: str, work_item_id: int, text: str) -> None:
    """Salva una nota (es. l'URL della PR aperta) nella discussione del work item.

    Usa System.History (il campo che alimenta la tab "Discussione" su Azure
    Boards): la WorkItemTrackingClient di questa versione dell'SDK non
    espone un metodo add_comment dedicato.
    """
    patch = [JsonPatchOperation(op="add", path="/fields/System.History", value=text)]
    wit_client.update_work_item(patch, work_item_id, project=project)
    logger.info("Work item %s: aggiunta nota nella discussione", work_item_id)
