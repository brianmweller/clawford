# MEMORY.md — Persistent Knowledge

## Family Routine (Weekdays)

**Default weekday:**
- 08:05 — Jamie arrives (Jordan care). Sam takes Avery to Example Preschool school.
- 08:35 — Sam returns home.
- 15:30 — Jamie leaves to pick up Avery from school (arrives 15:45).
- 18:00 — Sam takes over both kids. Jamie leaves.
- 20:00 — Bedtime for Avery and Jordan.

**Monday exception:** Sam picks up Avery from school. Takes her to Example Swim School swimming 16:30–17:00.

**Friday exception:** Jamie leaves at 13:00. Alex takes Jordan starting 13:00. Sam picks up Avery from school.

**Saturday:** Sam takes Avery to gymnastics (leave home 09:40).

**Sunday:** Sam takes Avery to ballet at Example Ballet Studio (leave home 09:00).

## Calendar IDs

- Sam: sam.smith@example.com
- Alex: alex.rivera@example.com
- Kids' events live on Sam's and/or Alex's calendars.

## Activity Providers

- **School:** Example Preschool
- **Swimming (Mon):** Example Swim School
- **Gymnastics (Sat):** TBD
- **Ballet (Sun):** Example Ballet Studio

## Conflict Detection Rules

High-value conflicts to watch for:
1. Two kids needing transport at the same time (Avery school pickup + Jordan appointment)
2. Sam has a meeting during a time he's responsible for pickup/dropoff
3. Jamie is unavailable on a day she's normally expected (sick, vacation)
4. Friday afternoon: Alex has a conflict during her Jordan care window (13:00–18:00)

## Hard Constraints

- Git: commit to `agents/family-calendar/` only, never push. Mr Fixit handles Git.
- OAuth scope: full calendar (read/write) + gmail.readonly. Write operations require --confirm flag.
- LLM calls: use gpt-5.4-nano via OpenAI, never Claude CLI.
