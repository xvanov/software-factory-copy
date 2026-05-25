# User flow

1. From the home screen, user taps "Create goal". App navigates to the chat goal creation screen.
2. App shows an assistant greeting: "Tell me what you want to do, and I'll figure out how to track it."
3. User types their goal in natural language (e.g. "Post a YouTube walkthrough of my project by Friday at 5pm and pledge $20") and taps "Send".
4. App shows a typing indicator while the backend matches the prompt against the goal-type registry.
5. Backend returns the best match.
   - **Matched path (confidence ≥ threshold):** App shows an assistant card titled "Looks like this is a YouTube Video goal" with the matched description and two buttons: "Use this" and "Try another approach".
     - User taps "Use this". If required criteria are still missing (deadline, charity, pledge amount, etc.), assistant asks for each one in sequence ("What's your deadline?", "Which charity should receive the pledge if you miss it?", etc.). Each user reply is captured as a chat message; the draft goal is updated server-side.
     - When all required criteria are filled, assistant shows a "Final review" card listing title, description, deadline, pledge, charity, goal type. Buttons: "Create goal" and "Edit".
     - User taps "Create goal". App calls the create endpoint, then navigates to the goal detail screen.
     - User taps "Edit". Assistant asks "What would you like to change?" and the chat continues.
   - **No-match path (confidence < threshold or `none`):** App shows an assistant card titled "I don't have a built-in way to verify that yet" with two buttons: "Yes, build it" and "Let me rephrase".
     - User taps "Yes, build it". App calls the `request-new-goal-type` endpoint. Since that endpoint is STUBBED in this direction, the assistant returns: "Goal-type generation isn't enabled yet — coming in D010." (D010 replaces this with the real flow.)
     - User taps "Let me rephrase". Chat continues with the next user message.
6. Failure modes:
   - Backend returns a 5xx during matching → assistant shows "I'm having trouble understanding right now — try again?" with a "Retry" button. Tapping retry re-sends the last user message.
   - User's message is empty or whitespace → the send button is disabled.
   - User leaves the chat mid-flow and returns later → the chat session resumes from the last assistant message (session id stored locally).
