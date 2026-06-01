# Semantic Operating Principle

The Slack bot must not treat live messages as isolated one-liners. For every incoming message, the operating agent should compare it against active proposals, recent pending questions, and approved items before deciding whether to create a new proposal or patch an existing one.

A slot-only message such as "회의 장소는 3층 회의실" should:

1. Try to match an existing task/event by participant, title, date, and recent context.
2. Apply the update directly when confidence is high and the actor is authorized.
3. Ask a candidate-selection question when confidence is medium or multiple targets match.
4. Only return a missing-request style error when no plausible target exists.

The deterministic core validates confidence, actor authorization, meaningful slot updates, persistence, audit events, and Slack output dedupe.