
# import normal packages
import logging
import sys
import os
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests # for http GET

import configparser # for config/ini file

import dbus

from dbus_service import ALARM_BALCONY 
from dbus_service import ALARM_GRID 
from dbus_service import ALARM_BATTERY 
from dbus_service import OpenDTUService
from dbus_service import GetSingleton
from version import softwareversion


# Victron packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService, VeDbusItemImport
from dbusmonitor import DbusMonitor


PRODUCTNAME = "GRID by Shelly"
CONNECTION = "TCP/IP (HTTP)"
PRODUCT_ID = 0
FIRMWARE_VERSION = 0
HARDWARE_VERSION = 0
CONNECTED = 1

AUXDEFAULT = 500
EXCEPTIONPOWER = -100
BASESOC = 53  # with 6% min SOC -> 94% range -> 53% in the middle
MINMAXSOC = BASESOC + 10  # 20% range per default
COUNTERLIMIT = 255


# you can prefix a function name with an underscore (_) to declare it private. 
def _validate_percent_value(path, newvalue):
    # percentage range
    return newvalue <= 100 and newvalue >= MINMAXSOC
    
def _validate_feedin_value(path, newvalue):
    # percentage range
    return newvalue <= 800 

def _incLimitCnt(value):
    return (value + 1) % COUNTERLIMIT

    
