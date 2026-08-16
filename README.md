<p align=center>
    <img src="https://www.stiebel-eltron.de/content/dam/ste/logo-stiebel-eltron.svg"/>
</p>
<p align=center>
    <a href="https://pypi.org/project/pystiebeleltron/"><img src="https://img.shields.io/pypi/v/pystiebeleltron.svg"/></a>
    <a href="https://github.com/ThyMYthOS/python-stiebel-eltron/actions/workflows/test-python-package.yml"><img src="https://github.com/ThyMYthOS/python-stiebel-eltron/actions/workflows/test-python-package.yml/badge.svg"/></a>
    <!--a href='https://coveralls.io/github/fucm/python-stiebel-eltron?branch=master'><img src='https://coveralls.io/repos/github/fucm/python-stiebel-eltron/badge.svg?branch=master' alt='Coverage Status' /></a>
  <img src="https://img.shields.io/github/license/ThyMYthOS/python-stiebel-eltron.svg"/></a Maybe use https://github.com/marketplace/actions/coverage-badge-->
</p>

# python-stiebel-eltron
Python API for interacting with the STIEBEL ELTRON ISG web gateway via modbus for controlling integral ventilation units and heat pumps.

This module is based on the STIEBEL ELTRON [modbus user manual](https://www.stiebel-eltron.ch/content/dam/ste/ch/de/downloads/kundenservice/smart-home/Modbus/Modbus%20Bedienungsanleitung.pdf), but is not official, developed, supported or endorsed by Stiebel Eltron GmbH & Co. KG. For questions and other inquiries, use the issue tracker in this repo please.

## Requirements
You need to have [Python](https://www.python.org) installed.

* STIEBEL ELTRON Internet-Service Gateway [ISG WEB](https://www.stiebel-eltron.com/en/home/products-solutions/renewables/controller_energymanagement/internet_servicegateway/isg_web.html) with enabled [modbus module](https://www.stiebel-eltron.ch/de/home/service/smart-home/modbus.html)
  * You can call the STIEBEL ELTRON support, if your ISG does not have the modbus module enabled. They upgraded mine for free.
* STIEBEL ELTRON heatpumpt (compatible). Successfully used devices:
  * LWZ504e
  * LWZ304
* Network connection to the ISG WEB

## Installation
The package is available in the [Python Package Index](https://pypi.python.org/).

```bash
    $ pip install pystiebeleltron
```

## Example usage of the module
The sample below shows how to use this Python module (api for wpm heat pumps).

The API takes a [`ModbusUnit`](https://github.com/home-assistant-libs/modbus-connection). You own the connection: build it, hand a unit to the API, and close it when done. Building it performs no I/O — the first read establishes the link, and a link that drops later is re-established on the next request, over the same unit handle. Each register block is a component exposed on the API, and values are read as typed attributes (`None` when the register is unavailable).

```python
    import asyncio
    from modbus_connection import ModbusTcpParams
    from modbus_connection.pymodbus import ModbusConnection
    from pystiebeleltron.wpm import WpmStiebelEltronAPI

    async def main():
      connection = ModbusConnection(ModbusTcpParams(host='IP_ADDRESS_ISG', port=502))
      api = WpmStiebelEltronAPI(connection.for_unit(1))

      await api.async_update()

      print(f"outside temperature: {api.system_values.outside_temperature}")
      print(f"water comfort target temperature: {api.system_parameters.comfort_temperature}")

      # Writing a register:
      await api.system_parameters.write("comfort_temperature", 50)

      await connection.close()

    asyncio.run(main())
```

A poll reads each register block on its own, so one slow or refused block does not take the rest of it with it. `async_update()` returns an `UpdateReport` — a component whose block failed keeps the values of its last successful read, does not notify its listeners, and is listed by attribute name with the error that failed it, while every other component refreshes and notifies once the whole poll is done. A block the controller does not serve at all is in neither set. Only a dead link (`ModbusConnectionError`) raises:

```python
    report = await api.async_update()
    for name, error in report.failed.items():
        print(f"{name} kept its previous values: {error}")
```

### Reading settings apart from what the machine reports

The controller draws the line itself. What it reports — temperatures, state,
energy counters — lives in the input registers; what it has been set to — the
system parameters and the energy management settings — lives in the holding
registers, and a holding register only moves when something writes it: the
wall controller, the ISG, a schedule, or this library. So the two are separate
polls. On a WPM the settings are four of the eleven blocks and 189 of the 621
registers a full poll reads:

```python
    await api.async_update_readings()  # every cycle
    await api.async_update_settings()  # rarely, and after a write

    await api.system_parameters.write("comfort_temperature_dhw", 50)
    await api.async_update_settings()  # read back what took effect
```

`async_update()` does both and reports them together, for a caller that does
not want to schedule them apart. Every method returns an `UpdateReport` naming
only what it polls, and listeners fire at the end of the poll that read them,
so a settings poll does not hold up the readings.

Note that the set points the machine works out for itself — from the heating
curve, the operating mode and the time — are reported in the input space
alongside the temperatures (`system_values.set_temperature_hk_1` and friends),
so they refresh with the readings. Only the values someone configured are in
the settings poll.

`async_read_raw()` reads the same blocks and returns them undecoded, `{space: {address: value}}` — the payload a Home Assistant diagnostics download wants, and one that replays straight into the mock backend for a regression test. A block the controller does not serve is left out rather than failing the dump.

## License

``python-stiebel-eltron`` is licensed under MIT, for more details check LICENSE.
