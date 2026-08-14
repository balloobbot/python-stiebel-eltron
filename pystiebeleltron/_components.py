"""Reading the components of one controller, one block at a time."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusError,
    ModbusTimeoutError,
)
from modbus_connection.model import Component

from . import UpdateReport

_LOGGER = logging.getLogger(__package__)


class ControllerComponents:
    """The components of one controller, each read on its own.

    Stiebel documents register blocks that not every controller and firmware
    actually serves - the energy-management extension, the inverter and
    efficiency figures - and no register says which of them a given machine has.
    A controller answers a read of a block it does not implement with a Modbus
    exception (illegal data address).

    The reads are not pooled. One pooled read is all-or-nothing: the first block
    the controller refused or answered too slowly aborted the poll, threw away
    what had already been read and left every value of the machine unavailable
    over one sluggish block. Each component is read on its own instead, so a
    failure costs that component's values and nothing else - it keeps the values
    of its last successful read and is named in the returned
    :class:`~pystiebeleltron.UpdateReport` with the error that failed it.

    An ``optional`` component is one real machines are known to refuse. The first
    time the controller answers it with illegal data address it is dropped for
    the life of this object, so no later poll wastes a round trip on it, and it
    counts as absent rather than failed. Every other answer means the registers
    are there and the read went wrong, so it is reported as a failure and read
    again next poll.

    A machine that does not have the block refuses it on the very first read,
    before any value was stored, so its fields read ``None`` from then on, the
    same as a value the controller reports as unavailable. A block that answered
    once and is refused later - a module switched off, new firmware, a different
    device on that address - keeps the values of its last successful read
    instead, until the object is rebuilt. Clearing them would take a public way
    to invalidate a ``Component``'s cache, which ``modbus_connection`` does not
    expose.
    """

    def __init__(
        self,
        required: Mapping[str, Component],
        optional: Mapping[str, Component] | None = None,
    ) -> None:
        """Poll every component by name; drop an ``optional`` one the controller refuses."""
        self._components = {**required, **(optional or {})}
        self._optional = frozenset(optional or ())

    def _drop(self, name: str, err: IllegalDataAddressError) -> None:
        del self._components[name]
        _LOGGER.info(
            "The controller does not serve the registers of %s, so they stay unavailable and are not read again: %s",
            name,
            err,
        )

    async def async_update(self) -> UpdateReport:
        """Read every component still in play and report what refreshed.

        Listeners fire only once every component has been tried, and only for
        the ones that refreshed: notifying as we go would let a listener act on
        half a poll. A failure of the link itself is not one block's problem, so
        it raises rather than reporting every remaining block as failed. Neither
        is a controller that has answered nothing at all: the first block timing
        out raises instead of paying one timeout per remaining block.
        """
        updated: set[str] = set()
        failed: dict[str, ModbusError] = {}
        for name, component in list(self._components.items()):
            try:
                await component.async_update(notify=False)
            except ModbusConnectionError:
                raise
            except IllegalDataAddressError as err:
                if name in self._optional:
                    self._drop(name, err)
                else:
                    failed[name] = err
            except ModbusTimeoutError as err:
                # Required components lead, so anything answered is recorded here.
                if not updated and not failed:
                    raise  # nothing answered at all: assume the rest time out too
                failed[name] = err
            except ModbusError as err:
                failed[name] = err
            else:
                updated.add(name)

        for name in updated:
            self._components[name].notify()
        return UpdateReport(updated, failed)

    async def async_read_raw(self) -> dict[str, dict[int, int | bool]]:
        """Read every component still in play undecoded, keyed by space and address.

        Nothing is read only at setup here - the controller has no identity
        block and no probe, so the components a poll walks are the whole map.

        An ``optional`` component the controller does not serve refuses this
        read too, as long as no poll has dropped it yet. That is absence rather
        than a failure, so it is left out of the dump instead of failing the
        whole download; a required component still raises. Being a read like
        any other, it does not drop the component - a poll does that.

        A download is not a poll, so the fields refresh without notifying.
        """
        raw: dict[str, dict[int, int | bool]] = {}
        for name, component in self._components.items():
            try:
                values = await component.async_read_raw(notify=False)
            except IllegalDataAddressError:
                if name not in self._optional:
                    raise
                continue
            for space, addresses in values.items():
                raw.setdefault(space, {}).update(addresses)
        return raw
