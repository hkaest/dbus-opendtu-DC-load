
# system imports:
import configparser
import os
import sys
import logging
import time
import requests  # for http GET an POST
from requests.auth import HTTPBasicAuth
import copy


# victron imports:
import dbus

if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject

sys.path.insert(
    1,
    os.path.join(
        os.path.dirname(__file__),
        "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python",
    ),
)
from vedbus import VeDbusService  # noqa - must be placed after the sys.path.insert
from version import softwareversion
from vedbus import VeDbusItemImport


# Singleton metaclass, see pattern ...
class Singleton(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(Singleton, cls).__call__(*args, **kwargs)
        return cls._instances[cls]

# DTU Socket class using a session to communicate with the DTU, http get meter data once for all inverters and http put individually 
class DtuSocket(metaclass=Singleton):

    def __init__(self):
        self._session = None
        self._meter_data = None
        self.host = None
        self.username = None
        self.password = None
        self.httptimeout = None
        self.ConnectError = 0
        self.ReadError = 0
        self.WriteError = 0
        self.FetchCounter = 0
        self.SwitchCounter = 0
        self.ResetCounter = 0
        self._initSession()

    def _initSession(self):        
        # set global session once for inverter 0 for all inverters
        if not self._session:
            self._read_config_dtu()
            #s = requests.session(config={'keep_alive': False})
            self._session = requests.Session()
            if self.username and self.password:
                logging.info("initialize session to use basic access authentication...")
                self._session.auth=(self.username, self.password)
            # first fetch on first inverter
            self._refresh_data()

    def getLimitData(self, pvinverternumber):
        # copied json strings are passed to the inverters and hopefully collected with an garbage collector when an new string is passed
        return self._meter_data["inverters"][pvinverternumber].copy() if self._meter_data else None
    
    def fetchLimitData(self):
        self.SwitchCounter = 0
        self.ResetCounter = max(0, self.ResetCounter - 1)
        if self._session:
            result = False
            try: 
                ageBeforeRefresh = (self._meter_data["inverters"][0]["data_age"]) if self._meter_data else 0
                self._refresh_data()
                ageAfterRefresh = (self._meter_data["inverters"][0]["data_age"])
                result = (ageBeforeRefresh != ageAfterRefresh)
            finally:
                return result
        else:
            return False
    
    # curl -u "User:Passwort" http://10.1.1.98/api/power/config -d 'data={"serial":"11418308xxxx","restart":true}'
    def resetDevice(self, pvinverternumber):
        result = 0  # 0 AKA not connected
        try:
            invSerial = self._meter_data["inverters"][pvinverternumber]["serial"]
            name = self._meter_data["inverters"][pvinverternumber]["name"]
            url = f"http://{self.host}/api/power/config"
            payload = f'data={{"serial":"{invSerial}", "restart":true}}'
            rsp = self._session.post(
                url = url, 
                data = payload,
                headers = {'Content-Type': 'application/x-www-form-urlencoded'}, 
                timeout=float(self.httptimeout)
                )
            logging.info(f"RESULT: resetDevice, response = {str(rsp.status_code)}")
            if rsp:
                result = 1
        except Exception as e:
            logging.warning(f"HTTP Error at resetDevice for inverter "
                f"{pvinverternumber} ({name}): {str(e)}")
        finally:
            return result
    
    # curl -u "User:Passwort" http://10.1.1.98/api/maintenance/reboot -d 'data={"reboot":true}'
    def resetDTU(self):
        result = 0  # 0 AKA not connected
        if self.ResetCounter != 0:
             logging.info(f"RESULT: resetDTU, skip resetting to avoid to much resetting")
             return 1 # skip resetting to avoid to much resetting
        try:
            for invData in self._meter_data["inverters"]:
                if bool(invData["producing"] in (1, '1', True, "True", "TRUE", "true")):
                    return 1  # if at least one inverter is producing do not reset the device
            url = f"http://{self.host}/api/maintenance/reboot"
            payload = f'data={{"reboot":true}}'
            rsp = self._session.post(
                url = url, 
                data = payload,
                headers = {'Content-Type': 'application/x-www-form-urlencoded'}, 
                timeout=float(self.httptimeout)
                )
            logging.info(f"RESULT: resetDevice, response = {str(rsp.status_code)}")
            self.ResetCounter = 10 # avoid to much reset in case of connection problems, only allow reset every 10 loops, depends on loop time counted in seconds
            if rsp:
                result = 1
        except Exception as e:
            logging.warning("HTTP Error on reboot DTU")
        finally:
            return result
    
    def pushNewLimit(self, pvinverternumber, newLimitPercent):
        result = 0  # 0 AKA not connected
        try:
            invSerial = self._meter_data["inverters"][pvinverternumber]["serial"]
            name = self._meter_data["inverters"][pvinverternumber]["name"]
            url = f"http://{self.host}/api/limit/config"
            payload = f'data={{"serial":"{invSerial}", "limit_type":1, "limit_value":{newLimitPercent}}}'
            rsp = self._session.post(
                url = url, 
                data = payload,
                headers = {'Content-Type': 'application/x-www-form-urlencoded'}, 
                timeout=float(self.httptimeout)
                )
            logging.info(f"RESULT: pushNewLimit, response = {str(rsp.status_code)}")
            if rsp:
                result = 1
        except Exception as e:
            self.WriteError += 1
            logging.warning(f"HTTP Error at pushNewLimit for inverter "
                f"{pvinverternumber} ({name}): {str(e)}")
        finally:
            return result

    def switchOnOff(self, pvinverternumber, boOn):
        result = 0  # 0 AKA not connected
        if self.SwitchCounter != 0:
             logging.info(f"RESULT: switchOnOff, skip switching to avoid to much switching")
             return 0 # skip switching to avoid to much switching
        try:
            invSerial = self._meter_data["inverters"][pvinverternumber]["serial"]
            name = self._meter_data["inverters"][pvinverternumber]["name"]
            url = f"http://{self.host}/api/power/config"
            payload = f'data={{"serial":"{invSerial}", "power":{int(boOn)}}}'
            rsp = self._session.post(
                url = url, 
                data = payload,
                headers = {'Content-Type': 'application/x-www-form-urlencoded'}, 
                timeout=float(self.httptimeout)
                )
            logging.info(f"RESULT: switchOnOff, response = {str(rsp.status_code)}")
            if rsp:
                result = 1
                self.SwitchCounter += 1
        except Exception as e:
            self.WriteError += 1
            logging.warning(f"HTTP Error at switchOnOff for inverter "
                f"{pvinverternumber} ({name}): {str(e)}")
        finally:
            return result

    def getErrorCounter(self):
        return (self.FetchCounter, self.ReadError, self.WriteError, self.ConnectError)

    # read config file
    def _read_config_dtu(self):
        config = configparser.ConfigParser()
        config.read(f"{(os.path.dirname(os.path.realpath(__file__)))}/config.ini")
        self.host = config["DEFAULT"]["Host"]
        self.username = config["DEFAULT"]["Username"]
        self.password = config["DEFAULT"]["Password"]
        self.httptimeout = config["DEFAULT"]["HTTPTimeout"]

    def _refresh_data(self):
        '''Fetch new data from the DTU API and store in locally if successful.'''
        url = f"http://{self.host}/api/livedata/status"
        meter_data = self._fetch_url(url)
        if meter_data:
            try:
                self._check_opendtu_data(meter_data)
                #Store meter data for later use in other methods
                self._meter_data = meter_data
                self.FetchCounter = _incLimitCnt(self.FetchCounter)
            except Exception as e:
                logging.critical('Error at %s', '_fetch_url', exc_info=e)
        else:
            logging.info("_fetch_url returned null, reset session ")
            # self._session.close()
            # self._session = requests.Session()
        
    def _check_opendtu_data(self, meter_data):
        ''' Check if OpenDTU data has the right format'''
        # Check for OpenDTU Version
        if (    (not "AC" in meter_data["inverters"][0])
             or (not "DC" in meter_data["inverters"][0])):
            raise ValueError("Response from OpenDTU does not contain AC or DC data")

    # @timeit
    def _fetch_url(self, url):
        '''Fetch JSON data from url. Throw an exception on any error. Only return on success.'''
        json = None
        try:
            logging.debug(f"calling {url} with timeout={self.httptimeout}")
            rsp = self._session.get(url=url, timeout=float(self.httptimeout))
            rsp.raise_for_status() #HTTPError for status code >=400
            logging.info(f"_fetch_url response status code: {str(rsp.status_code)}")
            json = rsp.json()
        except requests.HTTPError as http_err:
            logging.info(f"_fetch_url response http error: {http_err}")
        except requests.ConnectTimeout as e:
            # Requests that produced this error are safe to retry.
            self.ConnectError += 1
        except requests.ReadTimeout as e:
            self.ReadError += 1
        except requests.ConnectionError as e:
            # site does not exist
            self.ConnectError += 1
        except Exception as err:
            logging.critical('Error at %s', '_fetch_url', exc_info=err)
        finally:
            return json


# Constants for meta data and control
PRODUCTNAME = "OpenDTU"
CONNECTION = "TCP/IP (HTTP)"
PRODUCT_ID = 0
FIRMWARE_VERSION = 0
HARDWARE_VERSION = 0
CONNECTED = 1

COUNTERLIMIT = 255
PRODUCE_COUNTER = 90 #number of loops, depends on loop time counted in seconds
ON_COUNTER_VALUE = 60 #number of loops, depends on loop time counted in seconds
OFF_COUNTER_VALUE = 0 #number of loops, depends on loop time counted in seconds

STATE_OK = 8
STATE_ALARM = 9
ALARM_OK = 0
ALARM_WARNING = 1
ALARM_ALARM = 2
ALARM_GRID = "Grid Shelly HTTP"
ALARM_TEMPERATURE = "Temperature"
ALARM_DTU = "OpenDTU HTTP Push"
ALARM_FETCH = "OpenDTU HTTP Fetch"
ALARM_HM = "OpenDTU HM state"
ALARM_BALCONY = "Balcony Shelly HTTP"
ALARM_BATTERY = "Battery charge current limit"
ALARM_NONE = "HM status (--)"

TEMPERATURE_OFF_OFFSET = 5 #deegre to cool down


def _incLimitCnt(value):
    return (value + 1) % COUNTERLIMIT

def _is_true(val):
    '''helper function to test for different true values'''
    return val in (1, '1', True, "True", "true")


# DBUS registry metaclass for all instance of DBUS service, see pattern in ...
class DCloadRegistry(type):
    '''Run a registry for all PV Inverter'''
    def __iter__(cls):
        return iter(cls._registry)

# DBUS service metaclass for common definitions and functions    
class DCLoadDbusService(metaclass=DCloadRegistry):
    _registry = []
    _servicename = None

    def __init__(
        self,
        servicename,
        deviceinstance, 
        paths,
    ):
        self._registry.append(self)
        self._servicename = servicename
        self._deviceinstance = deviceinstance

        # Allow for multiple Instance per process in DBUS
        dbus_conn = (
            dbus.SessionBus()
            if "DBUS_SESSION_BUS_ADDRESS" in os.environ
            else dbus.SystemBus(private=True)
        )

        self._dbusservice = VeDbusService("{}.http_{:03d}".format(servicename, self._deviceinstance), dbus_conn)

        # Create the mandatory objects
        self._dbusservice.add_mandatory_paths(__file__, softwareversion, CONNECTION, self._deviceinstance, PRODUCT_ID, PRODUCTNAME, FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
         # add path values to dbus
        self._paths = paths
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings["initial"],
                gettextcallback=settings["textformat"],
                writeable=True,
                onchangecallback=self.handlechangedvalue,
            )

    # https://github.com/victronenergy/velib_python/blob/master/dbusdummyservice.py#L63
    def handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True # accept the change

    # read config file
    def _read_config_dtu_self(self, actual_inverter):
        config = configparser.ConfigParser()
        config.read(f"{(os.path.dirname(os.path.realpath(__file__)))}/config.ini")
        self.configDeviceInstance = int(config[f"INVERTER{actual_inverter}"]["DeviceInstance"])
        self.configStatusTime = config["DEFAULT"]["DTU_statusTime"]
        self.configMinPercent = int(config["DEFAULT"]["MinPercent"])
        self.configMaxPercent = int(config["DEFAULT"]["MaxPercent"])
        self.configStepsPercent = int(config["DEFAULT"]["stepsPercent"])
        self.configMaxTemperature = int(config["DEFAULT"]["maxTemperature"])
        self.configEnableSwitchOff = config[f"INVERTER{actual_inverter}"].getboolean("enableSwitchOff", fallback=True)


# DBUS com.victronenergy.dcsystem class, consumed power by HM inverters added to the production limit (CCL) of solar inverters 
class DCSystemService(DCLoadDbusService):
    def __init__(
        self,
        servicename,
        paths,
        actual_inverter,
    ):
        # load config data, self.deviceinstance ...
        self._read_config_dtu_self(actual_inverter)

        # init & register DBUS service
        super().__init__(servicename, self.configDeviceInstance, paths)

        self._dbusservice.add_path("/CustomName", "DC Consumer (CCL)")
        self.setPower(0, 0, 0, 0)

    # public functions
    def setPower(self, volt, ampere, power, temp):
        self._dbusservice["/Dc/0/Voltage"] = volt
        self._dbusservice["/Dc/0/Current"] = ampere
        self._dbusservice["/Dc/0/Power"] = power
        self._dbusservice["/Dc/0/Temperature"] = temp
   

# DBUS com.victronenergy.temperature class for temperature rule of GX relay to control a battery heater
class DCTempService(DCLoadDbusService):
    def __init__(
        self,
        servicename,
        paths,
        actual_inverter,
    ):
        # load config data, self.deviceinstance ...
        self._read_config_dtu_self(actual_inverter)

        # init & register DBUS service
        super().__init__(servicename, self.configDeviceInstance, paths)

        self._dbusservice.add_path("/CustomName", "Pytes Heater (Relay)")
        self.setTemperature(0)

    # public functions
    def setTemperature(self, temp):
        self._dbusservice["/Temperature"] = temp

    def getTemperature(self):
        return self._dbusservice["/Temperature"]

class DCAlarmService(DCLoadDbusService):
    _alarmInstance = None
    def __init__(
        self,
        servicename,
        paths,
        actual_inverter,
    ):
        # load config data, self.deviceinstance ...
        self._read_config_dtu_self(actual_inverter)
        # init & register DBUS service
        super().__init__(servicename, self.configDeviceInstance, paths)
        self._dbusservice.add_path("/CustomName", ALARM_NONE, writeable=True)
        self.__class__._alarmInstance = self

    # set alarm, first wins
    def setAlarmName(self, name):
        if self._dbusservice["/Alarm"] == ALARM_OK:
            self._dbusservice["/CustomName"] = name
            self.setAlarmState(True)

    # reset alarm, if alarm name matches
    def resetAlarmName(self, name):
        if self._dbusservice["/Alarm"] == ALARM_ALARM and self._dbusservice["/CustomName"] == name:
            self.setAlarmState(False)
            self._dbusservice["/CustomName"] = ALARM_NONE

    # public functions
    def setAlarmState(self, on):
        # /Alarm 0 = ok, 2 = alarm
        # /State 8 = ok, 9 = alarm
        if on:
            self._dbusservice["/Alarm"] = ALARM_ALARM
            self._dbusservice["/State"] = STATE_ALARM
        else:
            self._dbusservice["/Alarm"] = ALARM_OK
            self._dbusservice["/State"] = STATE_OK

def setAlarmOnService(name, device: str, on: bool):
    inst:DCAlarmService = DCAlarmService._alarmInstance
    txt = ALARM_NONE
    if device:
        txt = f"HM status ({device}: {name})"
    else:
        txt = f"HM status ({name})"
    if on: 
        inst.setAlarmName(txt)
    else:
        inst.resetAlarmName(txt)


# DBUS com.victronenergy.dcload class for HM inverters logic using singleto class DtuSocket for DTU communication    
class OpenDTUService(DCLoadDbusService):
    _alarm_mapping = {
        ALARM_GRID:"/Alarms/LowVoltage",
        ALARM_TEMPERATURE:"/Alarms/HighVoltage",
        ALARM_DTU:"/Alarms/LowStarterVoltage",
        ALARM_HM:"/Alarms/HighStarterVoltage",
        ALARM_BALCONY:"/Alarms/LowTemperature",
        ALARM_BATTERY:"/Alarms/HighTemperature",
    }

    def __init__(
        self,
        servicename,
        paths,
        actual_inverter,
        data=None,
    ):
        self._socket = DtuSocket()
        self._meter_data = data
        self.pvinverternumber = actual_inverter
        # load config data, self.deviceinstance ...
        self._read_config_dtu_self(actual_inverter)

        # init & register DBUS service
        super().__init__(servicename, self.configDeviceInstance, paths)

        self._tempAlarm = False
        self._WriteAlarm = False

        # Use dummy data
        self.invName = self._meter_data["name"] if data else "no DTU data"
        self.invSerial = self._meter_data["serial"] if data else "--"

        # Counter         
        self._dbusservice.add_path("/UpdateCount", 0)
        self._dbusservice.add_path("/ConnectError", 0)
        self._dbusservice.add_path("/ReadError", 0)
        self._dbusservice.add_path("/WriteError", 0)
        self._dbusservice.add_path("/FetchCounter", 0)
        self._dbusservice.add_path("/SetLimitCounter", 0)
        self._dbusservice.add_path("/HmAlarmWaitCounter", 0)
        self._dbusservice.add_path("/LastLimit", 0)

        # State machine variables for HM inverter control
        self._hm_state = "Init"  # Init, Connect, Grid, Producing, SwitchOff, Off, SwitchOn, Error
        self._hm_state_timeout = 0  # Counter for state timeouts
        self._hm_data_age = 0  # Track data age for error detection
        self._hm_state_before_error = None  # Preserve active state when entering Error
        self._dbusservice.add_path("/HmState", self._hm_state)
        self._dbusservice.add_path("/HmStateTimeout", self._hm_state_timeout)

        logging.debug("%s /DeviceInstance = %d", servicename, self.configDeviceInstance)

        # Custom name setting
        self._dbusservice.add_path("/CustomName", self.invName)
        logging.info(f"Name of Inverters found: {self.invName}")

        # add _update as cyclic call not as fast as setToZeroPower is called
        gobject.timeout_add_seconds((5 if not self.configStatusTime else int(self.configStatusTime)), self._update)

    # public functions
    def setAlarm(self, alarm: str, on: bool):
        setValue = ALARM_ALARM if on else ALARM_OK
        actvalue = self._dbusservice[self._alarm_mapping[alarm]] 
        if setValue != actvalue:
            self._dbusservice[self._alarm_mapping[alarm]] = setValue
   
    # public functions, load meter data and return current current
    def updateMeterData(self):
        self._meter_data = self._socket.getLimitData(self.pvinverternumber)
        # Copy current error counter to DBU values
        ( self._dbusservice["/FetchCounter"],
          self._dbusservice["/ReadError"],
          self._dbusservice["/WriteError"],
          self._dbusservice["/ConnectError"] ) = self._socket.getErrorCounter()
        hmProducing = self._is_hm_producing() # TODO use state
        return self._meter_data["DC"]["0"]["Current"]["v"] if hmProducing else 0.0 #"Current":{"v":6.070000172,"u":"A","d":2}

    def setToZeroPower(self, gridPower, maxFeedIn):
        addFeedIn = 0
        actFeedIn = 0
        logging.info(f"START: setToZeroPower, grid = {gridPower}, maxFeedIn = {maxFeedIn}, {self.invName}")
        root_meter_data = self._meter_data
        hmConnected = self._is_hm_connected()
        gridConnected = self._is_grid_connected()
        hmProducing = self._is_hm_producing()
        if hmProducing:
            self._dbusservice["/HmAlarmWaitCounter"] = 0  # activate disable state error period 
            setAlarmOnService(ALARM_HM, self.invName, not hmConnected)
        elif not gridConnected:
            self._dbusservice["/HmAlarmWaitCounter"] = 0  # activate disable state error period 
            setAlarmOnService(ALARM_HM, self.invName, not hmConnected)
        elif self._dbusservice["/HmAlarmWaitCounter"] < PRODUCE_COUNTER:
            self._dbusservice["/HmAlarmWaitCounter"] = _incLimitCnt(self._dbusservice["/HmAlarmWaitCounter"])
        else:
            setAlarmOnService(ALARM_HM, self.invName, not hmConnected)

        oldLimitPercent = int(root_meter_data["limit_relative"])
        maxPower = int((int(root_meter_data["limit_absolute"]) * 100) / oldLimitPercent) if oldLimitPercent else 0
        # check if temperature is lower than xx degree and inverter is coinnected to grid (power is always != 0 when connected)
        actTemp = int(root_meter_data["INV"]["0"]["Temperature"]["v"])
        if actTemp > self.configMaxTemperature and gridPower > 0:
            self._tempAlarm = True
        elif actTemp < (self.configMaxTemperature - TEMPERATURE_OFF_OFFSET):
             self._tempAlarm = False
        setAlarmOnService(ALARM_TEMPERATURE, self.invName, self._tempAlarm)
        if self._tempAlarm:
            logging.info(f"RESULT: setToZeroPower, temperature to high = {actTemp}")
        elif not hmConnected:
            logging.info("RESULT: setToZeroPower, not conneceted to DTU")
            result = self._socket.resetDTU()
        elif not gridConnected:
            logging.info("RESULT: setToZeroPower, not conneceted to grid")
        elif not hmProducing and self._dbusservice["/HmAlarmWaitCounter"] >= PRODUCE_COUNTER:
            logging.info("RESULT: setToZeroPower, conneceted to DTU / Grid, but not producing")
            result = self._socket.resetDevice(self.pvinverternumber)
        # calculate new limit
        if maxPower > 0 and hmConnected: # and limitStatus in ('Ok', 'OK'):
            # check allowedFeedIn with active feed in
            actFeedIn = int(oldLimitPercent * maxPower / 100)
            allowedFeedIn = maxFeedIn - actFeedIn
            addFeedIn = gridPower
            if addFeedIn > allowedFeedIn:
                addFeedIn = allowedFeedIn

            # calculate new limit percent with steps
            newLimitPercent = int(int((oldLimitPercent + (addFeedIn * 100 / maxPower)) / self.configStepsPercent) * self.configStepsPercent)
            if newLimitPercent < self.configMinPercent:
                newLimitPercent = self.configMinPercent
            if newLimitPercent > self.configMaxPercent:
                newLimitPercent = self.configMaxPercent
            if not gridConnected or self._tempAlarm or not hmProducing or self._hm_state != "Producing":
                self._dbusservice["/LastLimit"] = newLimitPercent #signal state machine new limits to switch on
                newLimitPercent = self.configMinPercent

            # check if limit should be updated
            if abs(newLimitPercent - oldLimitPercent) > 0:
                if self._dbusservice["/LastLimit"] != oldLimitPercent:
                    # wait one cycle until limit is applied to avoid to much pushing of limits to the DTU
                    self._dbusservice["/LastLimit"] = oldLimitPercent
                else:
                    # check if limit has already been set
                    result = self._socket.pushNewLimit(self.pvinverternumber, newLimitPercent)
                    setAlarmOnService(ALARM_DTU, self.invName, (not result and self._WriteAlarm))
                    self._WriteAlarm = not result # ignore first error
                    self._dbusservice["/SetLimitCounter"] = _incLimitCnt(self._dbusservice["/SetLimitCounter"]) # increase counter to signal limit change, can be used for debugging
                    if not result: # reset to oldLimitPercent on error
                        newLimitPercent = oldLimitPercent
                    else:
                        self._dbusservice["/LastLimit"] = newLimitPercent

            # return reduced gridPower values
            addFeedIn = int((newLimitPercent - oldLimitPercent) * maxPower / 100)
            logging.info(f"RESULT: setToZeroPower, result = {addFeedIn}")
            # set DBUS power to new set value
            actFeedIn = int(newLimitPercent * maxPower / 100)
            # use /Dc/1/Voltage showed in details as control loop AC power set value
            self._dbusservice["/Dc/1/Voltage"] = actFeedIn
        return [int(gridPower - addFeedIn),int(maxFeedIn - actFeedIn)]
    
    # ============================================================================
    # State Machine for HM Inverter Control
    # States: Init -> Connect -> Grid/Producing -> SwitchOff -> Off -> SwitchOn
    # Error state can be triggered from any state if data is stale
    # ============================================================================
    
    def _hm_state_machine(self):
        # Main state machine for controlling HM inverter power state. Called from _update() after data fetch.
        if not self._meter_data:
            logging.warning("HM State Machine: No meter data available")
            return

        current_data_age = self._meter_data.get("data_age", 0)
        data_is_stale = (current_data_age == self._hm_data_age)
        self._hm_data_age = current_data_age

        if data_is_stale:
            if self._hm_state != "Error":
                self._hm_enter_error()
                self._dbusservice["/HmState"] = self._hm_state
                self._dbusservice["/HmStateTimeout"] = self._hm_state_timeout
                return
        elif self._hm_state == "Error":
            if self._hm_state_before_error:
                logging.info(
                    f"HM State Error: communication recovered, restoring {self._hm_state_before_error}"
                )
                previous_state = self._hm_state_before_error
                self._hm_state_before_error = None
                self._hm_set_state(previous_state, 0)
            else:
                self._hm_set_state("Init", 0)

        if self._hm_state == "Init":
            self._state_init()
        elif self._hm_state == "Connect":
            self._state_connected()
        elif self._hm_state == "Grid":
            self._state_grid()
        elif self._hm_state == "Producing":
            self._state_producing()
        elif self._hm_state == "SwitchOff":
            self._state_switch_off()
        elif self._hm_state == "Off":
            self._state_off()
        elif self._hm_state == "SwitchOn":
            self._state_switch_on()
        elif self._hm_state == "Error":
            self._state_error()

        # Update DBUS paths
        self._dbusservice["/HmState"] = self._hm_state
        self._dbusservice["/HmStateTimeout"] = self._hm_state_timeout
    
    def _state_init(self):
        # Init state: Check HM connectivity, grid, and production on startup.
        if not self._is_hm_connected():
            self._hm_set_state("Connect")  # Actually not connected, but wait in this state
        elif not self._is_grid_connected():
            self._hm_set_state("Grid")
        elif self._is_hm_producing():
            self._hm_set_state("Producing")

    def _state_connect(self):
        # Connect state: Wait for HM to connect.
        if not self._is_hm_connected():
            return  # Stay in Connect state
        if self._is_grid_connected() and self._is_hm_producing():
            self._hm_set_state("Producing")
        else:
            self._hm_set_state("Grid")
    
    def _state_grid(self):
        # Grid state: Grid is connected but HM not yet producing
        if not self._is_hm_connected():
            self._hm_set_state("Connect")
        elif not self._is_grid_connected():
            return  # Stay in Grid state
        elif self._is_hm_producing():
            self._hm_set_state("Producing")
        # After a ceratin time with grid connection but no production, try to switch on
        self._hm_state_timeout += 1
        # Configurable time before switching off (e.g., 90 loops)
        if self._hm_state_timeout >= 90:
            self._trigger_switch_on()
    
    def _state_producing(self):
        # Producing state: HM is actively producing power. Transition to SwitchOff after configurable time with minLimit.
        if not self._is_hm_connected():
            self._hm_set_state("Connect")
            return
        if not self._is_grid_connected() or not self._is_hm_producing():
            self._hm_set_state("Grid")
            return
        # Check if limit is at minimum and should trigger SwitchOff
        if self._dbusservice["/LastLimit"] <= self.configMinPercent:
            self._hm_state_timeout += 1
            # Configurable time before switching off (e.g., 10 loops)
            if self._hm_state_timeout >= 90:
                if self.configEnableSwitchOff:
                    self._trigger_switch_off()
                else:
                    self._hm_state_timeout = 0
        else:
            self._hm_state_timeout = 0  # Reset timeout if not at min limit
    
    def _state_off(self):   
        # Off state: HM is off. Wait for rising edge of producing signal to transition to
        if self._is_hm_producing():
            self._hm_set_state("Producing")   
        # Check if limit is requesting production and should trigger SwitchOn
        if self._dbusservice["/LastLimit"] > self.configMinPercent:
            self._hm_state_timeout += 1
            # Configurable time before switching on (e.g., 10 loops)
            if self._hm_state_timeout >= 20:
                self._trigger_switch_on()
        else:
            self._hm_state_timeout = 0  # Reset timeout if not at min limit
    
    def _state_switch_off(self):
        # SwitchOff state: Transitioning HM to off. Wait for falling edge of producing signal.
        if not self._is_hm_producing():
            self._hm_set_state("Off", 0)
            return
        # Timeout after 30 loops if still producing
        self._hm_state_timeout += 1
        if self._hm_state_timeout >= 30:
            logging.warning(f"HM State SwitchOff timeout for {self.invName}, forcing Off state")
            self._hm_set_state("Producing", 0)
    
    def _state_switch_on(self):
        # SwitchOn state: Transitioning HM to on. Wait for rising edge of producing signal.
        if self._is_hm_producing():
            self._hm_set_state("Producing", 0)
            return
        # Timeout after 60 loops if not producing after switch on attempt
        self._hm_state_timeout += 1
        if self._hm_state_timeout >= 60:
            logging.warning(f"HM State SwitchOn timeout for {self.invName}, returning to Off state")
            self._hm_set_state("Off", 0)
    
    def _state_error(self):
        # rror state: Data fetch or update is not working. Wait for DTU recovery (90 loops). If not recovered, reset DTU.
        self._hm_state_timeout += 1
        # Allow 90 loops for recovery
        if self._hm_state_timeout < 90:
            return
        # After 90 loops, attempt DTU reset
        logging.error(f"HM State Error: DTU recovery failed after 90 loops for {self.invName}, resetting DTU")
        self._socket.resetDTU()
        self._hm_state_before_error = None
        self._hm_set_state("Init", 0)  # Return to Init after reset attempt
    
    def _hm_enter_error(self):
        # Enter Error state and preserve the previous active state.
        if self._hm_state != "Error":
            self._hm_state_before_error = self._hm_state
            self._hm_set_state("Error", 0)
        else:
            self._hm_state_timeout = 0

    def _hm_set_state(self, new_state, timeout=0):
        # Set new state and optional timeout.
        if self._hm_state != new_state:
            logging.info(f"HM State Transition: {self._hm_state} -> {new_state}")
            self._hm_state = new_state
        self._hm_state_timeout = timeout
    
    # Trigger functions for external state transitions
    def trigger_switch_off(self):
        # Trigger transition to SwitchOff state.
        if self._hm_state == "Producing":
            if self.configEnableSwitchOff:
                logging.info(f"Triggering SwitchOff for {self.invName}")
                self._trigger_switch_off()
    
    def _trigger_switch_off(self):
        # Internal trigger to switch off HM.
        if not self.configEnableSwitchOff:
            logging.info(f"HM SwitchOff disabled for {self.invName}, internal switch off skipped")
        result = self._socket.switchOnOff(self.pvinverternumber, False)
        self._hm_set_state("SwitchOff", 0)
        logging.info(f"HM SwitchOff command sent, result={result}")
    
    def trigger_switch_on(self):
        # Trigger transition to SwitchOn state.
        if self._hm_state == "Off":
            logging.info(f"Triggering SwitchOn for {self.invName}")
            self._trigger_switch_on()

    def _trigger_switch_on(self):
        # Internal trigger to switch on HM.
        result = self._socket.switchOnOff(self.pvinverternumber, True)
        self._hm_set_state("SwitchOn", 0)
        logging.info(f"HM SwitchOn command sent, result={result}")

    # Helper methods to check HM conditions
    def _is_hm_connected(self):
        # Check if HM is connected and reachable.
        try:
            return bool(self._meter_data.get("reachable") in (1, '1', True, "True", "TRUE", "true"))
        except (ValueError, TypeError):
            return False

    def _is_grid_connected(self):
        # Check if HM is connected to grid.
        try:
            # bool(int(root_meter_data["AC"]["0"]["Voltage"]["v"]) > 100)
            ac_voltage = int(float(self._meter_data.get("AC", {}).get("0", {}).get("Voltage", {}).get("v", 0)))
            return ac_voltage > 100
        except (ValueError, TypeError):
            return False
    
    def _is_hm_producing(self):
        # Check if HM is actively producing power.
        try:
            return bool(self._meter_data.get("producing") in (1, '1', True, "True", "TRUE", "true"))
        except (ValueError, TypeError):
            return False

    # slower update loop, a update triggers the DBUS-Monitor from com.victronenergy.system
    #  /Control/SolarChargeCurrent  -> 0: no limiting, 1: solar charger limited by user setting or intelligent battery
    #  /Dc/System/MeasurementType should be 1 (calculated by dcsystems)
    #  /Dc/System/Power should be equal to the sum of self._dbusservice["/Dc/0/Power"]
    def _update(self):
        try:
            self._meter_data = self._socket.getLimitData(self.pvinverternumber)
            # Copy current error counter to DBU values
            ( self._dbusservice["/FetchCounter"],
            self._dbusservice["/ReadError"],
            self._dbusservice["/WriteError"],
            self._dbusservice["/ConnectError"] ) = self._socket.getErrorCounter()
            # Run HM state machine after data fetch
            self._hm_state_machine()
            # update status
            self._dbusservice["/UpdateCount"] = _incLimitCnt(self._dbusservice["/UpdateCount"])
            if self._meter_data:
                self._dbusservice["/Dc/0/Voltage"] = self._meter_data["DC"]["0"]["Voltage"]["v"]
                self._dbusservice["/Dc/0/Current"] = self._meter_data["DC"]["0"]["Current"]["v"]
                self._dbusservice["/Dc/0/Temperature"] = self._meter_data["INV"]["0"]["Temperature"]["v"]
                # use /Dc/1/Voltage showed in details as control loop set value
                # self._dbusservice["/Dc/1/Voltage"] = power
                self._dbusservice["/History/EnergyIn"] = self._meter_data["AC"]["0"]["YieldTotal"]["v"]
                self._dbusservice["/Dc/0/Power"] = self._meter_data["AC"]["0"]["Power"]["v"]

        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)

        # return true, otherwise add_timeout will be removed from GObject - see docs
        return True

