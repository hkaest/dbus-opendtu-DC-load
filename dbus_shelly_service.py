
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

from dbus_service import OpenDTUService, DCSystemService, DCTempService, DtuSocket
from dbus_service import ALARM_BALCONY, ALARM_GRID, ALARM_BATTERY 
from version import softwareversion


# Victron packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService
from dbusmonitor import DbusMonitor


PRODUCTNAME = "GRID by Shelly"
CONNECTION = "TCP/IP (HTTP)"
PRODUCT_ID = 0
FIRMWARE_VERSION = 0
HARDWARE_VERSION = 0
CONNECTED = 1

ERROR_NONE = "--"

AUXDEFAULT = 500                   # [W] assumed plugin power to reduce allowed feed in
EXCEPTIONPOWER = -100              # [W] assumed feed in to reduce feed in by micro inverter
BASESOC = 54                       # [%] with 8% min SOC -> 92% range -> 54% in the middle
MINMAXSOC = BASESOC + 20           # [%] 40% range per default
MAXCALCSOC = 125                   # [%] 100% plus 25 days/loadcycles (stick longer at 100% in summer)
MAXSOC = 100
CCL_DEFAULT = 10                   # [A] at 10°C 
CCL_MINTEMP = 10                   # [°C]
COUNTERLIMIT = 255
MINMAXDISCHARGE = 52               # [V] required DCL for max Power (2200 + 800)W/58V 
HEATER_STOP = 16.5                 # [°C] deactivation value for relay, higher than configured value in GX
HEATER_RESTART = 12.0              # [°C] re-activation value for relay, bellow to configured value in GX
HEATER_POWER = 1.0                 # [A] heater power 50VA / 58V ~ 1A 
HEATER_ENABLE_TIME = 60 * 24 * 2   # [minutes] heater enabled time, after a CCL limit has been hit 
HEATER_MIN_SOC = 15                # [%] 


# you can prefix a function name with an underscore (_) to declare it private. 
def _validate_percent_value(path, newvalue):
    # percentage range
    return newvalue <= MAXCALCSOC and newvalue >= MINMAXSOC
    
def _validate_powersoc_value(path, newvalue):
    # percentage range
    return newvalue <= (MAXSOC - 1) and newvalue >= 10
    
def _validate_feedin_value(path, newvalue):
    # watts range
    return newvalue <= 800 

def _validate_heater_value(path, newvalue):
    # positive integer 
    return newvalue > 0 

def _incLimitCnt(value):
    return (value + 1) % COUNTERLIMIT

    
