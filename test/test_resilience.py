"""One failing block must not take the rest of the poll with it.

A pooled read was all-or-nothing: the first block the controller refused or
answered too slowly aborted the poll and threw away everything already read, so
one sluggish block left every value of the machine unavailable. Each component
is read on its own now and ``async_update`` reports what refreshed instead of
raising - only a dead link still raises.
"""

from __future__ import annotations

import pytest
from modbus_connection import IllegalDataAddressError, ModbusConnectionError, ModbusTimeoutError, ReadBlock, ServerDeviceBusyError
from modbus_connection.mock import MockModbusUnit
from modbus_connection.model import Component

from pystiebeleltron.lwz import LwzStiebelEltronAPI
from pystiebeleltron.wpm import WpmStiebelEltronAPI
from pystiebeleltron.wpm3i import Wpm3iStiebelEltronAPI

# outside_temperature (input 506, 0.1-scaled) and vd_heating_day (input 3500)
# sit in two different WPM blocks, so failing one leaves the other readable.


@pytest.mark.parametrize("api_class", [WpmStiebelEltronAPI, Wpm3iStiebelEltronAPI, LwzStiebelEltronAPI])
@pytest.mark.asyncio()
async def test_a_healthy_controller_reports_every_component(
    mock_modbus_unit: MockModbusUnit,
    api_class: type[WpmStiebelEltronAPI | Wpm3iStiebelEltronAPI | LwzStiebelEltronAPI],
) -> None:
    """Every component the API exposes is polled and reported under its own attribute name."""
    api = api_class(mock_modbus_unit)

    report = await api.async_update()

    assert report.complete
    assert report.failed == {}
    assert report.updated == {name for name, value in vars(api).items() if isinstance(value, Component)}


@pytest.mark.parametrize("api_class", [WpmStiebelEltronAPI, Wpm3iStiebelEltronAPI, LwzStiebelEltronAPI])
@pytest.mark.asyncio()
async def test_readings_and_settings_poll_their_own_blocks(
    mock_modbus_unit: MockModbusUnit,
    api_class: type[WpmStiebelEltronAPI | Wpm3iStiebelEltronAPI | LwzStiebelEltronAPI],
) -> None:
    """Neither method reads a register the other one owns.

    The controller draws the line: what it reports is in the input space, what
    it has been set to is in the holding space. On a WPM that is four of the
    eleven blocks and 189 of the 621 registers a full poll reads.
    """
    api = api_class(mock_modbus_unit)

    mock_modbus_unit.read_events.clear()
    readings = await api.async_update_readings()
    assert {event.register_type for event in mock_modbus_unit.read_events} == {"input"}

    mock_modbus_unit.read_events.clear()
    settings = await api.async_update_settings()
    assert {event.register_type for event in mock_modbus_unit.read_events} == {"holding"}

    assert not readings.updated & settings.updated
    assert readings.updated | settings.updated == {name for name, value in vars(api).items() if isinstance(value, Component)}
    assert "system_parameters" in settings.updated
    assert "system_values" in readings.updated


@pytest.mark.asyncio()
async def test_a_settings_poll_of_a_silent_controller_raises(mock_modbus_unit: MockModbusUnit) -> None:
    """It starts its own cycle, so nothing has answered and the rest would only time out."""
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.fail_read(1500, ModbusTimeoutError("controller asleep"), register_type="holding")

    with pytest.raises(ModbusTimeoutError):
        await api.async_update_settings()


@pytest.mark.asyncio()
async def test_a_slow_settings_block_is_contained_in_a_full_poll(mock_modbus_unit: MockModbusUnit) -> None:
    """The readings answered first, so the controller is plainly there."""
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.fail_read(1500, ModbusTimeoutError("slow parameters"), register_type="holding")

    report = await api.async_update()

    assert set(report.failed) == {"system_parameters"}
    assert "system_values" in report.updated


