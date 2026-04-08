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

- **id:** meetings-coach-2026-04-05-001
- **who:** Bob Martinez
- **to_whom:** me
- **what:** Send the revised proposal
- **by_when:** 2026-04-07
- **status:** open
- **source_detail:** Agreed during 1:1 on 4/5, per Krisp transcript
- **source_agent:** meetings-coach
- **created_at:** 2026-04-05T15:00:00Z
- **resolved_at:** —
- **resolution_note:** —

---

- **id:** connector-2026-04-03-001
- **who:** me
- **to_whom:** Sharon Chen
- **what:** Send Deer Hollow parking info
- **by_when:** 2026-04-08
- **status:** open
- **source_detail:** WhatsApp conversation with Sharon on 4/3
- **source_agent:** connector
- **created_at:** 2026-04-03T20:00:00Z
- **resolved_at:** —
- **resolution_note:** —

---

- **id:** family-calendar-2026-04-01-001
- **who:** me
- **to_whom:** Alex
- **what:** Book restaurant for anniversary
- **by_when:** 2026-04-15
- **status:** completed
- **source_detail:** Morning briefing reminder
- **source_agent:** family-calendar
- **created_at:** 2026-04-01T12:00:00Z
- **resolved_at:** 2026-04-06T18:00:00Z
- **resolution_note:** Booked Manresa for April 15
