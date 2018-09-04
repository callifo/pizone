"""Controller module"""

import asyncio
from asyncio import Condition
from enum import Enum
import logging
from typing import List, Dict, Union, Any

import aiohttp

from .zone import Zone

_LOG = logging.getLogger("pizone.controller")

class Controller:
    """Interface to IZone controller"""
    class Mode(Enum):
        """Valid controller modes"""
        COOL = 'cool'
        HEAT = 'heat'
        VENT = 'vent'
        DRY = 'dry'
        AUTO = 'auto'

    class Fan(Enum):
        """All fan modes"""
        LOW = 'low'
        MED = 'med'
        HIGH = 'high'
        AUTO = 'auto'

    DictValue = Union[str, int, float]
    ControllerData = Dict[str, DictValue]

    REQUEST_TIMEOUT = 3
    """Time to wait for results from server."""

    CONNECT_RETRY_TIMEOUT = 20
    """Cool-down period for retrying to connect to the controller"""

    _VALID_FAN_MODES: Dict[str, List[Fan]] = {
        'disabled' : [Fan.LOW, Fan.MED, Fan.HIGH],
        '3-speed' : [Fan.LOW, Fan.MED, Fan.HIGH, Fan.AUTO],
        '2-speed' : [Fan.LOW, Fan.HIGH, Fan.AUTO],
        'var-speed' : [Fan.LOW, Fan.MED, Fan.HIGH, Fan.AUTO],
        }

    def __init__(self, discovery, device_uid: str, device_ip: str) -> None:
        """Create a controller interface. Usually this is called from the discovery service.

        If neither device UID or address are specified, will search network
        for exactly one controller. If UID is specified then the addr is ignored.

        Args:
            device_uid: Controller UId as a string (eg: mine is '000013170')
                If specified, will search the network for a matching device
            device_addr: Device network address. Usually specified as IP address

        Raises:
            ConnectionAbortedError: If id is not set and more than one iZone instance is
                discovered on the network.
            ConnectionRefusedError: If no iZone discovered, or no iZone device discovered at
                the given IP address or UId
        """
        self._ip = device_ip
        self._discovery = discovery
        self._device_uid = device_uid

        self.zones: List[Zone] = []
        self.fan_modes: List[Controller.Fan] = []
        self._system_settings: Controller.ControllerData = {}

        self._fail_exception = None
        self._reconnect_condition = Condition()

    async def initialize(self) -> None:
        """Initialize the controller, does not complete until the system is initialised."""
        await self._refresh_system(notify=False)

        self.fan_modes = Controller._VALID_FAN_MODES[str(self._system_settings['FanAuto'])]

        zone_count = int(self._system_settings['NoOfZones'])
        self.zones = [Zone(self, i) for i in range(zone_count)]
        await self._refresh_zones(notify=False)

    @property
    def device_ip(self) -> str:
        """IP Address of the unit"""
        return self._ip

    @property
    def device_uid(self) -> str:
        '''UId of the unit'''
        return self._device_uid

    @property
    def state(self) -> bool:
        """True if the system is turned on"""
        return self._get_system_state('SysOn') == 'on'

    @state.setter
    async def state(self, value: bool) -> None:
        await self._set_system_state('SysOn', 'SystemON', 'on' if value else 'off')

    @property
    def mode(self) -> 'Mode':
        """System mode, cooling, heating, etc"""
        return self.Mode(self._get_system_state('SysMode'))

    async def set_mode(self, value: Mode):
        """Set system mode, cooling, heating, etc
        Async method, await to ensure command revieved by system.
        """
        await self._set_system_state('SysMode', 'SystemMODE', value.value)

    @property
    def fan(self) -> 'Fan':
        """The current fan level."""
        return self.Fan(self._get_system_state('SysFan'))

    async def set_fan(self, value: Fan) -> None:
        """The fan level. Not all fan modes are allowed depending on the system.
        Async method, await to ensure command revieved by system.
        Raises:
            AttributeError: On setting if the argument value is not valid
        """
        if value not in self.fan_modes:
            raise AttributeError("Fan mode {} not allowed".format(value.value))
        await self._set_system_state('SysFan', 'SystemFAN', value.value,
                                     'medium' if value is Controller.Fan.MED else value.value)

    @property
    def sleep_timer(self) -> int:
        """Current setting for the sleep timer."""
        return int(self._get_system_state('SleepTimer'))

    async def set_sleep_timer(self, value: int):
        """The sleep timer.
        Valid settings are 0, 30, 60, 90, 120
        Async method, await to ensure command revieved by system.
        Raises:
            AttributeError: On setting if the argument value is not valid
        """
        time = int(value)
        if time < 0 or time > 120 or time % 30 != 0:
            raise AttributeError(
                'Invalid Sleep Timer \"{}\", must be divisible by 30'.format(value))
        await self._set_system_state('SleepTimer', 'SleepTimer', value, time)

    @property
    def free_air_enabled(self) -> bool:
        """Test if the system has free air system available"""
        return self._get_system_state('FreeAir') != 'disabled'

    @property
    def free_air(self) -> bool:
        """True if the free air system is turned on. False if unavailable or off
        """
        return self._get_system_state('FreeAir') == 'on'

    async def set_free_air(self, value: bool) -> None:
        """Turn the free air system on or off.
        Async method, await to ensure command revieved by system.
        Raises:
            AttributeError: If attempting to set the state of the free air system
                when it is not enabled.
        """
        if not self.free_air_enabled:
            raise AttributeError('Free air is disabled')
        await self._set_system_state('FreeAir', 'FreeAir', 'on' if value else 'off')

    @property
    def temp_supply(self) -> float:
        """Current supply, or in duct, air temperature."""
        return float(self._get_system_state('Supply'))

    @property
    def temp_setpoint(self) -> float:
        """AC unit setpoint temperature.
        This is the unit target temp with with rasMode == RAS,
        or with rasMode == master and ctrlZone == 13.
        """
        return float(self._get_system_state('Setpoint'))

    async def set_temp_setpoint(self, value: float):
        """AC unit setpoint temperature.
        This is the unit target temp with with rasMode == RAS,
        or with rasMode == master and ctrlZone == 13.
        Args:
            value: Valid settings are between ecoMin and ecoMax, at 0.5 degree units.
        Raises:
            AttributeError: On setting if the argument value is not valid.
                Can still be set even if the mode isn't appropriate.
        """
        if value % 0.5 != 0:
            raise AttributeError('SetPoint \'{}\' not rounded to nearest 0.5'.format(value))
        if value < self.temp_min or value > self.temp_max:
            raise AttributeError('SetPoint \'{}\' is out of range'.format(value))
        await self._set_system_state('Setpoint', 'UnitSetpoint', value, str(value))


    @property
    def temp_return(self) -> float:
        """The return, or room, air temperature"""
        return float(self._get_system_state('Temp'))

    @property
    def eco_lock(self) -> bool:
        """True if eco lock setting is on."""
        return self._get_system_state('EcoLock') == 'true'

    @property
    def temp_min(self) -> float:
        """The value for the eco lock minimum, or 15 if eco lock not set"""
        return float(self._get_system_state('EcoMin')) if self.eco_lock else 15.0

    @property
    def temp_max(self) -> float:
        """The value for the eco lock maxium, or 30 if eco lock not set"""
        return float(self._get_system_state('EcoMax')) if self.eco_lock else 30.0

    @property
    def ras_mode(self) -> str:
        """This indicates the current selection of the Return Air temperature Sensor.
        Possible values are:
            master: the AC unit is controlled from a CTS, which is manually selected
            RAS:    the AC unit is controller from its own return air sensor
            zones:  the AC unit is controlled from a CTS, which is automatically
                selected dependant on the cooling/ heating need of zones.
        """
        return self._get_system_state('RAS')

    @property
    def zone_ctrl(self) -> int:
        """This indicates the zone that currently controls the AC unit.
        Value interpreted in combination with rasMode"""
        return int(self._get_system_state('CtrlZone'))

    @property
    def zones_total(self) -> int:
        """This indicates the number of zones the system is configured for."""
        return int(self._get_system_state('NoOfZones'))

    @property
    def zones_const(self) -> int:
        """This indicates the number of constant zones the system is configured for."""
        return self._get_system_state('NoOfConst')

    @property
    def sys_type(self) -> str:
        """This indicates the type of the iZone system connected. Possible values are:
        110: the system is zone control only and all the zones are OPEN/CLOSE zones
        210: the system is zone control only. Zones can be temperature controlled,
            dependant on the zone settings.
        310: the system is zone control and unit control.
        """
        return self._get_system_state('SysType')

    async def _refresh_system(self, notify: bool = True) -> None:
        """Refresh the system settings."""
        values: Controller.ControllerData = await self._get_resource('SystemSettings')

        if self._device_uid != values['AirStreamDeviceUId']:
            _LOG.error("_refresh_system called with unmatching device ID")
            return

        self._system_settings = values

        if notify:
            self._discovery.controller_update(self)

    async def _refresh_zones(self, notify: bool = True) -> None:
        """Refresh the Zone information."""
        zones = int(self._system_settings['NoOfZones'])
        await asyncio.gather(*[self._refresh_zone_group(i, notify)
                               for i in range(0, zones, 4)])

    async def _refresh_zone_group(self, group: int, notify: bool = True):
        assert group in [0, 4, 8]
        zone_data_part = await self._get_resource(f"Zones{group+1}_{group+4}")

        for i in range(4):
            zone_data = zone_data_part[i]
            self.zones[i+group]._update_zone(zone_data, notify) #pylint: disable=protected-access

    async def _trigger_reconnect(self):
        async with self._reconnect_condition:
            self._reconnect_condition.notify()

    def _refresh_address(self, address):
        """Called from discovery to update the address"""
        if self._ip == address:
            return
        self._ip = address
        # Signal to the retry connection loop to have another go.
        if self._fail_exception:
            self._discovery.create_task(self._trigger_reconnect())

    def _get_system_state(self, state):
        self._ensure_connected()
        return self._system_settings[state]

    async def _set_system_state(self, state, command, value, send=None):
        if send is None:
            send = value
        if self._system_settings[state] == value:
            return
        await self._send_command_async(command, send)
        self._system_settings[state] = value

    def _ensure_connected(self) -> None:
        if self._fail_exception:
            raise ConnectionError("Unable to connect to the controller") from self._fail_exception

    def _failed_connection(self, ex):
        if self._fail_exception:
            self._fail_exception = ex
            return
        self._fail_exception = ex
        self._discovery.controller_disconnected(self, ex)
        self._discovery.create_task(self._retry_connection())

    async def _retry_connection(self) -> None:
        while True:
            await self._discovery.rescan()

            # event will be fired if reconnected
            async with self._reconnect_condition:
                await asyncio.wait_for(self._reconnect_condition.wait(),
                                       Controller.CONNECT_RETRY_TIMEOUT)

            _LOG.info("Attempting to reconnect to server uid=%s ip=%s",
                      self.device_uid, self.device_ip)

            try:
                await asyncio.gather(self._refresh_system(notify=False),
                                     self._refresh_zones(notify=False))

                self._fail_exception = None

                self._discovery.controller_reconnected(self)
                self._discovery.controller_update(self)
                for zone in self.zones:
                    self._discovery.zone_update(self, zone)
                break
            except ConnectionError as ex:
                # Expected, just carry on.
                _LOG.warning("Reconnect attempt for uid=%s failed with exception: %s",
                             self.device_uid, ex.__repr__())

    async def _get_resource(self, resource: str):
        try:
            session = self._discovery.session
            async with session.get('http://%s/%s' % (self.device_ip, resource),
                                   timeout=Controller.REQUEST_TIMEOUT) as response:
                return await response.json()
        except (asyncio.TimeoutError, aiohttp.ClientError) as ex:
            self._failed_connection(ex)
            raise ConnectionError("Unable to connect to the controller") from ex

    async def _send_command_async(self, command: str, data: Any):
        body = {command : data}
        url = f"http://{self.device_ip}/{command}"
        try:
            session = self._discovery.session
            async with session.post(url,
                                    timeout=Controller.REQUEST_TIMEOUT,
                                    json=body) as response:
                response.raise_for_status()
        except (asyncio.TimeoutError, aiohttp.ClientError) as ex:
            self._failed_connection(ex)
            raise ConnectionError("Unable to connect to the controller") from ex