@pytest.mark.asyncio()
async def test_a_failed_block_leaves_the_rest_fresh(mock_modbus_unit: MockModbusUnit) -> None:
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.input[506] = 100
    await api.async_update()

    mock_modbus_unit.input[506] = 200  # the machine moves on in both blocks
    mock_modbus_unit.input[3500] = 7
    mock_modbus_unit.fail_read(506, ServerDeviceBusyError(), register_type="input")
    report = await api.async_update()

    assert not report.complete
    assert set(report.failed) == {"system_values"}
    assert isinstance(report.failed["system_values"], ServerDeviceBusyError)
    assert "energy_data" in report.updated
    assert api.system_values.outside_temperature == 10.0  # the previous read's value
    assert api.energy_data.vd_heating_day == 7


@pytest.mark.asyncio()
async def test_listeners_fire_at_the_end_and_only_for_fresh_components(mock_modbus_unit: MockModbusUnit) -> None:
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    await api.async_update()
    seen: list[int] = []
    api.energy_data.add_update_listener(lambda: seen.append(len(mock_modbus_unit.read_events)))
    api.system_values.add_update_listener(lambda: seen.append(-1))

    mock_modbus_unit.fail_read(506, ServerDeviceBusyError(), register_type="input")
    mock_modbus_unit.read_events.clear()
    await api.async_update()

    # One notification, counted after every component of the readings poll had
    # been tried - energy data is read early, so notifying inline would record a
    # lower number. None for the block that failed. The settings poll that
    # follows is its own and does not hold the readings up.
    settings_start = next(i for i, event in enumerate(mock_modbus_unit.read_events) if event.register_type == "holding")
    assert seen == [settings_start]


@pytest.mark.asyncio()
async def test_a_silent_controller_raises_on_the_first_component(mock_modbus_unit: MockModbusUnit) -> None:
    """Nothing answered, so the remaining blocks would only pay a timeout each."""
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.fail_read(506, ModbusTimeoutError("controller asleep"), register_type="input")

    with pytest.raises(ModbusTimeoutError):
        await api.async_update()


@pytest.mark.asyncio()
async def test_a_timeout_after_a_block_answered_is_still_contained(mock_modbus_unit: MockModbusUnit) -> None:
    """One slow block loses its own values only; the controller is plainly there."""
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.fail_read(3500, ModbusTimeoutError("slow energy data"), register_type="input")

    report = await api.async_update()

    assert set(report.failed) == {"energy_data"}
    assert isinstance(report.failed["energy_data"], ModbusTimeoutError)
    assert "system_values" in report.updated


@pytest.mark.asyncio()
async def test_a_dead_link_raises_instead_of_reporting(mock_modbus_unit: MockModbusUnit) -> None:
    """A link that is down is not one block's problem, so there is nothing to report."""
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    await api.async_update()

    mock_modbus_unit.fail_requests(ModbusConnectionError("link down"))

    with pytest.raises(ModbusConnectionError):
        await api.async_update()


@pytest.mark.asyncio()
async def test_a_refused_required_block_is_reported_and_read_again(mock_modbus_unit: MockModbusUnit) -> None:
    """A required block refused costs its own values and is not given up on.

    Dropping applies to the optional blocks only: a required block the
    controller refuses is a fault to report, not a module that isn't built in.
    """
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.fail_read(502, IllegalDataAddressError(), register_type="input")

    report = await api.async_update()

    assert set(report.failed) == {"system_values"}
    assert api.system_values.outside_temperature is None
    assert "energy_data" in report.updated

    mock_modbus_unit.fail_read(502, None, register_type="input")
    mock_modbus_unit.input[506] = 100
    report = await api.async_update()

    assert report.complete
    assert api.system_values.outside_temperature == 10.0


