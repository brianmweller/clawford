# Tasks — Queue

> **Schema:** Each entry is an action item.
>
> | Field | Required | Description |
> |-------|----------|-------------|
> | id | Yes | `{agent}-{YYYY-MM-DD}-{seq}` |
> | description | Yes | What needs to be done |
> | assignee | Yes | `me`, `wife`, or an agent name |
> | status | Yes | `open`, `done`, `cancelled` |
> | due_date | No | When it's due |
> | source_agent | Yes | Which agent created it |
> | created_at | Yes | ISO 8601 timestamp |
> | completed_at | No | When marked done |
>
> **Convention:** Append-only. Tasks assigned to "me" are synced to Google Calendar Tasks. Completed tasks archived monthly by Fix-It.

---
