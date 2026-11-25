# Apple TV Touch

Home Assistant custom integration that sends touch-surface gestures to Apple TVs,
reusing the connections of the built-in **Apple TV** integration (pyatv, Companion
protocol). tvOS treats them like a finger on the Siri Remote: focus moves in menus
and players scrub smoothly, in any app.

## Install
HACS → custom repository `fahad00x/atv-touch` (Integration) → download → restart →
Settings → Devices & services → Add integration → **Apple TV Touch**.

## Use
Coordinates are 0–1000 on both axes.

- Services: `atv_touch.swipe`, `atv_touch.action` (press / move / release),
  `atv_touch.click` (single / double / hold).
- WebSocket: `atv_touch/touch` `{entity_id, x, y, mode}` for streaming a finger
  (moves are coalesced); `atv_touch/available` `{entity_id}` → `{available}`.

`entity_id` is any `media_player.*` or `remote.*` entity of the Apple TV integration.
