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

from dbus_service import DbusService
 
# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService

VERSION = '1.0'
ASECOND = 1000  # second
PRODUCTNAME = "GRID by Shelly"
CONNECTION = "TCP/IP (HTTP)"
ACCURACY = 10 #watts, something like a control step size (2 * ACCURACY) 


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
        self._ZeroPoint = int(config['DEFAULT']['ZeroPoint'])
        self._MaxFeedIn = int(config['DEFAULT']['MaxFeedIn'])
        self._consumeFilterFactor = int(config['DEFAULT']['consumeFilterFactor'])
        self._feedInFilterFactor = int(config['DEFAULT']['feedInFilterFactor'])
 
        # inverter list
        self._inverter = inverter
              
        self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance))
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
        self._dbusservice.add_path('/UpdateIndex', 0)
        self._dbusservice.add_path('/LoopIndex', 0)
        
        # add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
              path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)
      
        # power value 
        self._power = int(0)
        self._BalconyPower = int(0)
        
        # last update
        self._lastUpdate = 0
        
        # add _update function 'timer'
        gobject.timeout_add(ASECOND * 1, self._update) 
        
        # add _signOfLife 'timer' to get feedback in log every 5minutes
        gobject.timeout_add(self._getSignOfLifeInterval()*60*ASECOND, self._signOfLife)
        # call it once to trigger included alive signal 
        self._signOfLife() 
      
        # add _controlLoop for zero feeding
        gobject.timeout_add(ASECOND * 1, self._controlLoop)

 
    # Periodically function
    def _controlLoop(self):
        # pass grid meter value and allowed feed in to first DTU inverter
        logging.info("START: Control Loop is running")
        POWER = 0
        FEEDIN = 1
        gridValue = [int(int(self._power) - self._ZeroPoint),int(self._MaxFeedIn - self._BalconyPower)]
        # around zero point do nothing 
        logging.info(f"PRESET: Control Loop {gridValue[POWER]}, {gridValue[FEEDIN]} ")
        number = 0
        while abs(gridValue[POWER]) > ACCURACY and number < len(self._inverter):
            gridValue = self._inverter[number].setToZeroPower(gridValue[POWER], gridValue[FEEDIN])
            logging.info(f"CHANGED: Control Loop {gridValue[POWER]}, {gridValue[FEEDIN]} ")
            #adapt stored power value to value reduced by micro inverter  
            self._power = gridValue[POWER] + self._ZeroPoint
            #swap inverters to avoid using mainly the first one
            if number > 0:
                self._inverter[number], self._inverter[number - 1] = self._inverter[number - 1], self._inverter[number]
            number = number + 1
        logging.info("END: Control Loop is running")
        # increment LoopIndex - to show that loop is running
        index = self._dbusservice['/LoopIndex'] + 1  # increment index
        if index < 255:   # maximum value of the index
            self._dbusservice['/LoopIndex'] = index
        return True
        
    def getPower(self):
        return self._power
 
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
 
 
    def _getSignOfLifeInterval(self):
        config = self._getConfig()
        value = config['DEFAULT']['SignOfLifeLog']
        if not value: 
            value = 0
        return int(value)
  
  
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
        return meter_data
 
 
    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s" % (self._lastUpdate))
        logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
        logging.info("--- End: sign of life ---")
        # send relay On request to conected Shelly to keep micro inverters connected to grid 
        if self._dbusservice['/LoopIndex'] > 0 and self._keepAliveURL:
            try:
                    url = self._keepAliveURL
                    response = requests.get(url = url)
                    logging.info(f"RESULT: keepAliveURL, response = {response}")
            except Exception as genExc:
                logging.warning(f"HTTP Error at keepAliveURL for inverter: {str(genExc)}")
        # reset 
        self._dbusservice['/LoopIndex'] = 0
        return True
 
    def _update(self):   
        try:
            #get data from Shelly em
            URL = self._statusURL
            meter_data = self._getShellyData(URL)
            #get data from Shelly balcony
            URL = self._balconyURL
            balcony_data = self._getShellyData(URL)

            #store balcony power
            self._BalconyPower = balcony_data['emeters'][0]['power']
            
            #send data to DBus
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
       
            # update power value with a average sum
            if meter_data['emeters'][0]['power'] > 0:
                self._power = int(((self._power * self._consumeFilterFactor) + meter_data['emeters'][0]['power']) / (self._consumeFilterFactor + 1))
            else:
                self._power = int(((self._power * self._feedInFilterFactor) + meter_data['emeters'][0]['power']) / (self._feedInFilterFactor + 1))

            #logging
            logging.debug("House Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
            logging.debug("House Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
            # logging.debug("House Reverse (/Ac/Energy/Revers): %s" % (self._dbusservice['/Ac/Energy/Reverse']))
            logging.debug("---");
            
            # increment UpdateIndex - to show that new data is available
            index = self._dbusservice['/UpdateIndex'] + 1  # increment index
            if index > 255:   # maximum value of the index
              index = 0       # overflow from 255 to 0
            self._dbusservice['/UpdateIndex'] = index
       
            #update lastupdate vars
            self._lastUpdate = time.time()              
        except Exception as e:
            logging.critical('Error at %s', '_update', exc_info=e)
           
        # return true, otherwise add_timeout will be removed from GObject - 
        # see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True
 
    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True # accept the change