class DbusShellyemService:
    def __init__(
            self, 
            servicename, 
            paths, 
            inverter,
            dbusmon 
        ):
        self._monitor = dbusmon
        config = self._getConfig()
        deviceinstance = int(config['SHELLY']['Deviceinstance'])
        customname = config['SHELLY']['CustomName']
        self._statusURL = self._getShellyStatusUrl()
        self._balconyURL = self._getShellyBalconyUrl()
        self._keepAliveURL = config['SHELLY']['KeepAliveURL']
        self._SwitchOffURL = config['SHELLY']['SwitchOffURL']
        self._ZeroPoint = int(config['DEFAULT']['ZeroPoint'])
        self._MaxFeedIn = int(config['DEFAULT']['MaxFeedIn'])
        self._consumeFilterFactor = int(config['DEFAULT']['consumeFilterFactor'])
        self._feedInFilterFactor = int(config['DEFAULT']['feedInFilterFactor'])
        self._feedInAtNegativeWattDifference = int(config['DEFAULT']['feedInAtNegativeWattDifference'])
        self._Accuracy = int(config['DEFAULT']['ACCURACY'])
        self._DTU_loopTime = int(config['DEFAULT']['DTU_loopTime'])
        self._SignOfLifeLog = config['DEFAULT']['SignOfLifeLog']
        # Shelly EM session
        self._eMsession = requests.Session()
        self._balconySession = requests.Session()
 
        # inverter list
        self._inverter = inverter

        # Allow for multiple Instance per process in DBUS
        dbus_conn = (
            dbus.SessionBus()
            if "DBUS_SESSION_BUS_ADDRESS" in os.environ
            else dbus.SystemBus()
        )
      
        self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance), dbus_conn)

        # Create the mandatory objects
        self._dbusservice.add_mandatory_paths(__file__, softwareversion, CONNECTION, deviceinstance, PRODUCT_ID, PRODUCTNAME, FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        
        self._dbusservice.add_path('/CustomName', customname)    
        self._dbusservice.add_path('/Role', 'acload')

        # counter
        self._dbusservice.add_path('/UpdateIndex', 0)
        self._dbusservice.add_path('/LoopIndex', 0)
        self._dbusservice.add_path('/FeedInIndex', 0)

        # additional values
        self._dbusservice.add_path('/AuxFeedInPower', AUXDEFAULT)
        self._dbusservice.add_path('/Soc', BASESOC)
        self._dbusservice.add_path('/SocChargeCurrent', 0)
        self._dbusservice.add_path('/SocMaxChargeCurrent', 20)
        # self._dbusservice.add_path('/ActualFeedInPower', 0)
        self._dbusservice.add_path('/SocFloatingMax', MINMAXSOC, writeable=True, onchangecallback=_validate_percent_value)
        self._dbusservice.add_path('/SocIncrement', 0)
        self._dbusservice.add_path('/MaxFeedIn', self._MaxFeedIn, writeable=True, onchangecallback=_validate_feedin_value)

        # test custom error 
        self._dbusservice.add_path('/Error', "--")

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        # add path values to dbus
        self._paths = paths
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path, settings['initial'], 
                gettextcallback=settings['textformat'], 
                writeable=True, 
                onchangecallback=self._handlechangedvalue
            )
      
        # power value 
        self._power = int(0)
        self._BalconyPower = int(0)
        self._ChargeLimited = False
        
        # last update
        self._lastUpdate = 0
        
        # add _update timed function, get DTU data and control HMs
        gobject.timeout_add_seconds(self._DTU_loopTime, self._update) 
        
        # add _signOfLife timed function to switch HM relais at Shelly
        gobject.timeout_add_seconds((10 if not self._SignOfLifeLog else int(self._SignOfLifeLog)) * 60, self._signOfLife)
        
        # call _createDbusMonitor after x minutes, since create dbusmonitor disturbs service creation (not all dcsystem are recognized from system)
        gobject.timeout_add_seconds(60, self._createDbusMonitor)

        # Note: The given function is called repeatedly until it returns G_SOURCE_REMOVE or FALSE, at which point the timeout is automatically 
        # destroyed and the function will not be called again. The first call to the function will be at the end of the first interval. 
        #
        # Note that timeout functions may be delayed, due to the processing of other event sources. Thus they should not be relied on for precise timing. 
        

    # public function
    def getPower(self):
        return self._power

    # Periodically function
    def _controlLoop(self):
        try:
            # pass grid meter value and allowed feed in to first DTU inverter
            logging.info("START: Control Loop is running")
            # trigger read data once from DTU
            limitData = GetSingleton().fetchLimitData()
            if not limitData:
                logging.info("LIMIT DATA: Failed")
            else:
                number = 0
                # trigger inverter to fetch meter data from singleton
                while number < len(self._inverter):
                    self._inverter[number].updateMeterData()                    
                    number = number + 1
                # loop
                POWER = 0
                FEEDIN = 1
                gridValue = [int(int(self._power) - self._ZeroPoint),int(self._dbusservice['/MaxFeedIn'] - self._BalconyPower)]
                logging.info(f"PRESET: Control Loop {gridValue[POWER]}, {gridValue[FEEDIN]} ")
                number = 0
                # use loop counter to swap with slow _SignOfLifeLog cycle
                swap = bool(self._dbusservice['/LoopIndex'] == 0)
                # around zero point do nothing 
                while abs(gridValue[POWER]) > self._Accuracy and number < len(self._inverter):
                    # Do not swap when set values are changed
                    swap = False
                    inPower = gridValue[POWER]
                    gridValue = self._inverter[number].setToZeroPower(gridValue[POWER], gridValue[FEEDIN])
                    # multiple inverter, set new limit only once in a loop
                    if inPower != gridValue[POWER]:
                        # adapt stored power value to value reduced by micro inverter  
                        self._power = gridValue[POWER] + self._ZeroPoint
                        logging.info(f"CHANGED and Break: Control Loop {gridValue[POWER]}, {gridValue[FEEDIN]} ")
                        break
                    # switch to next inverter if inverter is at limit (no change so far)
                    number = number + 1
                
                if swap:
                    # swap inverters to avoid using mainly the first ones
                    logging.info(f"UNCHANGED and Continue: Control Loop {gridValue[POWER]}, {gridValue[FEEDIN]} ")
                    position = 0
                    while position < (len(self._inverter) - 1):
                        self._inverter[position], self._inverter[position + 1] = self._inverter[position + 1], self._inverter[position]
                        position = position + 1

                logging.info("END: Control Loop is running")
                # increment or reset FeedInIndex
                if self._power < -(self._feedInAtNegativeWattDifference):
                    index = self._dbusservice['/FeedInIndex'] + 1  # increment index
                    if index < 255:   # maximum value of the index
                        self._dbusservice['/FeedInIndex'] = index
                else:
                    self._dbusservice['/FeedInIndex'] = 0
                # increment LoopIndex - to show that loop is running
                self._dbusservice['/LoopIndex'] += 1  # increment index
            
            # read SOC
            if self._monitor:
                newSoc = int(self._monitor.get_value('com.victronenergy.battery.socketcan_can0', '/Soc', MINMAXSOC))
                current = float(self._monitor.get_value('com.victronenergy.battery.socketcan_can0', '/Dc/0/Current', MINMAXSOC))
                maxCurrent = float(self._monitor.get_value('com.victronenergy.battery.socketcan_can0', '/Info/MaxChargeCurrent', MINMAXSOC))
                # two point control, to avoid volatile signal changes (assumed zero point 25W * 2 = 50VA / 58V ~ 1A) 
                self._ChargeLimited = bool((maxCurrent - current) < 1.2) if self._ChargeLimited else bool((maxCurrent - current) < 0.2) 
                #int(self._SOC.get_value())
                oldSoc = self._dbusservice['/Soc']
                incSoc = newSoc - oldSoc
                if incSoc != 0:
                    # direction change + * - = -
                    if (incSoc * self._dbusservice['/SocIncrement']) < 0:
                        if self._dbusservice['/SocIncrement'] > 0:
                            if oldSoc <= 100 and oldSoc > self._dbusservice['/SocFloatingMax']:
                                # increase max faster to allow minSOC to be decreased with range/2 directly to achieve min to be decreased immediately
                                self._dbusservice['/SocFloatingMax'] += 2 
                            if (oldSoc >= MINMAXSOC or self._dbusservice['/SocFloatingMax'] > MINMAXSOC) and oldSoc < self._dbusservice['/SocFloatingMax']:
                                # decrease until MINMAXSOC is reached
                                self._dbusservice['/SocFloatingMax'] -= 1 
                    self._dbusservice['/SocIncrement'] = incSoc
                    self._dbusservice['/Soc'] = newSoc
                # publish data to DBUS as debug data
                self._dbusservice['/SocChargeCurrent'] = current
                self._dbusservice['/SocMaxChargeCurrent'] = maxCurrent
            else:
                self._dbusservice['/SocFloatingMax'] = MINMAXSOC
            
        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)
           
        # return true, otherwise add_timeout will be removed from GObject - 
        return True
       
    def _createDbusMonitor(self):
        dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
        self._monitor = DbusMonitor({
            # do not scan 'com.victronenergy.acload' since we are a acload too. This will cause trouble at the DBUS-Monitor from com.victronenergy.system
            # com.victronenergy.battery.socketcan_can0
            #  /Soc                        <- 0 to 100 % (BMV, BYD, Lynx BMS)
            #  /Info/MaxChargeCurrent      <- Charge Current Limit aka CCL  
            #  /Info/MaxDischargeCurrent   <- Discharge Current Limit aka DCL 
            #  /Info/MaxChargeVoltage      <- Maximum voltage to charge to
            #  /Info/BatteryLowVoltage     <- Note that Low Voltage is ignored by the system
            #  /Info/ChargeRequest         <- Battery is extremely low and needs to be charged
            #  /Dc/0/Voltage               <- V DC
            #  /Dc/0/Current               <- A DC positive when charged, negative when discharged
            #  /Dc/0/Power                 <- W positive when charged, negative when discharged
            #  /Dc/0/Temperature           <- °C Battery temperature 
            'com.victronenergy.battery': {
                '/Soc': dummy,
                '/Dc/0/Current': dummy,
                '/Info/MaxChargeCurrent': dummy,
            }
        })
        # return true, otherwise add_timeout will be removed from GObject - 
        return False

 
    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
        return config;
 
 
    def _getShellyStatusUrl(self):
        config = self._getConfig()
        accessType = config['SHELLY']['AccessType']
        if accessType == 'OnPremise': 
            URL = "http://%s:%s@%s/status" % (config['SHELLY']['Username'], config['SHELLY']['Password'], config['SHELLY']['Host'])
            URL = URL.replace(":@", "")
        else:
            raise ValueError("AccessType %s is not supported" % (config['SHELLY']['AccessType']))
        return URL

 
    def _getShellyBalconyUrl(self):
        config = self._getConfig()
        # accessType = config['SHELLY']['AccessType']
        #if accessType == 'OnPremise': 
        #    URL = "http://%s:%s@%s/status" % (config['SHELLY']['Username'], config['SHELLY']['Password'], config['SHELLY']['Host'])
        #    URL = URL.replace(":@", "")
        #else:
        #    raise ValueError("AccessType %s is not supported" % (config['SHELLY']['AccessType']))
        URL = "http://%s/status" % (config['SHELLY']['Balcony'])
        return URL
   
    def _fetch_url(self, URL, alarm, session):
        json = None
        try:
            logging.debug(f"calling {URL}")
            rsp = session.get(url=URL)
            rsp.raise_for_status() #HTTPError for status code >=400
            logging.info(f"_fetch_url response status code: {str(rsp.status_code)}")
            json = rsp.json()
        except requests.HTTPError as http_err:
            logging.info(f"_fetch_url response http error: {http_err}")
            self._dbusservice['/Error'] =f"{alarm} / {http_err}"
        except requests.ConnectTimeout as e:
            # Requests that produced this error are safe to retry.
            self._dbusservice['/Error'] =f"{alarm} / Connect Timeout"
        except requests.ReadTimeout as e:
            self._dbusservice['/Error'] =f"{alarm} / Read Timeout"
        except Exception as err:
            logging.critical('Error at %s', '_fetch_url', exc_info=err)
            self._dbusservice['/Error'] =f"{alarm} / Critical Exception"
        finally:
            self._inverter[0].setAlarm(alarm, bool(not json))
            return json
 
    def _signOfLife(self):
        try:
            logging.info(" --- Check for min SOC and switch relais --- ")
            # calculate min SOC based on max SOC and BASESOC. If max SOC increases lower min SOC and vice versa
            # min is addiotinal secured with an voltage guard relais and theoretically with the BMS of the battery
            minSoc = BASESOC - (self._dbusservice['/SocFloatingMax'] - BASESOC)
            # send relay On request to conected Shelly to keep micro inverters connected to grid 
            if self._dbusservice['/LoopIndex'] > 0 and self._dbusservice['/Soc'] >= minSoc:
                if bool(self._dbusservice['/FeedInIndex'] < 50):
                    self._inverterSwitch( True )
                    logging.info(" ---           switch relais ON          --- ")
                else:
                    self._inverterSwitch( False )
                    logging.info(" ---   Permanent negative grid --> OFF   --- ")
            else:
                logging.info(" ---  Configured min SOC reached --> OFF --- ")
            # reset 
            self._dbusservice['/LoopIndex'] = 0
        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)
           
        # return true, otherwise add_timeout will be removed from GObject - 
        return True

    def _inverterSwitch(self, on):
        # send relay On request to conected Shelly to keep micro inverters connected to grid 
        if on and self._keepAliveURL:
            try:
                response = requests.get(url = self._keepAliveURL)
                logging.info(f"RESULT: keep relay alive at shelly, response status code = {str(response.status_code)}")
                response.close()
            except Exception as genExc:
                logging.warning(f"HTTP Error at keepAliveURL for inverter: {str(genExc)}")
        if not on and self._SwitchOffURL:
            try:
                response = requests.get(url = self._SwitchOffURL)
                logging.info(f"RESULT: SwitchOffURL, response status code = {str(response.status_code)}")
                response.close()
            except Exception as genExc:
                logging.warning(f"HTTP Error at SwitchOffURL for inverter: {str(genExc)}")
    
    def _update(self):   

        # get feed in from balcony
        balcony_data = self._fetch_url(self._balconyURL, ALARM_BALCONY, self._balconySession)
        self._BalconyPower = balcony_data['emeters'][0]['power'] if balcony_data else AUXDEFAULT # assume AUXDEFAULT watt to reduce allowed feed in
        # publish balcony power
        self._dbusservice['/AuxFeedInPower'] = self._BalconyPower

        # get data from Shelly em (grid)
        meter_data = self._fetch_url(self._statusURL, ALARM_GRID, self._eMsession)
        if meter_data:
            # send data to DBus
            current = meter_data['emeters'][0]['power'] / meter_data['emeters'][0]['voltage']
            self._dbusservice['/Ac/L1/Voltage'] = meter_data['emeters'][0]['voltage']
            self._dbusservice['/Ac/L1/Current'] = current
            self._dbusservice['/Ac/L1/Power'] = meter_data['emeters'][0]['power']
            self._dbusservice['/Ac/L1/Energy/Forward'] = (meter_data['emeters'][0]['total']/1000)
            # self._dbusservice['/Ac/L1/Energy/Reverse'] = (meter_data['emeters'][0]['total_returned']/1000)    
            # don't forget the global values  
            self._dbusservice['/Ac/Current'] = current
            self._dbusservice['/Ac/Power'] = meter_data['emeters'][0]['power']
            self._dbusservice['/Ac/Voltage'] = meter_data['emeters'][0]['voltage']
            self._dbusservice['/Ac/Energy/Forward'] = self._dbusservice['/Ac/L1/Energy/Forward']
            # self._dbusservice['/Ac/Energy/Reverse'] = self._dbusservice['/Ac/L1/Energy/Reverse'] 
       
            # update power value with a average sum, dependens on feedInAtNegativeWattDifference or on real feed in 
            if self._ChargeLimited:
                # if CCL at battery is reached put zero point to negative side
                self._power = (
                    int(((self._power * self._feedInFilterFactor) + meter_data['emeters'][0]['power'] + self._ZeroPoint * 2) / (self._feedInFilterFactor + 1))
                )
            elif meter_data['emeters'][0]['power'] < -(self._Accuracy) :
                self._power = (
                    int(((self._power * self._feedInFilterFactor) + meter_data['emeters'][0]['power']) / (self._feedInFilterFactor + 1))
                )
            elif (self._power - meter_data['emeters'][0]['power']) > self._feedInAtNegativeWattDifference:
                self._power = (
                    int(((self._power * self._feedInFilterFactor) + meter_data['emeters'][0]['power']) / (self._feedInFilterFactor + 1))
                )
            else:
                self._power = (
                    int(((self._power * self._consumeFilterFactor) + meter_data['emeters'][0]['power']) / (self._consumeFilterFactor + 1))
                )

            # increment UpdateIndex - to show that new data is available
            self._dbusservice['/UpdateIndex'] = _incLimitCnt(self._dbusservice['/UpdateIndex'])
       
            # update lastupdate vars
            self._lastUpdate = time.time()              
        else:
            self._power = EXCEPTIONPOWER   # assume feed in to reduce feed in by micro inverter
            
        # run control loop after grid values have been updated
        self._controlLoop()
           
        # return true, otherwise add_timeout will be removed from GObject - 
        # see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True
 
    # https://github.com/victronenergy/velib_python/blob/master/dbusdummyservice.py#L63
    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True # accept the change

