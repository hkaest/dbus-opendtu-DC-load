# import normal packages
import logging
import sys
import os
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys

import dbus

from version import softwareversion

# Victron packages
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService

PRODUCTNAME = "Temperature by CAN Battery"
CONNECTION = "TCP/IP (HTTP)"
PRODUCT_ID = 0
FIRMWARE_VERSION = 0
HARDWARE_VERSION = 0
CONNECTED = 1

def _validate_temprature_value(path, newvalue):
    # percentage range
    return newvalue <= 20 and newvalue >= 0

class DbusTempService:
    def __init__(
            self, 
            servicename, 
            paths, 
        ):

        deviceinstance = int(139)

        self._dbusservice = VeDbusService("{}.dbus_{:02d}".format(servicename, deviceinstance), register=False)

        # Create the mandatory objects
        self._dbusservice.add_mandatory_paths(__file__, softwareversion, CONNECTION, deviceinstance, PRODUCT_ID, PRODUCTNAME, FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)

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

        # register VeDbusService after all paths where added
        self._dbusservice.register()


        # public functions
    def setTemperature(self, temp):
        self._dbusservice["/Temperature"] = temp

    # https://github.com/victronenergy/velib_python/blob/master/dbusdummyservice.py#L63
    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True # accept the change


        

