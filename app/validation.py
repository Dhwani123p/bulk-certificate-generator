"""Per-recipient validation (see ``app.schemas`` for the two validation levels)."""

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.schemas import RecipientIn


@dataclass
class ValidRecipient:
    index: int
    recipient: RecipientIn


@dataclass
class RejectedRecipient:
    index: int
    raw: Any
    errors: list[str]


def _format_errors(exc: ValidationError) -> list[str]:
    messages = []
    for err in exc.errors():
        field = ".".join(str(part) for part in err["loc"]) or "recipient"
        messages.append(f"{field}: {err['msg']}")
    return messages


def validate_recipients(entries: list[Any]) -> tuple[list[ValidRecipient], list[RejectedRecipient]]:
    """Split submitted entries into valid and rejected recipients.

    Rules:
      * each entry must be an object with a non-blank ``name`` (<= 100 chars),
      * ``email`` is optional but must be a valid address when given,
      * the same email may only appear once per job (later duplicates are rejected),
        to avoid issuing the same person two certificates by accident.
    """
    valid: list[ValidRecipient] = []
    rejected: list[RejectedRecipient] = []
    seen_emails: dict[str, int] = {}

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            rejected.append(RejectedRecipient(index, entry, ["recipient must be a JSON object"]))
            continue

        try:
            recipient = RecipientIn.model_validate(entry)
        except ValidationError as exc:
            rejected.append(RejectedRecipient(index, entry, _format_errors(exc)))
            continue

        if recipient.email:
            key = recipient.email.lower()
            if key in seen_emails:
                rejected.append(
                    RejectedRecipient(
                        index,
                        entry,
                        [f"email: duplicate of recipient at index {seen_emails[key]}"],
                    )
                )
                continue
            seen_emails[key] = index

        valid.append(ValidRecipient(index, recipient))

    return valid, rejected
