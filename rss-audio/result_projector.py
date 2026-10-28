"""Recover final confirmation from durable RSS results; never rerun publication."""
import logging

from tg_tools_common.runtime_contract import TERMINAL_RESULT_KINDS

from pipeline import render_completion_message
from runtime_jobs import confirmation_delivery_payload

log = logging.getLogger(__name__)


class RSSResultProjector:
    def __init__(self, client):
        self.client = client

    def project_once(self) -> int:
        projected = 0
        for result in self.client.list_results(result_owner="rss-audio", limit=100) or []:
            if result.get("resultKind") not in TERMINAL_RESULT_KINDS:
                continue
            try:
                self._project(result)
                projected += 1
            except Exception:
                log.exception("RSS result left unconsumed for recovery result=%s", result.get("resultId"))
        return projected

    def _project(self, result: dict) -> None:
        job = self.client.get_job(job_id=result["jobId"])["job"]
        if job["resultOwner"] != "rss-audio" or job["jobId"] != result["jobId"]:
            raise ValueError("result/job owner mismatch")
        output = result.get("resultJson") or {}
        text = output.get("completionHtml") or self._fallback_text(result["resultKind"], output)
        self.client.append_delivery(confirmation_delivery_payload(
            job_id=job["jobId"], result_id=result["resultId"], input_json=job.get("inputJson") or {},
            html_text=text))
        self.client.consume_result(result_id=result["resultId"], result_owner="rss-audio")

    @staticmethod
    def _fallback_text(kind: str, output: dict) -> str:
        if kind != "succeeded":
            return "Request failed or was cancelled."
        return render_completion_message(output["title"], output["publicUrl"], notes_url=output.get("notesUrl"),
                                         script_url=output.get("scriptUrl"),
                                         source_url=output.get("sourceUrlDisplay"))
