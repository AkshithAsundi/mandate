"""Approval notifications to n8n (which forwards to Telegram)."""
import logging

import httpx

from . import config

log = logging.getLogger("mandate.approvals")


def notify(approval: dict) -> dict:
    """POST pending approval details to N8N_WEBHOOK_URL, or just log if it is not set.

    The payload contains no secrets. n8n calls back POST {callback_url} with
    header X-Approval-Secret (stored in n8n, not sent here) and {"decision": "approve"|"deny"}.
    """
    payload = {
        **approval,
        "callback_url": f"{config.base_url()}/approvals/{approval['approval_id']}",
    }
    url = config.n8n_webhook_url()
    if not url:
        log.warning("N8N_WEBHOOK_URL not set - would have sent approval %s: %s",
                    approval["approval_id"], payload)
        return {"sent": False, "detail": "N8N_WEBHOOK_URL not set; logged instead"}
    try:
        r = httpx.post(url, json=payload, timeout=5.0)
        r.raise_for_status()
        log.info("approval %s sent to n8n (HTTP %s)", approval["approval_id"], r.status_code)
        return {"sent": True, "detail": f"HTTP {r.status_code}"}
    except httpx.HTTPError as e:
        # Don't log the URL itself: n8n webhook URLs act as bearer secrets.
        log.error("failed to send approval %s to n8n: %s", approval["approval_id"], type(e).__name__)
        return {"sent": False, "detail": f"webhook error: {type(e).__name__}"}
