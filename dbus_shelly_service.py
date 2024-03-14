'''DbusShellyemService'''
 
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
from dbusmonitor import DbusMonitor


# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))

from vedbus import VeDbusService, VeDbusItemImport

VERSION = '1.0'
ASECOND = 1000  # second
PRODUCTNAME = "GRID by Shelly"
CONNECTION = "TCP/IP (HTTP)"
AUXDEFAULT = 500
EXCEPTIONPOWER = -100
BASESOC = 53  # with 6% min SOC -> 94% range -> 53% in the middle
MINMAXSOC = BASESOC + 10  # 20% range per default
COUNTERLIMIT = 255

# com.victronenergy.solarcharger.ttyS1 & ttyS2 
# ---------------------------------------------------------------------------
# External control:
# /Link/NetworkMode    <- Bitmask
#                         0x1 = External control
#                         0x4 = External voltage/current control
#                         0x8 = Controled by BMS (causes Error #67, BMS lost, if external control is interrupted).
# /Link/BatteryCurrent <- When SCS is enabled on the GX device, the battery current is written here to improve tail-current detection.
# /Link/ChargeCurrent  <- Maximum charge current. Must be written every 60 seconds. Used by GX device if there is a BMS or user limit.
# /Link/ChargeVoltage  <- Charge voltage. Must be written every 60 seconds. Used by GX device to communicate BMS charge voltages.
# /Link/NetworkStatus  <- Bitmask
#                         0x01 = Slave
#                         0x02 = Master
#                         0x04 = Standalone
#                         0x20 = Using I-sense (/Link/BatteryCurrent)
#                         0x40 = Using T-sense (/Link/TemperatureSense)
#                         0x80 = Using V-sense (/Link/VoltageSense)
# Settings:
# /Settings/BmsPresent         <- BMS in the system. External control is expected. This happens automatically if NetworkMode is set to expect a BMS.
# /Settings/ChargeCurrentLimit <- The maximum configured (non-volatile) charge current. This is the same as set by VictronConnect.
# Other paths:
# /Dc/0/Voltage     <- Actual battery voltage
# /Dc/0/Current     <- Actual charging current
# /Yield/User       <- Total kWh produced (user resettable)
# /Yield/System     <- Total kWh produced (not resettable)
# /Load/State       <- Whether the load is on or off
# /Load/I           <- Current from the load output
# /ErrorCode        <- 0=No error
#                     1=Battery temperature too high
#                     2=Battery voltage too high
#                     3=Battery temperature sensor miswired (+)
#                     4=Battery temperature sensor miswired (-)
#                     5=Battery temperature sensor disconnected
#                     6=Battery voltage sense miswired (+)
#                     7=Battery voltage sense miswired (-)
#                     8=Battery voltage sense disconnected
#                     9=Battery voltage wire losses too high
#                     17=Charger temperature too high
#                     18=Charger over-current
#                     19=Charger current polarity reversed
#                     20=Bulk time limit reached
#                     22=Charger temperature sensor miswired
#                     23=Charger temperature sensor disconnected
#                     34=Input current too high
#                     https://www.victronenergy.com/live/mppt-error-codes
# /State            <- 0=Off
#                     2=Fault
#                     3=Bulk
#                     4=Absorption
#                     5=Float
#                     6=Storage
#                     7=Equalize
#                     252=External control
# /History/*        <- Contains values about the last month's history
#                     (Only for VE.Direct solarchargers)
# /Mode             <- 1=On; 4=Off, Writeable for both VE.Direct & VE.Can solar chargers
# /DeviceOffReason  <- Bitmask indicating the reason(s) that the MPPT is in Off State
#                     0x01 = No/Low input power
#                     0x02 = Disabled by physical switch
#                     0x04 = Remote via Device-mode or push-button
#                     0x08 = Remote input connector
#                     0x10 = Internal condition preventing startup
#                     0x20 = Need token for operation
#                     0x40 = Signal from BMS
#                     0x80 = Engine shutdown on low input voltage
#                     0x100 = Converter is off to read input voltage accurately
#                     0x200 = Low temperature
#                     0x400 = no/low panel power
#                     0x800 = no/low battery power
#                     0x8000 = Active alarm
# 
# com.victronenergy.system
#  Control/Dvcc 
#  /Control/SolarChargeCurrent  -> 0: no limiting, 1: solar charger limited by user setting or intelligent battery
#  /Dc/System/MeasurementType                                                                                                                            1
#  /Dc/System/Power 
#
# com.victronenergy.battery.socketcan_can0
#  Info/MaxChargeCurrent  -> Charge Current Limit aka CCL 



