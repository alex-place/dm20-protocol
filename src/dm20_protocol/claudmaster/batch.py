"""
Message Batches API path for offline Claudmaster jobs.

Session recaps, module/chapter summaries and NPC voice sheets are not
latency-sensitive, so they can go through the Message Batches API at ~50%
of the standard price. Batches usually finish within an hour (max 24h).

Batch requests never carry server-side ``fallbacks`` (rejected on the
Batches API) or other request-only betas; refusals are still handled per
result. Enable with ``ClaudmasterConfig.enable_batch_offline_jobs``.

Usage::

    runner = BatchJobRunner(client)            # client: AnthropicLLMClient
    batch_id = await runner.submit([
        session_recap_job("recap-s4", facts_text),
        module_summary_job("lmop-ch1", chapter_text),
    ])
    ...                                        # later
    if await runner.is_done(batch_id):
        results = await runner.results(batch_id)   # {custom_id: BatchResult}
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from .llm_client import REFUSAL_NARRATION, AnthropicLLMClient, extract_text

logger = logging.getLogger("dm20-protocol")

_CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

SESSION_RECAP_SYSTEM = """\
You write "Previously on..." recaps for a D&D campaign. Use only the facts \
provided; do not invent events. Write in second person, two or three short \
paragraphs, under 250 words, ending with a hook for what comes next."""

MODULE_SUMMARY_SYSTEM = """\
You summarise chapters of a published D&D adventure module for a game master. \
Keep every named NPC, location, encounter, treasure and cross-reference to \
other chapters. Use headings per area and bullet points; no flavour prose."""


@dataclass
class BatchJob:
    """One offline generation job."""

    custom_id: str
    system: str
    prompt: str
    max_tokens: int = 8000
    effort: str | None = None

    def __post_init__(self) -> None:
        if not _CUSTOM_ID_RE.match(self.custom_id):
            raise ValueError(
                f"custom_id {self.custom_id!r} must be 1-64 chars of letters, digits, '_' or '-'"
            )


@dataclass
class BatchResult:
    custom_id: str
    status: str  # succeeded | refused | errored | canceled | expired
    text: str = ""
    error: str | None = None


def session_recap_job(custom_id: str, facts: str, *, max_tokens: int = 8000) -> BatchJob:
    """Batch job for a session recap from verified facts."""
    return BatchJob(custom_id, SESSION_RECAP_SYSTEM, f"Session facts:\n{facts}", max_tokens)


def module_summary_job(custom_id: str, chapter_text: str, *, max_tokens: int = 16000) -> BatchJob:
    """Batch job for a module chapter summary (whole chapter, no chunking)."""
    return BatchJob(custom_id, MODULE_SUMMARY_SYSTEM, f"Chapter text:\n{chapter_text}", max_tokens)


class BatchJobRunner:
    """Submit and collect offline jobs through ``client.messages.batches``."""

    def __init__(self, client: AnthropicLLMClient) -> None:
        self.llm = client

    def build_requests(self, jobs: list[BatchJob]) -> list[dict[str, Any]]:
        ids = [j.custom_id for j in jobs]
        if len(set(ids)) != len(ids):
            raise ValueError("Batch custom_ids must be unique")
        requests = []
        for job in jobs:
            params, betas = self.llm.build_params(
                [{"role": "user", "content": self.llm.build_user_content(job.prompt)}],
                max_tokens=job.max_tokens,
                system=job.system,
                effort=job.effort,
                for_batch=True,
            )
            if betas:
                # Batch requests here use GA features only.
                raise ValueError(f"Unexpected beta features in batch request: {betas}")
            requests.append({"custom_id": job.custom_id, "params": params})
        return requests

    async def submit(self, jobs: list[BatchJob]) -> str:
        """Create a batch; returns its ID."""
        if not jobs:
            raise ValueError("No jobs to submit")
        batch = await self.llm.client.messages.batches.create(requests=self.build_requests(jobs))
        logger.info("Submitted batch %s with %d jobs", batch.id, len(jobs))
        return batch.id

    async def is_done(self, batch_id: str) -> bool:
        batch = await self.llm.client.messages.batches.retrieve(batch_id)
        return getattr(batch, "processing_status", None) == "ended"

    async def results(self, batch_id: str) -> dict[str, BatchResult]:
        """Collect results; refusals map to the neutral fade-to-black text."""
        out: dict[str, BatchResult] = {}
        stream = await self.llm.client.messages.batches.results(batch_id)
        async for entry in stream:
            res = entry.result
            if res.type == "succeeded":
                msg = res.message
                if getattr(msg, "stop_reason", None) == "refusal":
                    out[entry.custom_id] = BatchResult(entry.custom_id, "refused", REFUSAL_NARRATION)
                else:
                    out[entry.custom_id] = BatchResult(
                        entry.custom_id, "succeeded", extract_text(list(msg.content or []))
                    )
            elif res.type == "errored":
                err = getattr(res, "error", None)
                out[entry.custom_id] = BatchResult(
                    entry.custom_id, "errored", error=str(getattr(err, "type", err))
                )
            else:
                out[entry.custom_id] = BatchResult(entry.custom_id, res.type)
        return out


__all__ = [
    "BatchJob",
    "BatchResult",
    "BatchJobRunner",
    "session_recap_job",
    "module_summary_job",
    "SESSION_RECAP_SYSTEM",
    "MODULE_SUMMARY_SYSTEM",
]
