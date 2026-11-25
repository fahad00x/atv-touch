"""Apple TV Touch.

Sends touch-surface events (like the Siri Remote's clickpad or the iPhone's
Apple TV Remote) to an Apple TV, through the connection the Apple TV
integration already holds (pyatv Companion protocol). tvOS then does what it
does with a real finger: moves focus in menus, scrubs smoothly in players.

Coordinates are 0..1000 on both axes.

- WebSocket `atv_touch/touch` {entity_id, x, y, mode: press|move|release}:
  fast path for a remote app streaming a finger. Moves are coalesced so the
  Apple TV always gets the latest position without a backlog.
- WebSocket `atv_touch/available` {entity_id} -> {available: bool}.
- Services atv_touch.swipe / atv_touch.action / atv_touch.click for testing
  and automations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pyatv.const import InputAction, TouchAction
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MODES: dict[str, TouchAction] = {
    "press": TouchAction.Press,
    "move": TouchAction.Hold,
    "hold": TouchAction.Hold,
    "release": TouchAction.Release,
}
CLICKS: dict[str, InputAction] = {
    "single": InputAction.SingleTap,
    "double": InputAction.DoubleTap,
    "hold": InputAction.Hold,
}

COORD = vol.All(vol.Coerce(int), vol.Range(min=0, max=1000))


def _atv_for(hass: HomeAssistant, entity_id: str) -> Any:
    """The connected pyatv device behind an Apple TV media_player/remote entity."""
    entry_id = None
    ent = er.async_get(hass).async_get(entity_id)
    if ent is not None:
        entry_id = ent.config_entry_id
    if entry_id is None:
        raise HomeAssistantError(f"{entity_id} is not an Apple TV entity")
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != "apple_tv":
        raise HomeAssistantError(f"{entity_id} is not an Apple TV entity")
    manager = getattr(entry, "runtime_data", None)
    atv = getattr(manager, "atv", None)
    if atv is None:
        raise HomeAssistantError(f"{entity_id}: Apple TV not connected")
    return atv


TICK = 1 / 60          # the Siri Remote reports the finger ~60 times a second
CHASE = 0.7            # share of the remaining distance covered per tick
IDLE_RELEASE = 2.0     # no news from the client this long while down: lift the finger


class _TouchSender:
    """Plays one device's finger to the Apple TV on a steady 60 Hz beat.

    Touch events carry a timestamp taken when they are sent, and tvOS derives
    the finger's speed from them. Events from a phone arrive in bursts (Wi-Fi),
    so forwarding them as they come makes the finger look jerky: speed jumps,
    then stalls. Instead, after a press this sends a Hold every tick, gliding
    toward the latest position the client reported, and releases where the
    client lifted.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._atv: Any = None
        self._target = (0.0, 0.0)
        self._pos = (0.0, 0.0)
        self._down = False
        self._releasing = False
        self._last_news = 0.0
        self._task: asyncio.Task | None = None

    def push(self, atv: Any, x: int, y: int, mode: TouchAction) -> None:
        loop_time = self._hass.loop.time()
        self._atv = atv
        self._last_news = loop_time
        if mode is TouchAction.Press:
            # A finger still down (e.g. a release that hasn't gone out yet,
            # when the client lifts and presses again at once): lift it first.
            lift = self._target if self._down else None
            self._target = self._pos = (float(x), float(y))
            self._down = True
            self._releasing = False
            self._start(press=True, lift=lift)
        elif mode is TouchAction.Hold:
            if not self._down:  # move without a press: treat it as one
                self.push(atv, x, y, TouchAction.Press)
                return
            self._target = (float(x), float(y))
        elif mode is TouchAction.Release:
            self._target = (float(x), float(y))
            if self._down:
                self._releasing = True
            else:
                self._hass.async_create_background_task(
                    self._send(x, y, TouchAction.Release), "atv_touch release"
                )

    def _start(self, press: bool, lift: tuple[float, float] | None = None) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = self._hass.async_create_background_task(
            self._run(press, lift), "atv_touch finger"
        )

    async def _send(self, x: float, y: float, mode: TouchAction) -> None:
        try:
            await self._atv.touch.action(int(round(x)), int(round(y)), mode)
        except Exception as err:  # noqa: BLE001 - keep the stream going
            _LOGGER.debug("touch %s at %s,%s failed: %s", mode, x, y, err)

    async def _run(self, press: bool, lift: tuple[float, float] | None = None) -> None:
        if lift is not None:
            await self._send(*lift, TouchAction.Hold)
            await self._send(*lift, TouchAction.Release)
        if press:
            await self._send(*self._pos, TouchAction.Press)
        while self._down:
            await asyncio.sleep(TICK)
            tx, ty = self._target
            px, py = self._pos
            if self._releasing:
                # Lift where the client lifted, at once: gliding there first
                # would show tvOS a finger slowing down and kill a fast
                # swipe's momentum.
                self._pos = (tx, ty)
                await self._send(tx, ty, TouchAction.Hold)
                await self._send(tx, ty, TouchAction.Release)
                self._down = False
                self._releasing = False
                break
            dx, dy = tx - px, ty - py
            if abs(dx) < 1 and abs(dy) < 1:
                px, py = tx, ty
            else:
                px, py = px + dx * CHASE, py + dy * CHASE
            self._pos = (px, py)
            await self._send(px, py, TouchAction.Hold)
            if self._hass.loop.time() - self._last_news > IDLE_RELEASE:
                await self._send(px, py, TouchAction.Release)
                self._down = False


