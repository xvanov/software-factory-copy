# Story
- Write canonical documentation only under `context/` for the D010 direction.
- Do not write executable code, tests, dev notes, or story files.

# Canonical Paths
- `context/project.md`
- `context/current-state.md`
- `context/navigation.md`
- `context/modules/goal-type-generator.md`
- `context/modules/factory-direction-lifecycle.md`
- `context/modules/chat-to-factory-integration.md`
- `context/modules/pushup-counter-goal-type.md`

# Acceptance Criteria
1. **Regression**: the generator can regenerate one of the existing four goal types from a prompt that describes it. With the chat matcher artificially bypassed, the chain produces a module that passes the same fixtures that the existing module passes.
2. **Novel**: the canonical pushup case (D010's reason to exist) works end-to-end. From the prompt "Do 20 pushups every morning at 7am, verify with my phone camera", the factory produces a `pushup_counter` module that uses D008's camera pipeline and passes fixture-based rep-counting assertions in CI.
