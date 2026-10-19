# Station Construction Overhaul

Replaces the look and workflow of the player-station **Construction** tab.

## What changes

* **Progress you can read** -- a summary strip shows how many jobs are
  building / paused and the combined progress. Every job is a card with a
  large bar, an exact percentage (one decimal) and a live **ETA** that is
  measured from how fast that job has actually been advancing.
* **The queue stays live** -- the mod re-requests the queue every 15 s
  while the tab is open, so bars move without reopening the tab.
* **One-click building** -- every blueprint row has a **BUILD** button, and
  an **xN...** button (quantity prompt) for blueprints with more than one use.
  No right-click needed (right-click still works).
* **Inline Pause / Resume and Cancel** on each job card. Cancel still goes
  through the game's own confirmation dialog.
* **Find things fast** -- tech-tier filter chips next to the search box, and
  each blueprint row shows its credit / resource / item cost at a glance.
* Left-click a blueprint row for the full cost and description popup.
* **Hover a job card** to see its requirements (looked up from the job or its
  blueprint): what was paid up front vs. what is still drawn while building.

Everything else (selection, scrolling, search typing, the right-click menu,
the cancel confirmation) is still handled by the game's own code; the mod
only redraws the tab and adds the new buttons.

## Safe by design

If anything in the redraw fails, the mod logs the error once, stops
touching the tab, and the vanilla Construction tab is shown instead.