class DbusShellyemService:
    def __init__(
            self, 
            servicename, 
            paths, 
            inverter,
            dbusmon,
            dcSystemService: DCSystemService, 
            tempService: DCTempService,
        ):
        self._socket = DtuSocket()
        self._monitor = dbusmon
        config = self._getConfig()
        deviceinstance = int(config['SHELLY']['Deviceinstance'])
        customname = config['SHELLY']['CustomName']
        self._statusURL = self._getShellyStatusUrl()
        self._plugInSolarURL = self._getPlugInSolarShellyUrl()
        self._keepAliveURL = config['SHELLY']['KeepAliveURL']
        self._SwitchOffURL = config['SHELLY']['SwitchOffURL']
        self._ZeroPoint = int(config['DEFAULT']['ZeroPoint'])
        self._MaxFeedIn = int(config['DEFAULT']['MaxFeedIn'])
        self._consumeFilterFactor = int(config['DEFAULT']['consumeFilterFactor'])
        self._feedInFilterFactor = int(config['DEFAULT']['feedInFilterFactor'])
        self._bigPowerChangeDifference = int(config['DEFAULT']['feedInAtNegativeWattDifference'])
        self._Accuracy = int(config['DEFAULT']['ACCURACY'])
        self._DTU_loopTime = int(config['DEFAULT']['DTU_loopTime'])
        self._SignOfLifeLog = config['DEFAULT']['SignOfLifeLog']
        # Shelly EM session
        self._eMsession = requests.Session()
        self._balconySession = requests.Session()
 
        # inverter list of type OpenDTUService
        self._inverter = inverter
        self._dcSystemService = dcSystemService
        self._tempService = tempService

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
        self._dbusservice.add_path('/NegativeGridCounter', 0)  # counts the times there is a real feed in / power from grid is real negative
        self._dbusservice.add_path('/FeedInRelay', False)

        # additional values
        self._dbusservice.add_path('/AuxFeedInPower', AUXDEFAULT)
        self._dbusservice.add_path('/Soc', BASESOC)
        self._dbusservice.add_path('/SocChargeCurrent', 0)
        self._dbusservice.add_path('/SocMaxChargeCurrent', 20)
        self._dbusservice.add_path('/SocMaxDischargeCurrent', 12.5)  # 12.5 @ 3% SOC in summer
        # self._dbusservice.add_path('/ActualFeedInPower', 0)
        self._dbusservice.add_path('/SocFloatingMax', MINMAXSOC, writeable=True, onchangecallback=_validate_percent_value)
        self._dbusservice.add_path('/SocIncrement', 0)
        self._dbusservice.add_path('/SocVolt', 0)
        self._dbusservice.add_path('/MaxFeedIn', self._MaxFeedIn, writeable=True, onchangecallback=_validate_feedin_value)
        self._dbusservice.add_path('/FeedInMinSoc', MAXCALCSOC)
        self._dbusservice.add_path('/PowerFeedInSoc', 96, writeable=True, onchangecallback=_validate_powersoc_value)
        self._dbusservice.add_path('/HeaterEnableCounter', HEATER_ENABLE_TIME, writeable=True, onchangecallback=_validate_heater_value)

        # test custom error 
        self._dbusservice.add_path('/Error', ERROR_NONE)

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
        self._PlugInSolarPower = int(0)
        self._ChargeLimited = False

        # last update
        self._lastUpdate = 0
        
        # add _update timed function, get DTU data and control HMs by call of _controlLoop()
        # Doing all in one task context realizes a control loop by reading back the actual values before new values are calculated
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
            limitData = self._socket.fetchLimitData()
            invCurrent = 0
            boostCurrent = 0
            temperature = 0
            plugInFeedsIn = False
            if not limitData:
                logging.info("LIMIT DATA: Failed")
            else:
                number = 0
                # trigger inverter to fetch meter data from singleton
                while number < len(self._inverter):
                    dtuService:OpenDTUService = self._inverter[number]
                    invCurrent += dtuService.updateMeterData()
                    number = number + 1
                # loop
                POWER = 0
                FEEDIN = 1
                maxFeedIn = int(self._dbusservice['/MaxFeedIn'] - self._PlugInSolarPower)
                maxDischarge = int(self._dbusservice['/SocVolt'] * self._dbusservice['/SocMaxDischargeCurrent'])
                plugInFeedsIn = int(self._PlugInSolarPower) > 20 and (self._dbusservice['/Error'] == ERROR_NONE)  # plug in with appr. 20 W
                powerOffset = -self._ZeroPoint
                # with floating max is high and plugin feeds in - put zero point to zero
                if (int( self._dbusservice['/SocFloatingMax']) > MAXSOC) and plugInFeedsIn:
                    powerOffset = 0
                # with apprx. 100% SOC and solar available - put zero point to lower side to feed in more
                if int(self._dbusservice['/Soc']) > int(self._dbusservice['/PowerFeedInSoc']) and plugInFeedsIn:
                    powerOffset = self._ZeroPoint * (int(self._dbusservice['/Soc']) - int(self._dbusservice['/PowerFeedInSoc']))
                gridValue = [int(int(self._power) + powerOffset),min(maxFeedIn, maxDischarge)]
                logging.info(f"PRESET: Control Loop {gridValue[POWER]}, {gridValue[FEEDIN]} ")
                number = 0
                # use loop counter to swap with slow _SignOfLifeLog cycle
                swap = bool(self._dbusservice['/LoopIndex'] == 0)
                # around zero point do nothing 
                while abs(gridValue[POWER]) > self._Accuracy and number < len(self._inverter):
                    # Do not swap when set values are changed
                    swap = False
                    inPower = gridValue[POWER]
                    dtuService:OpenDTUService = self._inverter[number]
                    gridValue = dtuService.setToZeroPower(gridValue[POWER], gridValue[FEEDIN])
                    # multiple inverter, set new limit only once in a loop
                    if inPower != gridValue[POWER]:
                        # adapt stored power value to value reduced by micro inverter  
                        self._power = gridValue[POWER] - powerOffset
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
                # increment or reset NegativeGridCounter, increment in case the power set value is negative
                if gridValue[POWER] < -(self._bigPowerChangeDifference):
                    index = self._dbusservice['/NegativeGridCounter'] + 1  # increment index
                    if index < COUNTERLIMIT:   # maximum value of the index
                        self._dbusservice['/NegativeGridCounter'] = index
                else:
                    self._dbusservice['/NegativeGridCounter'] = 0
                # increment LoopIndex - to show that loop is running
                self._dbusservice['/LoopIndex'] += 1  # increment index
            
            # reset inverter current when relay is off 
            if self._dbusservice['/FeedInRelay'] == False:
                invCurrent = 0

            # read SOC
            if self._monitor:
                batteryServices = self._monitor.get_service_list('com.victronenergy.battery')
                for serviceItem in batteryServices:
                    newSoc = int(self._monitor.get_value(serviceItem, '/Soc', MINMAXSOC))
                    current = float(self._monitor.get_value(serviceItem, '/Dc/0/Current', 0))
                    maxCurrent = float(self._monitor.get_value(serviceItem, '/Info/MaxChargeCurrent', CCL_DEFAULT))
                    maxDischargeCurrent = float(self._monitor.get_value(serviceItem, '/Info/MaxDischargeCurrent', CCL_DEFAULT))
                    temperature = float(self._monitor.get_value(serviceItem, '/Dc/0/Temperature', 0))
                    volt = float(self._monitor.get_value(serviceItem, '/Dc/0/Voltage', 0))
                #int(self._SOC.get_value())
                oldSoc = self._dbusservice['/Soc']
                incSoc = newSoc - oldSoc
                if incSoc != 0:
                    # direction change + * - = -
                    if (incSoc * self._dbusservice['/SocIncrement']) < 0:
                        if self._dbusservice['/SocIncrement'] > 0:
                            if oldSoc == MAXSOC:
                                self._dbusservice['/SocFloatingMax'] = MAXCALCSOC
                            if oldSoc < MAXSOC and oldSoc > self._dbusservice['/SocFloatingMax']:
                                # increase max immediately with half of difference since each increase of max counts twice for increase of range
                                self._dbusservice['/SocFloatingMax'] += int( ((oldSoc - self._dbusservice['/SocFloatingMax']) + 1) / 2 )
                            if (oldSoc >= MINMAXSOC or self._dbusservice['/SocFloatingMax'] > MINMAXSOC) and oldSoc < self._dbusservice['/SocFloatingMax']:
                                # decrease by steps until MINMAXSOC is reached
                                self._dbusservice['/SocFloatingMax'] -= 1 
                    self._dbusservice['/SocIncrement'] = incSoc
                    self._dbusservice['/SocVolt'] = volt
                    self._dbusservice['/Soc'] = newSoc
                # publish data to DBUS as debug data
                self._dbusservice['/SocChargeCurrent'] = current
                self._dbusservice['/SocMaxChargeCurrent'] = maxCurrent
                self._dbusservice['/SocMaxDischargeCurrent'] = maxDischargeCurrent

                # CCL:              [.....]---100A-----
                #                  dwn    up
                #           [--10A--[.....]
                # ----------[       
                #          ~5°    ~13°  ~16° 
                # 
                # two point control, to avoid volatile signal changes
                self._ChargeLimited = bool((maxCurrent - current) < 1.2) if self._ChargeLimited else bool((maxCurrent - current) < 0.5) 
                
                # set booster data (additional CCL, since CCL is to restrictive at lower temperature) see graph
                # rumors state that a FW update of the LFP batteries will increase CCL at lower limits. A option for the future!
                #if self._ChargeLimited and self._dbusservice['/Soc'] < BASESOC:
                #    # allow additional charge current on lower side of SOC, limit is at double of max current
                #    boostCurrent = min(CCL_DEFAULT,maxCurrent,(current - maxCurrent) + 1.0);
            else:
                self._dbusservice['/SocFloatingMax'] = MINMAXSOC
            
            # calculate min SOC based on max SOC and BASESOC. If max SOC increases lower min SOC and vice versa
            # min is addiotinal secured with an voltage guard relais and theoretically with the BMS of the battery
            # deactivate when AC load is on (at least 10A additional dc load) to prevent high discharge current when SocMaxDischargeCurrent is low
            if self._dbusservice['/SocChargeCurrent'] > -float(invCurrent + CCL_DEFAULT):
                self._dbusservice['/FeedInMinSoc'] = int(BASESOC - (min(int(self._dbusservice['/SocFloatingMax']),MAXSOC) - BASESOC))
            elif int(self._dbusservice['/SocMaxDischargeCurrent']) > MINMAXDISCHARGE:
                self._dbusservice['/FeedInMinSoc'] = int(BASESOC - (min(int(self._dbusservice['/SocFloatingMax']),MAXSOC) - BASESOC))
            else:
                self._dbusservice['/FeedInMinSoc'] = int(MAXCALCSOC)

            # set consumed power and CCL booster at dcsystem  
            if invCurrent > 0:
                volt = self._dbusservice['/SocVolt']
                self._dcSystemService.setPower(volt, int(invCurrent + boostCurrent), int(volt * (invCurrent + boostCurrent)), temperature)
            else:
                self._dcSystemService.setPower(0, 0, 0, temperature)
            
            # set temperature to control heater relay when plugin solar feeds in
            if self._ChargeLimited:
                self._dbusservice['/HeaterEnableCounter'] = HEATER_ENABLE_TIME
            if temperature >= HEATER_STOP: 
                # pass higher battery temperature to stop heater anyway
                self._tempService.setTemperature(temperature)
            elif (    not plugInFeedsIn
                   or (self._dbusservice['/SocMaxChargeCurrent'] > CCL_DEFAULT and temperature > HEATER_RESTART)):
                # w/o general solar power stop heater
                self._tempService.setTemperature(HEATER_STOP)
            elif (     self._dbusservice['/SocChargeCurrent'] > HEATER_POWER 
                   and self._dbusservice['/SocMaxChargeCurrent'] <= CCL_DEFAULT 
                   and self._dbusservice['/HeaterEnableCounter'] > 0 
                   and self._dbusservice['/Soc'] > HEATER_MIN_SOC ):
                # if CCL is limited and charge power has reached required power by heater at low battery tepreature 
                self._tempService.setTemperature(HEATER_RESTART)


        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)
           
        # return true, otherwise add_timeout will be removed from GObject - 
        return True
       
    def _createDbusMonitor(self):
        dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
        self._monitor = DbusMonitor({
            # do not scan 'com.victronenergy.acload' since we are a acload too. This will cause trouble at the DBUS-Monitor from com.victronenergy.system
            # com.victronenergy.battery.socketcan_can0 or can1 etc.
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
                '/Info/MaxDischargeCurrent': dummy,
                '/Dc/0/Temperature': dummy,
                '/Dc/0/Voltage': dummy,
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

 
    def _getPlugInSolarShellyUrl(self):
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
        except requests.ConnectionError as e:
            # site does not exist
            self._dbusservice['/Error'] =f"{alarm} / Connect Error"
        except Exception as err:
            logging.critical('Error at %s', '_fetch_url', exc_info=err)
            self._dbusservice['/Error'] =f"{alarm} / Critical Exception"
        finally:
            self._inverter[0].setAlarm(alarm, bool(not json))
            return json
 
    def _signOfLife(self):
        try:
            self._dbusservice['/HeaterEnableCounter'] = max(0, self._dbusservice['/HeaterEnableCounter'] - (10 if not self._SignOfLifeLog else int(self._SignOfLifeLog)))
            logging.info(" --- Check for min SOC and switch relais --- ")
            # send relay On request to conected Shelly to keep micro inverters connected to grid 
            if self._dbusservice['/LoopIndex'] > 0 and int(self._dbusservice['/Soc']) > int(self._dbusservice['/FeedInMinSoc']):
                if bool(self._dbusservice['/NegativeGridCounter'] < 50):
                    self._inverterSwitch( True )
                    logging.info(" ---           switch relais ON          --- ")
                else:
                    self._inverterSwitch( False )
                    logging.info(" ---   Permanent negative grid --> OFF   --- ")
            else:
                self._inverterSwitch( False )
                logging.info(" ---  Configured min SOC reached --> OFF --- ")
            # reset 
            self._dbusservice['/LoopIndex'] = 0
        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)
           
        # return true, otherwise add_timeout will be removed from GObject - 
        return True

    def _inverterSwitch(self, on):
        # send relay On request to conected Shelly to keep micro inverters connected to grid
        self._dbusservice['/FeedInRelay'] = False 
        if on and self._keepAliveURL:
            try:
                response = requests.get(url = self._keepAliveURL)
                logging.info(f"RESULT: keep relay alive at shelly, response status code = {str(response.status_code)}")
                self._dbusservice['/FeedInRelay'] = True 
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
        self._dbusservice['/Error'] = "--"

        # get feed in from plug in solar
        balcony_data = self._fetch_url(self._plugInSolarURL, ALARM_BALCONY, self._balconySession)
        if balcony_data:
            self._PlugInSolarPower = balcony_data['emeters'][0]['power'] 
        else:
            self._dbusservice['/Error'] = ALARM_BALCONY
            self._PlugInSolarPower = AUXDEFAULT # assume AUXDEFAULT watt to reduce allowed feed in
        # publish power of plug in solar
        self._dbusservice['/AuxFeedInPower'] = self._PlugInSolarPower

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
       
            # update power value with a average sum, dependens on (use)feedInAtNegativeWattDifference or on real feed in 
            if meter_data['emeters'][0]['power'] < -(self._Accuracy):
                # if the meter value is negative (feed in) react faster, _feedInFilterFactor = 0 -> meter value is directly used
                self._setPowerMovingAverage(self._feedInFilterFactor, meter_data['emeters'][0]['power'])
            elif (self._power - meter_data['emeters'][0]['power']) > self._bigPowerChangeDifference:
                # if the change is greater than _bigPowerChangeDifference handle the power value like a feed in (react faster)
                self._setPowerMovingAverage(self._feedInFilterFactor, meter_data['emeters'][0]['power'])
            else:
                # in all other cases assume consume and react with _consumeFilterFactor
                self._setPowerMovingAverage(self._consumeFilterFactor, meter_data['emeters'][0]['power'])

            # increment UpdateIndex - to show that new data is available
            self._dbusservice['/UpdateIndex'] = _incLimitCnt(self._dbusservice['/UpdateIndex'])
       
            # update lastupdate vars
            self._lastUpdate = time.time()              
        else:
            self._dbusservice['/Error'] = ALARM_GRID
            self._power = EXCEPTIONPOWER   # assume feed in to reduce feed in by micro inverter
            
        # run control loop after grid values have been updated
        self._controlLoop()
           
        # switch feed in relais off at low soc, do not wait for _signOfLife, concurrent access?
        # if int(self._dbusservice['/Soc']) <= int(self._dbusservice['/FeedInMinSoc']):
        #    self._inverterSwitch( False )

        # return true, otherwise add_timeout will be removed from GObject - 
        # see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True

    # the factors are used to build a simplified moving average (SMA), SMA(x) = (SMA(t - 1) * factor + x) / (factor + 1)
    def _setPowerMovingAverage(self, factor, actPower):
        # for bad mathematics check fator for 0 :)
        if factor == 0:
            self._power = int(actPower)
        else:
            self._power = int(((self._power * factor) + actPower) / (factor + 1))


    # https://github.com/victronenergy/velib_python/blob/master/dbusdummyservice.py#L63
    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True # accept the change

