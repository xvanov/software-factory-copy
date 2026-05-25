# User flow

1. From within the chat goal creation screen (D009), user describes a goal whose verification doesn't match any existing type. Canonical example: "I want to do 20 pushups every morning at 7am, verify with my phone camera."
2. App shows the no-match assistant card: "I don't have a built-in way to verify that yet. Want me to build a new goal type for it? Takes a few minutes." with buttons "Yes, build it" and "Let me rephrase".
3. User taps "Yes, build it".
4. App shows: "Got it. I'm building a 'pushup-counter' goal type. I'll notify you when it's ready — feel free to close the app." A status banner appears with "Building goal type — queued".
5. Behind the scenes, the goal is created in `awaiting_goal_type` status with a reference to the direction id. A new direction directory is written under `apps/sacrifice/directions/`.
6. The status banner updates as the factory chain progresses: "queued" → "in progress" → "pull request open" → "merging".
7. Optional: user closes the app and goes about their day.
8. When the factory PR merges, the app's notification system fires a `goal_type_ready` notification. The notification bell increments.
9. User taps the notification. App opens the chat session.
10. Assistant shows: "Your 'pushup-counter' goal type is ready. It records a video of your pushups and counts reps. Want to accept and activate your goal?" with buttons "Accept and activate" and "Give feedback to iterate".
11. **Accept path:** User taps "Accept and activate". App calls the accept endpoint; goal transitions from `awaiting_goal_type` to `active`. App navigates to the goal detail screen.
12. **Iterate path:** User taps "Give feedback to iterate". App shows a text input prompt: "What should be different?". User types feedback (e.g. "Use a side-on camera angle instead of front-on; count partial reps as 0.5"). User taps "Send".
13. App shows: "Got it — I'm filing a follow-up direction that will update the module." The status banner returns to "Building goal type — queued" showing the new direction id (e.g. `047-pushup-counter-side-angle` — the id is whatever the global counter allocates; concurrent directions may interleave). The factory chain runs on the new direction; on its merge, steps 8–11 repeat against the updated module. The user's goal stays in `awaiting_goal_type` across iterations until they tap "Accept and activate".
14. Failure modes:
    - The factory chain bounces with a `(tests-need-clarification)` direction (per the chain's escalation rule). Sacrifice surfaces this in chat as: "I'm stuck — can you describe what 'counts as a pushup' more precisely?" with a free-text input that appends to the direction's `feedback.md`. The user's reply re-enters the iteration loop.
    - The user's daily AI spend cap is exhausted. Chat returns: "You've hit today's AI budget. Try again tomorrow, or reach out if this is wrong." No direction is filed.
    - The user already has an in-flight generation. Chat returns: "You're already building 'pushup-counter'. Want to add to that one instead?" — tapping yes routes feedback into the existing direction's `feedback.md`.
    - Direction synthesis fails (LLM cannot produce a coherent direction). Chat returns: "I couldn't pin down what you want — try rephrasing with more concrete success criteria."
