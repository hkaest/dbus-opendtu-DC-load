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
#import tests
from dbus_service import DbusService
from dbus_shelly_service import DbusShellyemService

if sys.version_info.major == 2:
    import gobject  # pylint: disable=E0401
else:
    from gi.repository import GLib as gobject  # pylint: disable=E0401

ASECOND = 1000

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

    #tests.run_tests()

    try:
        logging.info("Start")

        from dbus.mainloop.glib import DBusGMainLoop  # pylint: disable=E0401,C0415

        # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
        DBusGMainLoop(set_as_default=True)

        # formatting
        def _kwh(p, v): return (str(round(v, 2)) + "kWh")
        def _a(p, v): return (str(round(v, 1)) + "A")
        def _w(p, v): return (str(round(v, 1)) + "W")
        def _v_dc(p, v): return (str(round(v, 1)) + "V DC")
        def _v_ac(p, v): return (str(round(v, 1)) + "V AC")
        def _c(p, v): return (str(round(v, 1)) + "Degrees celsius")

        # com.victronenmergy.dcload
        # /Dc/0/Voltage              <-- V DC
        # /Dc/0/Current              <-- A, positive when power is consumed by DC loads
        # /Dc/0/Temperature          <-- Degrees centigrade, temperature sensor on SmarShunt/BMV
        # /Dc/1/Voltage              <-- SmartShunt/BMV secondary battery voltage (if configured)
        # /History/EnergyIn          <-- Total energy consumed by dc load(s).
        paths = {
            "/Dc/0/Voltage": {"initial": None, "textformat": _v_dc},
            "/Dc/0/Current": {"initial": None, "textformat": _a},
            "/Dc/0/Temperature": {"initial": None, "textformat": _c},
            "/Dc/1/Voltage": {"initial": None, "textformat": _w},
            "/History/EnergyIn": {"initial": None, "textformat": _kwh},
        }

        # Init devices/services, I've two devices
        servicename="com.victronenergy.dcload"
        logging.info("Registering dtu devices")
        # [INVERTER0]
        inverter1 = DbusService(
            servicename=servicename,
            paths=paths,
            actual_inverter=0,
        )
        # [INVERTER1]
        inverter2= DbusService(
            servicename=servicename,
            paths=paths,
            actual_inverter=1,
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
        paths = {
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
        shellyEM = DbusShellyemService(
            servicename=servicename,
            paths=paths,
            inverter1=inverter1,
            inverter2=inverter2,
        )

        # Periodically function
        # def controlLoop():
        #    logging.info("START: Control Loop is running")
        #    # pass grid meter value to first DTU inverter
        #    gridValue = shellyEM.getPower()
        #    gridValue = inverter1.setToZeroPower(gridValue)
        #    gridValue = inverter2.setToZeroPower(gridValue)
        #    logging.info("END: Control Loop is running")
        #    return True
        # gobject.timeout_add(ASECOND * 3, controlLoop)
        
        #start our main-service
        logging.info("Connected to dbus, and switching over to gobject.MainLoop() (= event based)")
        mainloop = gobject.MainLoop()
        mainloop.run()
    except Exception as error:
        logging.critical("Error at %s", "main", exc_info=error)

if __name__ == "__main__":
    main()