def _sender(hass: HomeAssistant, entity_id: str) -> _TouchSender:
    senders: dict[str, _TouchSender] = hass.data.setdefault(DOMAIN, {}).setdefault(
        "senders", {}
    )
    if entity_id not in senders:
        senders[entity_id] = _TouchSender(hass)
    return senders[entity_id]


@websocket_api.websocket_command(
    {
        vol.Required("type"): "atv_touch/touch",
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("x"): COORD,
        vol.Required("y"): COORD,
        vol.Required("mode"): vol.In(list(MODES)),
    }
)
@callback
def ws_touch(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Queue one touch event (answered at once, sent in order)."""
    try:
        atv = _atv_for(hass, msg["entity_id"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "not_available", str(err))
        return
    _sender(hass, msg["entity_id"]).push(atv, msg["x"], msg["y"], MODES[msg["mode"]])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "atv_touch/available",
        vol.Required("entity_id"): cv.entity_id,
    }
)
@callback
def ws_available(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Whether touch can be sent to this Apple TV right now."""
    try:
        _atv_for(hass, msg["entity_id"])
        available = True
    except HomeAssistantError:
        available = False
    connection.send_result(msg["id"], {"available": available})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("registered"):
        websocket_api.async_register_command(hass, ws_touch)
        websocket_api.async_register_command(hass, ws_available)
        data["registered"] = True

    async def swipe(call: ServiceCall) -> None:
        atv = _atv_for(hass, call.data["entity_id"])
        await atv.touch.swipe(
            call.data["start_x"],
            call.data["start_y"],
            call.data["end_x"],
            call.data["end_y"],
            call.data["duration_ms"],
        )

    async def action(call: ServiceCall) -> None:
        atv = _atv_for(hass, call.data["entity_id"])
        _sender(hass, call.data["entity_id"]).push(
            atv, call.data["x"], call.data["y"], MODES[call.data["mode"]]
        )

    async def click(call: ServiceCall) -> None:
        atv = _atv_for(hass, call.data["entity_id"])
        await atv.touch.click(CLICKS[call.data["action"]])

    hass.services.async_register(
        DOMAIN,
        "swipe",
        swipe,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Required("start_x"): COORD,
                vol.Required("start_y"): COORD,
                vol.Required("end_x"): COORD,
                vol.Required("end_y"): COORD,
                vol.Optional("duration_ms", default=300): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=5000)
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "action",
        action,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Required("x"): COORD,
                vol.Required("y"): COORD,
                vol.Required("mode"): vol.In(list(MODES)),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        "click",
        click,
        schema=vol.Schema(
            {
                vol.Required("entity_id"): cv.entity_id,
                vol.Optional("action", default="single"): vol.In(list(CLICKS)),
            }
        ),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    for service in ("swipe", "action", "click"):
        hass.services.async_remove(DOMAIN, service)
    return True
