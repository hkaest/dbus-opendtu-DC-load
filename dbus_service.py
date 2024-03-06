'''DbusService and DCloadRegistry'''

# File specific rules
# pylint: disable=E0401,C0411,C0413,broad-except

# system imports:
import configparser
import os
import sys
import logging
import time
import requests  # for http GET
from requests.auth import HTTPBasicAuth

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


class DCloadRegistry(type):
    '''Run a registry for all PV Inverter'''
    def __iter__(cls):
        return iter(cls._registry)


class DbusService:
    '''Main class to register PV Inverter in DBUS'''
    __metaclass__ = DCloadRegistry
    _registry = []
    _meter_data = None
    _servicename = None

    def __init__(
        self,
        servicename,
        paths,
        actual_inverter,
    ):

        self._registry.append(self)

        self._last_update = 0
        self._servicename = servicename
        self.last_update_successful = False

        # Initiale own properties
        self.meter_data = None

        # load config data, self.deviceinstance ...
        self._read_config_dtu(actual_inverter)
        
        # first fetch of DTU data
        self._get_data()
        self.invName = self._get_name()
        self.invSerial = self._get_serial()

        logging.debug("%s /DeviceInstance = %d", servicename, self.deviceinstance)

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

        # Custom name setting
        self._dbusservice.add_path("/CustomName", self.invName)
        logging.info(f"Name of Inverters found: {self.invName}")

        # Counter         
        self._dbusservice.add_path("/UpdateCount", 0)

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

    def getLimitData(self):
        #url = f"http://{self.host}/api/limit/status"
        #limit_data = self.fetch_url(url)
        self._refresh_data()
        return self.is_data_up2date()
    
    def setToZeroPower(self, gridPower, maxFeedIn):
        addFeedIn = 0
        actFeedIn = 0
        logging.info(f"START: setToZeroPower, grid = {gridPower}, maxFeedIn = {maxFeedIn}, {self.invName}")
        try:
            meter_data = self._get_data()
            root_meter_data = meter_data["inverters"][self.pvinverternumber]
            oldLimitPercent = int(root_meter_data["limit_relative"])
            maxPower = int((int(root_meter_data["limit_absolute"]) * 100) / oldLimitPercent)
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
                    url = f"http://{self.host}/api/limit/config"
                    payload = f'data={{"serial":"{self.invSerial}", "limit_type":1, "limit_value":{newLimitPercent}}}'
                    if self.username and self.password:
                        response = requests.post(
                            url = url, 
                            data = payload,
                            auth = HTTPBasicAuth(self.username, self.password),
                            headers = {'Content-Type': 'application/x-www-form-urlencoded'}, 
                            timeout=float(self.httptimeout)
                        )
                    else:
                        response = requests.post(url=url, data=payload, timeout=float(self.httptimeout))
                    logging.info(f"RESULT: setToZeroPower, response = {response}")

                # return reduced gridPower values
                addFeedIn = int((newLimitPercent - oldLimitPercent) * maxPower / 100)
                logging.info(f"RESULT: setToZeroPower, result = {addFeedIn}")
                # set DBUS power to new set value
                actFeedIn = int(newLimitPercent * maxPower / 100)
                # use /Dc/1/Voltage showed in details as control loop AC power set value
                self._dbusservice["/Dc/1/Voltage"] = actFeedIn
        except Exception as genExc:
            logging.warning(f"HTTP Error at setToZeroPower for inverter "
                            f"{self.pvinverternumber} ({self._get_name()}): {str(genExc)}")
        return [int(gridPower - addFeedIn),int(maxFeedIn - actFeedIn)]
    
    @staticmethod
    def _handlechangedvalue(path, value):
        logging.debug("someone else updated %s to %s", path, value)
        return True  # accept the change

    # read config file
    def _read_config_dtu(self, actual_inverter):
        config = configparser.ConfigParser()
        config.read(f"{(os.path.dirname(os.path.realpath(__file__)))}/config.ini")
        self.pvinverternumber = actual_inverter
        self.deviceinstance = int(config[f"INVERTER{self.pvinverternumber}"]["DeviceInstance"])
        self.DTU_statusTime = config["DEFAULT"]["DTU_statusTime"]
        self.host = config["DEFAULT"]["Host"]
        self.username = config["DEFAULT"]["Username"]
        self.password = config["DEFAULT"]["Password"]
        self.dry_run = self.is_true(config["DEFAULT"]["DryRun"])
        self.httptimeout = config["DEFAULT"]["HTTPTimeout"]
        self.MinPercent = int(config["DEFAULT"]["MinPercent"])
        self.MaxPercent = int(config["DEFAULT"]["MaxPercent"])
        self.stepsPercent = int(config["DEFAULT"]["stepsPercent"])
        self.maxTemperature = int(config["DEFAULT"]["maxTemperature"])

    def _get_name(self):
        meter_data = self._get_data()
        name = meter_data["inverters"][self.pvinverternumber]["name"]
        return name

    def _get_serial(self):
        meter_data = self._get_data()
        name = meter_data["inverters"][self.pvinverternumber]["serial"]
        return name

    def get_number_of_inverters(self):
        '''return number of inverters in JSON response'''
        meter_data = self._get_data()
        numberofinverters = len(meter_data["inverters"])
        logging.info("Number of Inverters found: %s", numberofinverters)
        return numberofinverters

    def _refresh_data(self):
        '''Fetch new data from the DTU API and store in locally if successful.'''
        if self.pvinverternumber != 0:
            # only fetch new data when called for inverter 0
            # (background: data is kept at class level for all inverters)
            return
        meter_data["inverters"][self.pvinverternumber]["reachable"] = False
        url = f"http://{self.host}/api/livedata/status"
        meter_data = self.fetch_url(url)
        if meter_data:
            try:
                self.check_opendtu_data(meter_data)
                #Store meter data for later use in other methods
                DbusService._meter_data = meter_data
            except Exception as e:
                logging.critical('Error at %s', 'fetch_url', exc_info=e)
        
    def check_opendtu_data(self, meter_data):
        ''' Check if OpenDTU data has the right format'''
        # Check for OpenDTU Version
        if not "AC" in meter_data["inverters"][self.pvinverternumber]:
            raise ValueError("You do not have the latest OpenDTU Version to run this script,"
                             "please upgrade your OpenDTU to at least version 4.4.3")
        # Check for Attribute (inverter)
        if (self._servicename == "com.victronenergy.dcload" and
                not "DC" in meter_data["inverters"][self.pvinverternumber]):
            raise ValueError("Response from OpenDTU does not contain DC data")
        # Check for another Attribute
        if not "Voltage" in meter_data["inverters"][self.pvinverternumber]["AC"]["0"]:
            raise ValueError("Response from OpenDTU does not contain Voltage data")

    # @timeit
    def fetch_url(self, url):
        '''Fetch JSON data from url. Throw an exception on any error. Only return on success.'''
        json = None
        try:
            logging.debug(f"calling {url} with timeout={self.httptimeout}")
            if self.username and self.password:
                logging.debug("using Basic access authentication...")
                json_str = requests.get(url=url, auth=(
                    self.username, self.password), timeout=float(self.httptimeout))
            else:
                json_str = requests.get(
                    url=url, timeout=float(self.httptimeout))
            json_str.raise_for_status()  # raise exception on bad status code

            # check for response
            if not json_str:
                logging.info("No Response from DTU")
                raise ConnectionError("No response from DTU - ", self.host)

            try:
                json = json_str.json()
            except json.decoder.JSONDecodeError as error:
                logging.debug(f"JSONDecodeError: {str(error)}")

            # check for Json
            if not json:
                # will be logged when catched
                raise ValueError(f"Converting response from {url} to JSON failed: "
                                 f"status={json_str.status_code},\nresponse={json_str.text}")
        except Exception as e:
            logging.critical('Error at %s', 'fetch_url', exc_info=e)
        finally:
            return json

    @staticmethod
    def is_true(val):
        '''helper function to test for different true values'''
        return val in (1, '1', True, "True", "true")
    
    def _get_data(self):
        if not DbusService._meter_data:
            self._refresh_data()
        return DbusService._meter_data

    def is_data_up2date(self):
        '''check if data is up to date with timestamp and producing inverter'''
        meter_data = self._get_data()
        return self.is_true(meter_data["inverters"][self.pvinverternumber]["reachable"])

    def get_ts_last_success(self, meter_data):
        '''return ts_last_success from the meter_data structure - depending on the API version'''
        return meter_data["inverter"][self.pvinverternumber]["ts_last_success"]

    def _update(self):
        successful = False
        try:
            if self.is_data_up2date():
                self.set_dbus_values()
            self._update_index()
            successful = True
        except requests.exceptions.RequestException as exception:
            if self.last_update_successful:
                logging.warning(f"HTTP Error at _update for inverter "
                                f"{self.pvinverternumber} ({self._get_name()}): {str(exception)}")
        except ValueError as error:
            if self.last_update_successful:
                logging.warning(f"Error at _update for inverter "
                                f"{self.pvinverternumber} ({self._get_name()}): {str(error)}")
        except Exception as error:  # pylint: disable=broad-except
            if self.last_update_successful:
                logging.warning(f"Error at _update for inverter "
                                f"{self.pvinverternumber} ({self._get_name()})", exc_info=error)
        finally:
            if successful:
                if not self.last_update_successful:
                    logging.warning(
                        f"Recovered inverter {self.pvinverternumber} ({self._get_name()}): "
                        f"Successfully fetched data now: "
                        f"{'NOT (yet?)' if not self.is_data_up2date() else 'Is'} up-to-date"
                    )
                    self.last_update_successful = True
            else:
                self.last_update_successful = False

        # return true, otherwise add_timeout will be removed from GObject - see docs
        return True

    def _update_index(self):
        if self.dry_run:
            return
        # increment UpdateCount - to show as DBUS data that new data is available
        index = self._dbusservice["/UpdateCount"] + 1  # increment index
        if index > 255:  # maximum value of the index
            index = 0  # overflow from 255 to 0
        self._dbusservice["/UpdateCount"] = index
        self._last_update = time.time()

    def set_dbus_values(self):
        '''read data and set dbus values'''
        meter_data = self._get_data()
        (power, totalEnergy, current, voltage, temperature) = (None, None, None, None, None)

        root_meter_data = meter_data["inverters"][self.pvinverternumber]
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