# you can prefix a function name with an underscore (_) to make it private. 
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
        ):
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

 
        # inverter list
        self._inverter = inverter

        # Allow for multiple Instance per process in DBUS
        dbus_conn = (
            dbus.SessionBus()
            if "DBUS_SESSION_BUS_ADDRESS" in os.environ
            else dbus.SystemBus()
        )
      
        self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance), dbus_conn)
        self._paths = paths
        
        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))
        
        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
        self._dbusservice.add_path('/Mgmt/ProcessVersion', VERSION)
        self._dbusservice.add_path('/Mgmt/Connection', CONNECTION)
        
        # Create the mandatory objects
        self._dbusservice.add_path('/DeviceInstance', deviceinstance)
        self._dbusservice.add_path('/ProductId', 0xFFFF)
        # self._dbusservice.add_path('/DeviceType', 345)
        self._dbusservice.add_path('/ProductName', PRODUCTNAME)
        self._dbusservice.add_path('/CustomName', customname)    
        # self._dbusservice.add_path('/AllowedRoles', 0)
        self._dbusservice.add_path('/FirmwareVersion', 0.1)
        # self._dbusservice.add_path('/HardwareVersion', 0)
        self._dbusservice.add_path('/Connected', 1)
        self._dbusservice.add_path('/Role', 'acload')
        # self._dbusservice.add_path('/Position', 0) # normaly only needed for pvinverter
        self._dbusservice.add_path('/Serial', self._getShellySerial())

        # counter
        self._dbusservice.add_path('/UpdateIndex', 0)
        self._dbusservice.add_path('/LoopIndex', 0)
        self._dbusservice.add_path('/FeedInIndex', 0)

        # additional values
        self._dbusservice.add_path('/AuxFeedInPower', AUXDEFAULT)
        self._dbusservice.add_path('/Soc', BASESOC)
        self._dbusservice.add_path('/ActualFeedInPower', 0)
        self._dbusservice.add_path('/SocFloatingMax', MINMAXSOC, writeable=True, onchangecallback=_validate_percent_value)
        self._dbusservice.add_path('/SocIncrement', 0)
        self._dbusservice.add_path('/MaxFeedIn', self._MaxFeedIn, writeable=True, onchangecallback=_validate_feedin_value)
        
        # add path values to dbus
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
        
        # last update
        self._lastUpdate = 0
        
        # add _update function 'timer'
        gobject.timeout_add(ASECOND * self._DTU_loopTime, self._update) 
        
        # add _signOfLife 'timer' to get feedback in log every 5minutes
        gobject.timeout_add((10 if not self._SignOfLifeLog else int(self._SignOfLifeLog)) * 60 * ASECOND, self._signOfLife)
        # call it once to trigger included alive signal 
        self._signOfLife() 
      
        dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
        self._monitor = DbusMonitor({
            'com.victronenergy.acload': {
                '/Ac/L1/Power': dummy
            },
            'com.victronenergy.battery': {
                '/Soc': dummy,
                '/Info/MaxChargeCurrent': dummy
            }
        })

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
            
            # read HM to grid power
            if True: #self._HM_meter:
                self._dbusservice['/ActualFeedInPower'] = self._monitor.get_value('com.victronenergy.acload.cgwacs_ttyUSB0_mb1', '/Ac/L1/Power', 0)
                #int(self._HM_meter.get_value())

            # read SOC
            if True: #self._SOC:
                newSoc = self._monitor.get_value('com.victronenergy.battery.socketcan_can0', '/Soc', MINMAXSOC)
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
            else:
                self._dbusservice['/SocFloatingMax'] = MINMAXSOC
            
        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)
           
        # return true, otherwise add_timeout will be removed from GObject - 
        return True
       

    def _getShellySerial(self):
        URL = self._statusURL
        meter_data = self._getShellyData(URL)  
        if not meter_data['mac']:
            raise ValueError("Response does not contain 'mac' attribute")
        serial = meter_data['mac']
        return serial
 
 
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
   
    def _getShellyData(self, URL):
        # request new data
        meter_r = requests.get(url = URL)
        # check for response
        if not meter_r:
            raise ConnectionError("No response from Shelly EM - %s" % (URL))
        meter_data = meter_r.json()     
        # check for Json
        if not meter_data:
            raise ValueError("Converting response to JSON failed")
        meter_r.close()
        return meter_data
 
    def _signOfLife(self):
        try:
            logging.info("--- Start: sign of life ---")
            logging.info("Last _update() call: %s" % (self._lastUpdate))
            logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
            logging.info("--- End: sign of life ---")
            # calculate min SOC based on max SOC and BASESOC. If max SOC increases lower min SOC and vice versa
            # min is addiotinal secured with an voltage guard relais and theoretically with the BMS of the battery
            minSoc = BASESOC - (self._dbusservice['/SocFloatingMax'] - BASESOC)
            # send relay On request to conected Shelly to keep micro inverters connected to grid 
            if self._dbusservice['/LoopIndex'] > 0 and self._dbusservice['/Soc'] >= minSoc:
                self._inverterSwitch( bool(self._dbusservice['/FeedInIndex'] < 50) )
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
        try:
            # get data from Shelly balcony
            URL = self._balconyURL
            balcony_data = self._getShellyData(URL)
            # store balcony power
            self._BalconyPower = balcony_data['emeters'][0]['power']
        except Exception as e:
            self._BalconyPower = AUXDEFAULT # assume AUXDEFAULT watt to reduce allowed feed in
            logging.critical('Error at %s', '_update get balcony data', exc_info=e)

        try:
            # get data from Shelly em
            URL = self._statusURL
            meter_data = self._getShellyData(URL)

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

            # publish balcony power
            self._dbusservice['/AuxFeedInPower'] = self._BalconyPower
       
            # update power value with a average sum, dependens on feedInAtNegativeWattDifference or on real feed in 
            if meter_data['emeters'][0]['power'] < -(self._Accuracy) :
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

            # logging
            logging.debug("House Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
            logging.debug("House Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
            # logging.debug("House Reverse (/Ac/Energy/Revers): %s" % (self._dbusservice['/Ac/Energy/Reverse']))
            logging.debug("---");
            
            # increment UpdateIndex - to show that new data is available
            self._dbusservice['/UpdateIndex'] = _incLimitCnt(self._dbusservice['/UpdateIndex'])
       
            # update lastupdate vars
            self._lastUpdate = time.time()              
        except Exception as e:
            self._power = EXCEPTIONPOWER   # assume feed in to reduce feed in by micro inverter
            logging.critical('Error at %s', '_update', exc_info=e)
            
        # run control loop after grid values have been updated
        self._controlLoop()
           
        # return true, otherwise add_timeout will be removed from GObject - 
        # see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True
 
    # https://github.com/victronenergy/velib_python/blob/master/dbusdummyservice.py#L63
    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True # accept the change

