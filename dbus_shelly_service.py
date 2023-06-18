'''DbusShellyemService'''
 
# import normal packages
import platform 
import logging
import sys
import os
import sys
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


class DbusShellyemService:
  def __init__(self, servicename, paths, inverter1: DbusService, inverter2: DbusService):
    config = self._getConfig()
    deviceinstance = int(config['SHELLY']['Deviceinstance'])
    customname = config['SHELLY']['CustomName']

    self._inverter1 = inverter1
    self._inverter2 = inverter2
   
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
    
    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    # power value 
    self._power = 0
    
    # last update
    self._lastUpdate = 0
    # add _update function 'timer'
    gobject.timeout_add(ASECOND * 3, self._update) 
    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*ASECOND, self._signOfLife)

    gobject.timeout_add(ASECOND * 3, self._controlLoop)

 
  # Periodically function
  def _controlLoop(self):
      logging.info("START: Control Loop is running")
      # pass grid meter value to first DTU inverter
      gridValue = self._power
      gridValue = inverter1.setToZeroPower(gridValue)
      gridValue = inverter2.setToZeroPower(gridValue)
      logging.info("END: Control Loop is running")
      return True
        
  def getPower(self):
    return self._power
 
  def _getShellySerial(self):
    meter_data = self._getShellyData()  
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
    
 
  def _getShellyData(self):
    # preset power value 
    self._power = 0
    # request new data
    URL = self._getShellyStatusUrl()
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
    return True
 
  def _update(self):   
    try:
       #get data from Shelly em
       meter_data = self._getShellyData()
       
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

       # update power value 
       self._power = meter_data['emeters'][0]['power']
   
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
