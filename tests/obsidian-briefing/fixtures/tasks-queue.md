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

- **id:** family-calendar-2026-04-08-001
- **description:** Confirm nanny availability for Thursday
- **assignee:** me
- **status:** open
- **due_date:** 2026-04-09
- **source_agent:** family-calendar
- **created_at:** 2026-04-08T12:00:00Z
- **completed_at:** —

---

- **id:** shopping-2026-04-07-001
- **description:** Restock diapers — Subscribe & Save delivery delayed
- **assignee:** shopping
- **status:** open
- **due_date:** 2026-04-10
- **source_agent:** shopping
- **created_at:** 2026-04-07T14:00:00Z
- **completed_at:** —

---

- **id:** connector-2026-04-06-001
- **description:** Send thank-you note to Alice for the baby clothes
- **assignee:** me
- **status:** done
- **due_date:** —
- **source_agent:** connector
- **created_at:** 2026-04-06T10:00:00Z
- **completed_at:** 2026-04-07T09:00:00Z
