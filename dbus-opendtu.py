#!/usr/bin/env python
'''module to read data from dtu/template and show in VenusOS'''


# File specific rules
# pylint: disable=broad-except

# system imports:
import logging
import os
import configparser
import sys

# our imports:
from dbus_service import OpenDTUService, DCSystemService, DCTempService
from dbus_shelly_service import DbusShellyemService
from dbus_service import GetSingleton
from dbus_temperature_service import DbusTempService

if sys.version_info.major == 2:
    import gobject  # pylint: disable=E0401
else:
    from gi.repository import GLib as gobject  # pylint: disable=E0401

# Victron packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from dbusmonitor import DbusMonitor


ASECOND = 1000
ALARM_OK = 0

def main():
    '''main loop'''
    
    # configure logging
    config = configparser.ConfigParser()
    config.read(f"{(os.path.dirname(os.path.realpath(__file__)))}/config.ini")
    logging_level = config["DEFAULT"]["Logging"]

    logging.basicConfig(
        format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging_level,
        handlers=[
            logging.FileHandler(f"{(os.path.dirname(os.path.realpath(__file__)))}/current.log"),
            logging.StreamHandler(),
        ],
    )

    try:
        logging.info("Start")

        from dbus.mainloop.glib import DBusGMainLoop  # pylint: disable=E0401,C0415

        # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
        DBusGMainLoop(set_as_default=True)

        # Use DtuSocket singleton to get init data
        socket = GetSingleton()

        # formatting
        def _kwh(p, v): return (str(round(v, 2)) + "kWh")
        def _a(p, v): return (str(round(v, 1)) + "A")
        def _w(p, v): return (str(round(v, 1)) + "W")
        def _v_dc(p, v): return (str(round(v, 1)) + "V DC")
        def _v_ac(p, v): return (str(round(v, 1)) + "V AC")
        def _c(p, v): return (str(round(v, 1)) + "Degrees celsius")

        # com.victronenmergy.(dcload)dcsystem
        # /Dc/0/Voltage              <-- V DC
        # /Dc/0/Current              <-- A, positive when power is consumed by DC loads
        # /Dc/0/Temperature          <-- Degrees centigrade, temperature sensor on SmarShunt/BMV
        # /Dc/1/Voltage              <-- SmartShunt/BMV secondary battery voltage (if configured)
        # /History/EnergyIn          <-- Total energy consumed by dc load(s).
        # /History/EnergyOut         <-- Total energy generated by ++dcsystem++ 
        # /Alarms/LowVoltage         <-- Low voltage alarm
        # /Alarms/HighVoltage        <-- High voltage alarm
        # /Alarms/LowStarterVoltage  <-- Low voltage secondary battery (if configured)
        # /Alarms/HighStarterVoltage <-- High voltage secondary battery (if configured)
        # /Alarms/LowTemperature     <-- Low temperature alarm
        # /Alarms/HighTemperature    <-- High temperature alarm
        dcPaths = {
            "/Dc/0/Voltage": {"initial": None, "textformat": _v_dc},
            "/Dc/0/Current": {"initial": None, "textformat": _a},
            "/Dc/0/Temperature": {"initial": None, "textformat": _c},
            "/Dc/1/Voltage": {"initial": None, "textformat": _w},
            "/History/EnergyIn": {"initial": None, "textformat": _kwh},
            "/History/EnergyOut": {"initial": None, "textformat": _kwh},
            "/Dc/0/Power": {"initial": None, "textformat": _w},
            "/Alarms/LowVoltage": {"initial": ALARM_OK, "textformat": None},
            "/Alarms/HighVoltage": {"initial": ALARM_OK, "textformat": None},
            "/Alarms/LowStarterVoltage": {"initial": ALARM_OK, "textformat": None},
            "/Alarms/HighStarterVoltage": {"initial": ALARM_OK, "textformat": None},
            "/Alarms/LowTemperature": {"initial": ALARM_OK, "textformat": None},
            "/Alarms/HighTemperature": {"initial": ALARM_OK, "textformat": None},
        }

        # Init devices/services, I've two devices
        # servicename="com.victronenergy.dcload" dcload has not effect when option "Has DC System" is used in the GX device
        # If the load of the HM inverters shall increase the set current limit at the mppt's dcsystem has to be used. See README.
        # But dcsystem is not visualized in VRM, therefore go back to dcload and dcload to booster dcsystem bellow 
        servicename="com.victronenergy.dcload"
        logging.info("Registering dtu devices")
        inverterList = [        
            # [INVERTER0]
            OpenDTUService(
                servicename=servicename,
                paths=dcPaths,
                actual_inverter=0,
                data=socket.getLimitData(0),
            ),
            # [INVERTER1]
            OpenDTUService(
                servicename=servicename,
                paths=dcPaths,
                actual_inverter=1,
                data=socket.getLimitData(1),
            ),
            # [INVERTER2]
            OpenDTUService(
                servicename=servicename,
                paths=dcPaths,
                actual_inverter=2,
                data=socket.getLimitData(2),
            )
        ]

        # add dc system to count dc load
        servicename="com.victronenergy.dcsystem"
        dcService = DCSystemService(
            servicename=servicename,
            paths=dcPaths,
            actual_inverter=3,
        )

        # com.victronenergy.temperature
        # /Temperature        degrees Celcius
        # /TemperatureType    0=battery; 1=fridge; 2=generic, 3=Room, 4=Outdoor, 5=WaterHeater, 6=Freezer
        # The others are for wired inputs only and Ruuvis only
        temperaturePaths = {
            "/Temperature": {"initial": None, "textformat": _c},
            '/TemperatureType': {'initial': 0, "textformat": None},
        }

        # add temperature service to control relay of cerbo GX
        servicename="com.victronenergy.temperature"
        tempService=DCTempService(
            servicename=servicename,
            paths=temperaturePaths,
            actual_inverter=4,
        )

        # com.victronenergy.acload
        # /Ac/Energy/Forward     <- kWh  - bought energy (total of all phases)
        # /Ac/Power              <- W    - total of all phases, real power
        # /Ac/Current            <- A AC - Deprecated
        # /Ac/Voltage            <- V AC - Deprecated
        # /Ac/L1/Current         <- A AC
        # /Ac/L1/Energy/Forward  <- kWh  - bought
        # /Ac/L1/Power           <- W, real power
        # /Ac/L1/Voltage         <- V AC
        # /Ac/L2/*               <- same as L1
        # /Ac/L3/*               <- same as L1
        # /DeviceType
        # /ErrorCode
        acPaths = {
            '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh}, # energy bought from the grid
            '/Ac/Power': {'initial': 0, 'textformat': _w},
            '/Ac/Current': {'initial': 0, 'textformat': _a},
            '/Ac/Voltage': {'initial': 0, 'textformat': _v_ac},
            '/Ac/L1/Voltage': {'initial': 0, 'textformat': _v_ac},
            '/Ac/L1/Current': {'initial': 0, 'textformat': _a},
            '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
            '/Ac/L1/Energy/Forward': {'initial': 0, 'textformat': _kwh},
        }

        #[SHELLY]
        servicename="com.victronenergy.acload"
        logging.info("Registering Shelle EM")
        DbusShellyemService(
            servicename=servicename,
            paths=acPaths,
            inverter=inverterList,
            dbusmon=None, #monitor is initialized by self with GLib.timeout_add_seconds method call
            dcSystemService=dcService, 
            tempService=tempService,
        )

        # start our main-service
        logging.info("Connected to dbus, and switching over to gobject.MainLoop() (= event based)")
        mainloop = gobject.MainLoop()
        mainloop.run()
    except Exception as error:
        logging.critical("Error at %s", "main", exc_info=error)

if __name__ == "__main__":
    main()
