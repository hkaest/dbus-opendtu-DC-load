
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


# Singleton instance for DtuSocket, use this to acces DtuSocket
DTUinstance = None
def GetSingleton():
    global DTUinstance
    if DTUinstance is None:
        DTUinstance = DtuSocket()
    return DTUinstance    

# DTU Socket class using a session to communicate with the DTU, http get meter data once for all inverters and http put individually 
class DtuSocket:

    def __init__(self):
        self._session = None
        self._meter_data = None
        self.host = None
        self.username = None
        self.password = None
        self.httptimeout = None
        self.ConnectError = 0
        self.ReadError = 0
        self.FetchCounter = 0
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
        try:
            for invData in self._meter_data["inverters"]:
                if bool(invData["reachable"] in (1, '1', True, "True", "TRUE", "true")):
                    return 1  # if at least one inverter is reachable do not reset the device
            url = f"http://{self.host}/api/maintenance/reboot"
            payload = f'data={{"reboot":true}}'
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
            logging.warning(f"HTTP Error at pushNewLimit for inverter "
                f"{pvinverternumber} ({name}): {str(e)}")
        finally:
            return result

    def getErrorCounter(self):
        return (self.FetchCounter, self.ReadError, self.ConnectError)

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

ALARM_OK = 0
ALARM_WARNING = 1
ALARM_ALARM = 2
ALARM_GRID = "Grid Shelly HTTP fault"
ALARM_TEMPERATURE = "Grid Shelly HTTP fault"
ALARM_DTU = "OpenDTU HTTP fault"
ALARM_HM = "OpenDTU HM fault"
ALARM_BALCONY = "Balcony Shelly HTTP fault"
ALARM_BATTERY = "Battery charge current limit"

TEMPERATURE_OFF_OFFSET = 5 #deegre to cool down


def _incLimitCnt(value):
    return (value + 1) % COUNTERLIMIT

def _is_true(val):
    '''helper function to test for different true values'''
    return val in (1, '1', True, "True", "true")


# Class for HM inverter instance as DBUS service
class DCloadRegistry(type):
    '''Run a registry for all PV Inverter'''
    def __iter__(cls):
        return iter(cls._registry)

