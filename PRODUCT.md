# Product

## Register

product

## Users

One user: the owner of the WhatsApp account being mirrored. No login, no teammates, no sharing. Every design decision can assume a single person who already knows every conversation in the archive by context, not by label.

Two working contexts, both real, alternating:

- **Ambient, during the workday.** The app is open on a second monitor while other work happens. Messages arrive and the user glances over. Nothing should demand attention; nothing should require a click to know the state of things.
- **Directed, on a question.** Something specific needs finding — what was agreed, which value, which address — in an archive spanning years and hundreds of conversations. The user knows roughly who and roughly when, never the exact words.

The archive is also a corpus for an AI layer: an Agno-based summarizer reads conversations, tracks in Postgres which messages it has already processed, and keeps a short standing summary per conversation. The user reads those summaries to catch up on threads they have not opened in days.

## Product Purpose

A private archive and live view of one person's WhatsApp: complete history mirrored locally, searchable in Portuguese, watched in real time, and summarized by an AI layer that keeps its own processing state.

It exists because WhatsApp itself cannot answer "what did we agree in March", cannot search well across conversations, and forgets media after its own retention rules. The mirror makes the archive queryable and permanent.

Success is a question answered in seconds that WhatsApp cannot answer at all, and a morning where reading four summaries replaces opening forty conversations.

## Brand Personality

Dense, fast, quiet.

The interface is instrumentation, not a destination. It is read at a glance, all day, by someone who is doing something else. The voice is plain and unceremonious — no encouragement, no personality in the copy, no celebrating a completed sync. State is reported, not announced.

Density is a feature, not a compromise: this user wants more conversation per screen, not more whitespace. Quiet means the interface never competes with the content it holds, and never performs work it is doing.

## Anti-references

- **WhatsApp Web.** Green chrome, rounded balloon bubbles, the tiled doodle background. Copying the original makes this read as a poor imitation of a thing the user already has, rather than the different tool it is. This one matters most: the content is identical, so only the treatment distinguishes them.
- **The generic SaaS dashboard.** Identical cards in a grid, a big number over a small label, a purple gradient somewhere. Template grammar. There are real metrics here (sync depth, processing backlog) and they must not be dressed as a hero-metric row.
- **The social network.** Large avatars, engagement affordances, performative infinite scroll. This is a personal archive, not a feed. Nothing here is being consumed for pleasure.

## Design Principles

**State is always visible, never announced.** Sync depth, backfill progress, session health, and AI processing backlog are permanent, ambient parts of the interface — legible without a click, never surfaced as a toast or a modal. A tool whose machinery is hidden makes the user poll it.

**Search is the primary verb, not a feature.** The archive's whole reason for existing is answering questions the source cannot. Search should be reachable without aiming, scoped without a settings trip, and should show enough context in results that opening the thread is often unnecessary.

**Earn density with hierarchy, not with cramming.** More per screen is the goal, but it must come from typographic and spatial hierarchy doing real work — not from shrinking everything uniformly. The test is whether the eye can find one conversation in a list of four hundred without reading.

**The AI layer is a reader, not a persona.** Summaries appear as content attributed to their source, with their staleness legible. No sparkle icons, no chat affordance, no implication of an assistant. When a summary is out of date relative to the conversation, say so plainly.

**Absence is information.** Expired media, an unsummarized conversation, a chat the backfill has not reached — each is a distinct, meaningful state and each gets a distinct, honest treatment. Never a broken image, never a spinner standing in for a fact the system already knows.

## Accessibility & Inclusion

WCAG AA as the floor:

- Body text ≥4.5:1 against its background; large text ≥3:1. Placeholder and metadata text held to the same 4.5:1 — no light-gray-for-elegance.
- Full keyboard navigation: conversation list, thread, and search reachable and operable without a pointer. Visible focus states throughout.
- `prefers-reduced-motion` respected on every transition, with a crossfade or instant alternative.
- State never carried by color alone — delivery status, expired media, and summary staleness each need a non-color signal.
- Light and dark themes both designed deliberately for their ambient conditions, not one inverted from the other.
