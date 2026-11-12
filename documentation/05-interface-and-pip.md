# 05 · Interface and Pip

## Principles applied

| Principle | How it shows up |
| --- | --- |
| One obvious next step | Home has a single primary button ("Move a store") and a checklist; every wizard step ends in one primary action |
| Progress is always visible | A five-step indicator with done / current / upcoming states; live counts during import and publish |
| Recognition over recall | Navigation uses plain task names ("Move a store", "Products", "Stores") with icons, not system terms |
| Nothing irreversible without a preview | Publishing always shows what will change first; destructive actions ask for confirmation in words that state the consequence |
| Errors say what to do | Every error is a sentence plus an action; technical codes are never shown |
| Forgiving | Wizard position is remembered; imports and publishes can be repeated safely; a typed form is never thrown away by a connection blip |
| Accessible | Keyboard reachable, visible focus, labels on every input, colour never the only signal, 44 px targets on primary controls, reduced-motion respected, light and dark themes |
| Honest states | Empty screens explain what belongs there and offer the action; "waiting to retry" is shown as paused, not failed |

The design-guidance tool consulted for this suggested a nature/wellness style with web
fonts; that did not fit a data tool, and web fonts would break the no-third-party policy.
What was taken from it: step indicators for multi-step flows, skip and back controls,
SVG icons instead of emoji, hover feedback of 150–300 ms, and respect for reduced motion.

## The wizard ("Move a store")

`frontend/js/views-migrate.js`

1. **Your current store.** Pick a connected store or connect one (practice store, Shopify,
   WooCommerce). Each option explains where to find the key. A wrong key keeps the form
   open and reuses the half-created store on the next try.
2. **Bring products in.** One button. Live counts; a report of what could not be read and
   what is missing (images, brand, category, price); "Continue from where it stopped" if
   the store went away.
3. **Improve them.** A short questionnaire; then a summary and three choices: look through
   the suggestions, approve all low-impact ones, or move on. The step can be skipped.
4. **Your new store.** Same picker; the source cannot be chosen as the destination.
5. **Preview & publish.** Defaults to a trial of 10 products. Warns if nothing is marked
   Active. Leads to the preview screen, where approval happens.

Position and the ids of the running import and publish are stored in the browser per
organization, so the wizard reopens where it was left.

## Pip

`frontend/js/pip.js`, styles in `frontend/wizard.css`.

Pip is a small character in the bottom-right corner, drawn as vector shapes by the page
itself (no image files, no emoji).

| Mood | When |
| --- | --- |
| Happy | Default |
| Thinking | A question it cannot answer; long-running work |
| Cheering | Something succeeded (import finished, store connected, changes applied) |
| Sad (with a tear) | An error, or the connection dropped |
| Stern (turns red, shakes) | An unsafe action: pasting something that looks like a password, a blocked action, a frozen organization |
| Sleepy | Two minutes without activity |

What Pip does:

- On the first visit to each screen, says in one sentence what the screen is for.
- When opened, shows that explanation plus a "heads up" about what **not** to do there.
- Answers seven common questions (what next, safety, lost connection, duplicates, undo,
  what the AI changes, what to avoid) and matches typed questions by keyword.
- "What should I do next?" is computed from the account's real state and offers a button
  that goes there.
- Reacts to every success and error message in the app.

**Pip is scripted.** Its answers are written by hand and run in the browser. It is not a
language model and sends nothing anywhere. It says so when it does not know an answer.

## Wording changes for sellers

| Before | After |
| --- | --- |
| Channels | Stores |
| Sandbox store (mock) | Practice store |
| "(preview, not yet verified against live stores)" | "Beta" badge |
| Overview | Home |
| Field paths and validator codes in errors | "The email address is not valid." |
| Handler and task ids in key usage | "Publishing / Reading products — Allowed / Refused" |