@pytest.mark.asyncio()
async def test_a_busy_controller_does_not_lose_an_optional_block(mock_modbus_unit: MockModbusUnit) -> None:
    """Only illegal data address means "not built in"; other codes are failures.

    A controller that answers a block with device busy or device failure still
    has those registers, so dropping the component would lose its values for
    good over a passing complaint. Such an answer is reported instead, and the
    block is read again once the controller answers properly.
    """
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.fail_read(5219, ServerDeviceBusyError(), register_type="input")

    report = await api.async_update()

    # The busy answer reaches the caller as itself, naming the block it aborted,
    # rather than as something the tolerance rewrapped on the way out.
    failure = report.failed["extended_energy_system_information"]
    assert isinstance(failure, ServerDeviceBusyError)
    assert failure.block == ReadBlock("input", 5219, 12)

    mock_modbus_unit.fail_read(5219, None, register_type="input")
    report = await api.async_update()

    assert report.complete
    assert api.extended_energy_system_information.sg_ready_inputs_active == 0


@pytest.mark.asyncio()
async def test_only_the_components_that_refreshed_notify(mock_modbus_unit: MockModbusUnit) -> None:
    """A failed block must not tell its listeners the values are fresh."""
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    notified: dict[str, int] = {"system_values": 0, "extended": 0}
    api.system_values.add_update_listener(lambda: notified.__setitem__("system_values", notified["system_values"] + 1))
    api.extended_energy_system_information.add_update_listener(lambda: notified.__setitem__("extended", notified["extended"] + 1))
    mock_modbus_unit.fail_read(5219, ServerDeviceBusyError(), register_type="input")

    await api.async_update()

    assert notified == {"system_values": 1, "extended": 0}

    mock_modbus_unit.fail_read(5219, None, register_type="input")
    await api.async_update()

    assert notified == {"system_values": 2, "extended": 1}


@pytest.mark.asyncio()
async def test_a_controller_refusing_everything_still_reports_it(mock_modbus_unit: MockModbusUnit) -> None:
    """Tolerating a refused block must not make a mute controller look healthy."""
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.fail_requests(IllegalDataAddressError())

    report = await api.async_update()

    assert not report.complete
    assert report.updated == set()
    # The optional blocks are taken as not built in and drop out of the poll;
    # every required one is named as failed.
    assert set(report.failed) == {
        "system_values",
        "system_parameters",
        "system_state",
        "energy_data",
        "energy_management_settings",
        "energy_system_information",
    }


@pytest.mark.asyncio()
async def test_the_raw_dump_covers_every_component(mock_modbus_unit: MockModbusUnit) -> None:
    """Diagnostics wants the whole map, not only what the last poll refreshed.

    Nothing is read only at setup on these controllers - there is no identity
    block and no probe - so the polled components are the whole map.
    """
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.input[506] = 100

    raw = await api.async_read_raw()

    assert raw["input"][506] == 100  # a required input block
    assert 1500 in raw["holding"]  # a required holding block
    assert 5219 in raw["input"]  # and an optional one


@pytest.mark.asyncio()
async def test_the_raw_dump_does_not_notify(mock_modbus_unit: MockModbusUnit) -> None:
    """A download must not look like a poll: it refreshes without notifying."""
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.input[506] = 100
    await api.async_update()
    seen: list[int] = []
    api.system_values.add_update_listener(lambda: seen.append(1))

    mock_modbus_unit.input[506] = 200
    await api.async_read_raw()

    assert seen == []
    assert api.system_values.outside_temperature == pytest.approx(20.0)  # still refreshed


@pytest.mark.asyncio()
async def test_the_raw_dump_leaves_out_a_block_the_controller_does_not_serve(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """An optional block refused is absent, so it must not fail the download."""
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.fail_read(5219, IllegalDataAddressError(), register_type="input")

    raw = await api.async_read_raw()

    assert 5219 not in raw["input"]
    assert 506 in raw["input"]  # the rest of the machine still came back


@pytest.mark.asyncio()
async def test_the_raw_dump_raises_on_a_refused_required_block(mock_modbus_unit: MockModbusUnit) -> None:
    """A required block refused is a fault; a dump hiding it would mislead."""
    api = WpmStiebelEltronAPI(mock_modbus_unit)
    mock_modbus_unit.fail_read(502, IllegalDataAddressError(), register_type="input")

    with pytest.raises(IllegalDataAddressError):
        await api.async_read_raw()
