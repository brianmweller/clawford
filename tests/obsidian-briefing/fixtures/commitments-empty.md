# Commitments — Active

> **Schema:** Each entry tracks a promise or obligation.
>
> | Field | Required | Description |
> |-------|----------|-------------|
> | id | Yes | `{agent}-{YYYY-MM-DD}-{seq}` |
> | who | Yes | Who made the commitment |
> | to_whom | Yes | Who it was made to |
> | what | Yes | What was committed |
> | by_when | No | Deadline, if one exists |
> | status | Yes | `open`, `completed`, `overdue`, `cancelled` |
> | source_detail | Yes | Where this was captured |
> | source_agent | Yes | Which agent recorded it |
> | created_at | Yes | ISO 8601 timestamp |
> | resolved_at | No | When status changed from open |
> | resolution_note | No | How it was resolved |
>
> **Status transitions:** open→completed, open→overdue (auto), open→cancelled, overdue→completed, overdue→cancelled
>
> **Convention:** Only the source agent or user may resolve. In-place edits allowed (sole exception to append-only).

---