class OpenDTUService:
    '''Main class to register PV Inverter in DBUS'''
    __metaclass__ = DCloadRegistry
    _registry = []
    _servicename = None
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
        data=None
    ):

        self._registry.append(self)

        self._servicename = servicename
        self._meter_data = data
        self.pvinverternumber = actual_inverter
        self._tempAlarm = False

        # load config data, self.deviceinstance ...
        self._read_config_dtu_self(actual_inverter)

        # Use dummy data
        self.invName = self._meter_data["name"] if data else "DC Consumer & Boost"
        self.invSerial = self._meter_data["serial"] if data else "--"

        # Allow for multiple Instance per process in DBUS
        dbus_conn = (
            dbus.SessionBus()
            if "DBUS_SESSION_BUS_ADDRESS" in os.environ
            else dbus.SystemBus(private=True)
        )

        self._dbusservice = VeDbusService("{}.http_{:03d}".format(servicename, self.deviceinstance), dbus_conn)

        # Create the mandatory objects
        self._dbusservice.add_mandatory_paths(__file__, softwareversion, CONNECTION, self.deviceinstance, PRODUCT_ID, PRODUCTNAME, FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        
        # Counter         
        self._dbusservice.add_path("/UpdateCount", 0)
        self._dbusservice.add_path("/ConnectError", 0)
        self._dbusservice.add_path("/ReadError", 0)
        self._dbusservice.add_path("/FetchCounter", 0)
        self._dbusservice.add_path("/ConnectCounter", 0)

        logging.debug("%s /DeviceInstance = %d", servicename, self.deviceinstance)

        # Custom name setting
        self._dbusservice.add_path("/CustomName", self.invName)
        logging.info(f"Name of Inverters found: {self.invName}")

        # add path values to dbus
        self._paths = paths
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings["initial"],
                gettextcallback=settings["textformat"],
                writeable=True,
                onchangecallback=self._handlechangedvalue,
            )

        # add _update as cyclic call not as fast as setToZeroPower is called
        if data:
            gobject.timeout_add_seconds((5 if not self.DTU_statusTime else int(self.DTU_statusTime)), self._update)
        else:
            self.setPower(0, 0, 0, 0)

    # public functions
    def setPower(self, volt, ampere, power, temp):
        self._dbusservice["/Dc/0/Voltage"] = volt
        self._dbusservice["/Dc/0/Current"] = ampere
        self._dbusservice["/Dc/0/Power"] = power
        self._dbusservice["/Dc/0/Temperature"] = temp
   
    # public functions
    def setAlarm(self, alarm: str, on: bool):
        self._dbusservice[self._alarm_mapping[alarm]] = ALARM_ALARM if on else ALARM_OK
   
    # public functions, load meter data and return current current
    def updateMeterData(self):
        self._meter_data = GetSingleton().getLimitData(self.pvinverternumber)
        # Copy current error counter to DBU values
        ( self._dbusservice["/FetchCounter"],
          self._dbusservice["/ReadError"],
          self._dbusservice["/ConnectError"] ) = GetSingleton().getErrorCounter()
        return self._meter_data["DC"]["0"]["Current"]["v"] #"Current":{"v":6.070000172,"u":"A","d":2}

    def setToZeroPower(self, gridPower, maxFeedIn):
        addFeedIn = 0
        actFeedIn = 0
        logging.info(f"START: setToZeroPower, grid = {gridPower}, maxFeedIn = {maxFeedIn}, {self.invName}")
        root_meter_data = self._meter_data
        hmConnected = bool(root_meter_data["reachable"] in (1, '1', True, "True", "TRUE", "true"))
        gridConnected = bool(int(root_meter_data["AC"]["0"]["Voltage"]["v"]) > 100)
        hmProducing = bool(root_meter_data["producing"] in (1, '1', True, "True", "TRUE", "true"))
        if hmProducing:
            self._dbusservice["/ConnectCounter"] = 0  # use for falling edge
        elif not gridConnected:
            self._dbusservice["/ConnectCounter"] = 0  # use for rising edge
        elif self._dbusservice["/ConnectCounter"] < PRODUCE_COUNTER:
            self._dbusservice["/ConnectCounter"] = _incLimitCnt(self._dbusservice["/ConnectCounter"])
            hmProducing = True                        # assume true after state change for PRODUCE_COUNTER times
        oldLimitPercent = int(root_meter_data["limit_relative"])
        maxPower = int((int(root_meter_data["limit_absolute"]) * 100) / oldLimitPercent) if oldLimitPercent else 0
        # check if temperature is lower than xx degree and inverter is coinnected to grid (power is always != 0 when connected)
        actTemp = int(root_meter_data["INV"]["0"]["Temperature"]["v"])
        if actTemp > self.maxTemperature and gridPower > 0:
            self._tempAlarm = True
        elif actTemp < (self.maxTemperature - TEMPERATURE_OFF_OFFSET):
             self._tempAlarm = False
        self.setAlarm(ALARM_HM, (not hmConnected or (gridConnected and not hmProducing)))
        self.setAlarm(ALARM_TEMPERATURE, self._tempAlarm)
        if self._tempAlarm:
            logging.info(f"RESULT: setToZeroPower, temperature to high = {actTemp}")
        elif not hmConnected:
            logging.info("RESULT: setToZeroPower, not conneceted to DTU")
            result = GetSingleton().resetDTU()
        elif not gridConnected:
            logging.info("RESULT: setToZeroPower, not conneceted to grid")
        elif not hmProducing:
            logging.info("RESULT: setToZeroPower, conneceted to DTU / Grid, but not producing")
            result = GetSingleton().resetDevice(self.pvinverternumber)
        # calculate new limit
        if maxPower > 0: # and limitStatus in ('Ok', 'OK'):
            # check allowedFeedIn with active feed in
            actFeedIn = int(oldLimitPercent * maxPower / 100)
            allowedFeedIn = maxFeedIn - actFeedIn
            addFeedIn = gridPower
            if addFeedIn > allowedFeedIn:
                addFeedIn = allowedFeedIn
                
            newLimitPercent = int(int((oldLimitPercent + (addFeedIn * 100 / maxPower)) / self.stepsPercent) * self.stepsPercent)
            if newLimitPercent < self.MinPercent:
                newLimitPercent = self.MinPercent
            if newLimitPercent > self.MaxPercent:
                newLimitPercent = self.MaxPercent
            if not gridConnected or not hmConnected or self._tempAlarm or not hmProducing:
                newLimitPercent = self.MinPercent
            if abs(newLimitPercent - oldLimitPercent) > 0:
                result = GetSingleton().pushNewLimit(self.pvinverternumber, newLimitPercent)
                self.setAlarm(ALARM_DTU, (not result))

            # return reduced gridPower values
            addFeedIn = int((newLimitPercent - oldLimitPercent) * maxPower / 100)
            logging.info(f"RESULT: setToZeroPower, result = {addFeedIn}")
            # set DBUS power to new set value
            actFeedIn = int(newLimitPercent * maxPower / 100)
            # use /Dc/1/Voltage showed in details as control loop AC power set value
            self._dbusservice["/Dc/1/Voltage"] = actFeedIn
        return [int(gridPower - addFeedIn),int(maxFeedIn - actFeedIn)]
    
    # read config file
    def _read_config_dtu_self(self, actual_inverter):
        config = configparser.ConfigParser()
        config.read(f"{(os.path.dirname(os.path.realpath(__file__)))}/config.ini")
        self.deviceinstance = int(config[f"INVERTER{self.pvinverternumber}"]["DeviceInstance"])
        self.DTU_statusTime = config["DEFAULT"]["DTU_statusTime"]
        self.MinPercent = int(config["DEFAULT"]["MinPercent"])
        self.MaxPercent = int(config["DEFAULT"]["MaxPercent"])
        self.stepsPercent = int(config["DEFAULT"]["stepsPercent"])
        self.maxTemperature = int(config["DEFAULT"]["maxTemperature"])

    # slower update loop, a update triggers the DBUS-Monitor from com.victronenergy.system
    #  /Control/SolarChargeCurrent  -> 0: no limiting, 1: solar charger limited by user setting or intelligent battery
    #  /Dc/System/MeasurementType should be 1 (calculated by dcsystems)
    #  /Dc/System/Power should be equal to the sum of self._dbusservice["/Dc/0/Power"]
    def _update(self):
        self._dbusservice["/UpdateCount"] = _incLimitCnt(self._dbusservice["/UpdateCount"])
        self._dbusservice["/Dc/0/Voltage"] = self._meter_data["DC"]["0"]["Voltage"]["v"]
        self._dbusservice["/Dc/0/Current"] = self._meter_data["DC"]["0"]["Current"]["v"]
        self._dbusservice["/Dc/0/Temperature"] = self._meter_data["INV"]["0"]["Temperature"]["v"]
        # use /Dc/1/Voltage showed in details as control loop set value
        # self._dbusservice["/Dc/1/Voltage"] = power
        self._dbusservice["/History/EnergyIn"] = self._meter_data["AC"]["0"]["YieldTotal"]["v"]
        self._dbusservice["/Dc/0/Power"] = self._meter_data["AC"]["0"]["Power"]["v"]

        # return true, otherwise add_timeout will be removed from GObject - see docs
        return True

    # https://github.com/victronenergy/velib_python/blob/master/dbusdummyservice.py#L63
    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True # accept the change
