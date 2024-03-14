'''OpenDTUService and DCloadRegistry'''

# File specific rules
# pylint: disable=E0401,C0411,C0413,broad-except

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


VERSION = '1.0'
ASECOND = 1000  # second
PRODUCTNAME = "OpenDTU"
CONNECTION = "TCP/IP (HTTP)"
COUNTERLIMIT = 255

ALARM_OK = 0
ALARM_WARNING = 1
ALARM_ALARM = 2
ALARM_GRID = "Grid Shelly HTTP fault"
ALARM_DTU = "OpenDTU HTTP fault"
ALARM_BALCONY = "Balcony Shelly HTTP fault"
ALARM_BATTERY = "Battery charge current limit"


def _incLimitCnt(value):
    return (value + 1) % COUNTERLIMIT

def _is_true(val):
    '''helper function to test for different true values'''
    return val in (1, '1', True, "True", "true")

# Singleton instance
DTUinstance = None
def GetSingleton():
    if DTUinstance is None:
        DTUinstance = DtuSocket()
    return DTUinstance    

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
            meter_data = self._meter_data
            ageBeforeRefresh = (meter_data["inverters"][0]["data_age"])
            self._refresh_data()
            meter_data = self._meter_data
            ageAfterRefresh = (meter_data["inverters"][0]["data_age"])
            return (ageBeforeRefresh != ageAfterRefresh)
        else:
            return False
    
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
            logging.info(f"RESULT: setToZeroPower, response = {str(rsp.status_code)}")
            if rsp:
                result = 1
        except Exception as genExc:
            logging.warning(f"HTTP Error at setToZeroPower for inverter "
                f"{pvinverternumber} ({name}): {str(genExc)}")
        finally:
            return result

    def getNumberOfInverters(self):
        '''return number of inverters in JSON response'''
        meter_data = self._meter_data
        numberofinverters = len(meter_data["inverters"])
        logging.info("Number of Inverters found: %s", numberofinverters)
        return numberofinverters
    
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
        #if self._meter_data:
        #    self._meter_data["inverters"][self.pvinverternumber]["reachable"] = False
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
        if not "AC" in meter_data["inverters"][0]:
            raise ValueError("You do not have the latest OpenDTU Version to run this script,"
                             "please upgrade your OpenDTU to at least version 4.4.3")
        # Check for Attribute (inverter)
        if (not "DC" in meter_data["inverters"][0]):
            raise ValueError("Response from OpenDTU does not contain DC data")
        # Check for another Attribute
        if not "Voltage" in meter_data["inverters"][0]["AC"]["0"]:
            raise ValueError("Response from OpenDTU does not contain Voltage data")

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
        except Exception as err:
            logging.critical('Error at %s', '_fetch_url', exc_info=err)
        finally:
            return json


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
        "Unused1":"/Alarms/HighVoltage",
        ALARM_DTU:"/Alarms/LowStarterVoltage",
        "Unused2":"/Alarms/HighStarterVoltage",
        ALARM_BALCONY:"/Alarms/LowTemperature",
        ALARM_BATTERY:"/Alarms/HighTemperature",
    }

    def __init__(
        self,
        servicename,
        paths,
        actual_inverter,
        data
    ):

        self._registry.append(self)

        self._last_update = 0
        self._servicename = servicename
        self.last_update_successful = False
        self._meter_data = data
        self.pvinverternumber = actual_inverter

        # load config data, self.deviceinstance ...
        self._read_config_dtu_self(actual_inverter)

        # Use dummy data
        self.invName = self._meter_data["name"]
        self.invSerial = self._meter_data["serial"]

        # Allow for multiple Instance per process in DBUS
        dbus_conn = (
            dbus.SessionBus()
            if "DBUS_SESSION_BUS_ADDRESS" in os.environ
            else dbus.SystemBus(private=True)
        )

        self._dbusservice = VeDbusService("{}.http_{:03d}".format(servicename, self.deviceinstance), dbus_conn)
        
        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path("/Mgmt/ProcessVersion", VERSION)
        self._dbusservice.add_path("/Mgmt/Connection", CONNECTION)

        # Create the mandatory objects
        self._dbusservice.add_path("/DeviceInstance", self.deviceinstance)
        self._dbusservice.add_path("/ProductId", 0xFFFF)  # id assigned by Victron Support from SDM630v2.py
        self._dbusservice.add_path("/ProductName", PRODUCTNAME)
        self._dbusservice.add_path("/Connected", 1)

        # Counter         
        self._dbusservice.add_path("/UpdateCount", 0)
        self._dbusservice.add_path("/ConnectError", 0)
        self._dbusservice.add_path("/ReadError", 0)
        self._dbusservice.add_path("/FetchCounter", 0)

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
        gobject.timeout_add((10 if not self.DTU_statusTime else int(self.DTU_statusTime)) * ASECOND, self._update)

    # public functions
    def setAlarm(self, alarm: str, on: bool):
        self._dbusservice[self._alarm_mapping[alarm]] = ALARM_ALARM if on else ALARM_OK
   
    def updateMeterData(self):
        self._meter_data = GetSingleton().getLimitData(self.pvinverternumber)
        # Copy current error counter to DBU values
        ( self._dbusservice["/FetchCounter"],
          self._dbusservice["/ReadError"],
          self._dbusservice["/ConnectError"] ) = GetSingleton().getErrorCounter()

    def is_data_up2date(self):
        '''check if data is up to date with timestamp and producing inverter'''
        meter_data = self._meter_data
        return _is_true(meter_data["reachable"])

    def setToZeroPower(self, gridPower, maxFeedIn):
        addFeedIn = 0
        actFeedIn = 0
        logging.info(f"START: setToZeroPower, grid = {gridPower}, maxFeedIn = {maxFeedIn}, {self.invName}")
        root_meter_data = self._meter_data
        oldLimitPercent = int(root_meter_data["limit_relative"])
        maxPower = int((int(root_meter_data["limit_absolute"]) * 100) / oldLimitPercent) if oldLimitPercent else 0
        #limitStatus = limit_data[self.invSerial]["limit_set_status"]
        # check if temperature is lower than xx degree and inverter is coinnected to grid (power is always != 0 when connected)
        actTemp = int(self._dbusservice["/Dc/0/Temperature"]) if self._dbusservice["/Dc/0/Temperature"] else 0
        gridConnected = bool(int(self._dbusservice["/Dc/0/Power"]) > 0) if self._dbusservice["/Dc/0/Power"] else False
        if actTemp > self.maxTemperature and gridPower > 0:
            logging.info(f"RESULT: setToZeroPower, temperature to high = {actTemp}")
        elif not gridConnected:
            logging.info("RESULT: setToZeroPower, not conneceted to grid")
        # calculate new limit
        elif maxPower > 0: # and limitStatus in ('Ok', 'OK'):
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

    def _update(self):
        successful = False
        try:
            if self.is_data_up2date():
                self._set_dbus_values()
                self._dbusservice["/UpdateCount"] = _incLimitCnt(self._dbusservice["/UpdateCount"])
                self._last_update = time.time()
            successful = True
        except Exception as error:  # pylint: disable=broad-except
            if self.last_update_successful:
                logging.warning(f"Error at _update for inverter "
                                f"{self.pvinverternumber} ({self.invName})", exc_info=error)
        finally:
            if successful:
                if not self.last_update_successful:
                    logging.warning(
                        f"Recovered inverter {self.pvinverternumber} ({self.invName}): "
                        f"Successfully fetched data now: "
                        f"{'NOT (yet?)' if not self.is_data_up2date() else 'Is'} up-to-date"
                    )
                    self.last_update_successful = True
            else:
                self.last_update_successful = False

        # return true, otherwise add_timeout will be removed from GObject - see docs
        return True

    def _set_dbus_values(self):
        '''read data and set dbus values'''
        root_meter_data = self._meter_data
        power = root_meter_data["AC"]["0"]["Power"]["v"]
        totalEnergy = root_meter_data["AC"]["0"]["YieldTotal"]["v"]
        voltage = root_meter_data["DC"]["0"]["Voltage"]["v"]
        temperature = root_meter_data["INV"]["0"]["Temperature"]["v"]
        current = root_meter_data["DC"]["0"]["Current"]["v"]

        # This will be refactored later in classes
        # /Dc/0/Voltage              <-- V DC
        # /Dc/0/Current              <-- A, positive when power is consumed by DC loads
        # /Dc/0/Temperature          <-- Degrees centigrade, temperature sensor on SmarShunt/BMV
        # /Dc/1/Voltage              <-- SmartShunt/BMV secondary battery voltage (if configured)
        # /History/EnergyIn          <-- Total energy consumed by dc load(s).
        self._dbusservice["/Dc/0/Voltage"] = voltage
        self._dbusservice["/Dc/0/Current"] = current
        self._dbusservice["/Dc/0/Temperature"] = temperature
        # use /Dc/1/Voltage showed in details as control loop set value
        # self._dbusservice["/Dc/1/Voltage"] = power
        self._dbusservice["/History/EnergyIn"] = totalEnergy
        self._dbusservice["/Dc/0/Power"] = power

        logging.debug(f"Inverter #{self.pvinverternumber} Voltage (/Ac/Out/L1/V): {voltage}")
        logging.debug(f"Inverter #{self.pvinverternumber} Current (/Ac/Out/L1/I): {current}")
        logging.debug("---")

    # https://github.com/victronenergy/velib_python/blob/master/dbusdummyservice.py#L63
    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True # accept the change
