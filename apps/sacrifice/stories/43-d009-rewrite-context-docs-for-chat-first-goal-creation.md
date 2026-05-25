# Story
- Canonical documentation paths to write:
  - `context/project.md`
  - `context/current-state.md`
  - `context/navigation.md`
  - `context/modules/goal-creation.md`
  - `context/modules/chat.md`

# Acceptance Criteria
Sacrifice should be AI-first: the user describes what they want in natural language and the app figures out which goal type to use. The current `GoalCreateScreen` (with its typed sub-forms for YouTube, API endpoint, dev sandbox, GitHub repo) forces the user to know which goal type they want before they can fill in anything. With D007's plugin registry shipped, the chat backend can match a free-text prompt against existing goal types via a single LLM call against the catalog (name + description + sample prompts). This direction lands the chat surface and the matching path. D010 adds the "no match → factory generates a new goal type" path on top of the stubbed endpoint defined here.

The chat REPLACES the existing creation surface. The four typed PROOF submission screens (`ProofSubmissionScreen.tsx`, `ApiEndpointSubmissionScreen.tsx`, `DevSandboxSubmissionScreen.tsx`) are unaffected — proof submission stays as it is in this batch; only goal CREATION moves to chat.
