'''DbusService and PvInverterRegistry'''

# File specific rules
# pylint: disable=E0401,C0411,C0413,broad-except

# system imports:
import configparser
import os
import sys
import logging
import time
import requests  # for http GET
from requests.auth import HTTPDigestAuth

# victron imports:
import dbus

from gi.repository import GLib

sys.path.insert(
    1,
    os.path.join(
        os.path.dirname(__file__),
        "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python",
    ),
)
from vedbus import VeDbusService  # noqa - must be placed after the sys.path.insert


VERSION = '0.1'
SAVEINTERVAL = 120000
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
    _config_data = None
    _servicename = None

    def __init__(
        self,
        servicename,
        paths,
        actual_inverter,
        istemplate=False,
    ):

        self._registry.append(self)

        self._last_update = 0
        self._servicename = servicename
        self.last_update_successful = False

        # Initiale own properties
        self.meter_data = None

        self._read_config_dtu(actual_inverter)
        self.numberofinverters = self.get_number_of_inverters()

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
        self._dbusservice.add_path("/CustomName", self._get_name())
        # logging.info(f"Name of Inverters found: {self._get_name()}")

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

        # add _update as cyclic call
        GLib.timeout_add(SAVEINTERVAL, self._update)

        # add _sign_of_life 'timer' to get feedback in log every 5minutes
        GLib.timeout_add(self._get_sign_of_life_interval() * SAVEINTERVAL, self._sign_of_life)

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
        self.signofliveinterval = config["DEFAULT"]["SignOfLifeLog"]
        self.host = config["DEFAULT"]["Host"]
        self.username = config["DEFAULT"]["Username"]
        self.password = config["DEFAULT"]["Password"]
        self.digestauth = False
        self.max_age_ts = int(config["DEFAULT"]["MaxAgeTsLastSuccess"])
        self.dry_run = self.is_true(config["DEFAULT"]["DryRun"])
        self.httptimeout = config["DEFAULT"]["HTTPTimeout"]
        
        # init data
        self.meter_data = 0

    def _get_name(self):
        meter_data = self._get_data()
        name = meter_data["inverters"][self.pvinverternumber]["name"]
        return name

    def get_number_of_inverters(self):
        '''return number of inverters in JSON response'''
        meter_data = self._get_data()
        numberofinverters = len(meter_data["inverters"])
        logging.info("Number of Inverters found: %s", numberofinverters)
        return numberofinverters

    def _get_sign_of_life_interval(self):
        '''Get intervall in seconds how often sign of life logs should be created.'''
        value = self.signofliveinterval
        if not value:
            value = 0
        return int(value)

    def _refresh_data(self):
        '''Fetch new data from the DTU API and store in locally if successful.'''

        if self.pvinverternumber != 0:
            # only fetch new data when called for inverter 0
            # (background: data is kept at class level for all inverters)
            return

        url = f"http://{self.host}/api" + "/livedata/status"
        meter_data = self.fetch_url(url)

        self.check_opendtu_data(meter_data)
        '''Store meter data for later use in other methods'''
        self.meter_data = meter_data
        
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
    def fetch_url(self, url, try_number=1):
        '''Fetch JSON data from url. Throw an exception on any error. Only return on success.'''
        try:
            logging.debug(f"calling {url} with timeout={self.httptimeout}")
            if self.digestauth:
                logging.debug("using Digest access authentication...")
                json_str = requests.get(url=url, auth=HTTPDigestAuth(
                    self.username, self.password), timeout=float(self.httptimeout))
            elif self.username and self.password:
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

            json = None
            try:
                json = json_str.json()
            except json.decoder.JSONDecodeError as error:
                logging.debug(f"JSONDecodeError: {str(error)}")

            # check for Json
            if not json:
                # will be logged when catched
                raise ValueError(f"Converting response from {url} to JSON failed: "
                                 f"status={json_str.status_code},\nresponse={json_str.text}")
            return json
        except Exception:
            # retry same call up to 3 times
            if try_number < 3:  # pylint: disable=no-else-return
                time.sleep(0.5)
                return self.fetch_url(url, try_number + 1)
            else:
                raise

    def is_true(val):
        '''helper function to test for different true values'''
        return val in (1, '1', True, "True", "true")
    
    def _get_data(self):
        if not DbusService._meter_data:
            self._refresh_data()
        return DbusService._meter_data

    def is_data_up2date(self):
        '''check if data is up to date with timestamp and producing inverter'''
        if self.max_age_ts < 0:
            # check is disabled by config
            return True
        meter_data = self._get_data()
        return self.is_true(meter_data["inverters"][self.pvinverternumber]["reachable"])

    def get_ts_last_success(self, meter_data):
        '''return ts_last_success from the meter_data structure - depending on the API version'''
        return meter_data["inverter"][self.pvinverternumber]["ts_last_success"]

    def _sign_of_life(self):
        logging.debug("Last inverter #%d _update() call: %s", self.pvinverternumber, self._last_update)
        logging.info("Last inverter #%d '/Ac/Power': %s", self.pvinverternumber, self._dbusservice["/Ac/Power"])
        return True

    def _update(self):
        successful = False
        try:
            # update data from DTU once per _update call:
            self._refresh_data()

            if self.is_data_up2date():
                if self.dry_run:
                    logging.info("DRY RUN. No data is sent!!")
                else:
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
        # http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
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

    def get_values_for_inverter(self):
        '''read data and return (power, totalEnergy, current, voltage, temperature)'''
        meter_data = self._get_data()
        (power, totalEnergy, current, voltage, temperature) = (None, None, None, None, None)

        root_meter_data = meter_data["inverters"][self.pvinverternumber]
        power = root_meter_data["AC"]["0"]["Power"]["v"]
        totalEnergy = root_meter_data["AC"]["0"]["YieldTotal"]["v"]
        voltage = root_meter_data["DC"]["0"]["Voltage"]["v"]
        temperature = root_meter_data["INV"]["0"]["Temperature"]["v"]
        current = root_meter_data["DC"]["0"]["Current"]["v"]

        return (power, totalEnergy, current, voltage, temperature)

    def set_dbus_values(self):
        '''read data and set dbus values'''
        (power, totalEnergy, current, voltage, temperature) = self.get_values_for_inverter()

        # This will be refactored later in classes
        # /Dc/0/Voltage              <-- V DC
        # /Dc/0/Current              <-- A, positive when power is consumed by DC loads
        # /Dc/0/Temperature          <-- Degrees centigrade, temperature sensor on SmarShunt/BMV
        # /Dc/1/Voltage              <-- SmartShunt/BMV secondary battery voltage (if configured)
        # /History/EnergyIn          <-- Total energy consumed by dc load(s).
        self._dbusservice["/Dc/0/Voltage"] = voltage
        self._dbusservice["/Dc/0/Current"] = current
        self._dbusservice["/Dc/0/Temperature"] = temperature
        self._dbusservice["/History/EnergyIn"] = totalEnergy

        logging.debug(f"Inverter #{self.pvinverternumber} Voltage (/Ac/Out/L1/V): {voltage}")
        logging.debug(f"Inverter #{self.pvinverternumber} Current (/Ac/Out/L1/I): {current}")
        logging.debug("---")
