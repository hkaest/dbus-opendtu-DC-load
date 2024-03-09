# dbus-opendtu-DC-load

## Table of contents

* [Introduction](#introduction)
* [Installation](#installation)
  * [Get the code](#get-the-code)
  * [Service names](#service-names)
  * [Videos how to install](#videos-how-to-install)
* [Usage](#usage)
  * [Check if script is running](#check-if-script-is-running)
  * [How to debug](#how-to-debug)
  * [How to install](#how-to-install)
  * [How to restart](#how-to-restart)
  * [How to uninstall](#how-to-uninstall)
* [How does it work](#how-does-it-work)
  * [Pictures](#pictures)
* [Tested Devices](#tested-devices)
* [Inspiration](#inspiration)
* [Furher reading](#further-reading)
  * [used documentation](#used-documentation)
  * [Discussions on the web](#discussions-on-the-web)

---

## Introduction

This project integrates (supported) Hoymiles Inverter into Victron Energy's (Venus OS) ecosystem as dcload. 

This project has been forked from https://github.com/henne49/dbus-opendtu. But there are many differences: 
* Only OpenDTU (logic is state of the art) is supported.
* The support for AHOY and templates have been removed from the original project.
* The script have been extended with the script file https://github.com/vincegod/dbus-shelly-em-smartmeter/blob/main/dbus-shelly-em-smartmeter.py for Shelly EM integration.
* None of the original services are used. This project uses com.victronenergy.dcload instead.
The remaining logic is available open source from Victron and others. Therefore the license from https://github.com/henne49/dbus-opendtu is not correct. Therefore, and to make the differences more visible, the fork has been detachted (requested via GitHub virtual assistant).

Nevertheless, this project is used in an private environment. Using this code in an commercial application may still violate some licenses.

The intention of this project is to integrate the Hoymiles micro inverter connected to the battery into the GX system and control them by the grid meter. As grid meter a shelly EM is used. 

A HM as DC load to see the temperature. The DTU set value for the AC side watt as AUX voltage.  
![title-image](img/DCloadGX.png)
This picture shows the representation of a uses com.victronenergy.dcload in the remote console. Since the topic is DC-load :-)  

---

## Installation

With the scripts in this repo, it should be easy possible to install, uninstall, restart a service that connects the OpenDTU or Ahoy to the VenusOS and GX devices from Victron.

### Get the code

The following commands should do everything for you:

Log in via console, e.g. `ssh root@192.168.178.149`

Clean up before load and load and open the ini file

```bash
/data/dbus-opendtu/uninstall.sh
rm -rf /data/dbus-opendtu
rm -rf /data/dbus-opendtu-DC-load-main
wget https://github.com/hkaest/dbus-opendtu-DC-load/archive/refs/heads/main.zip
unzip main.zip "dbus-opendtu-DC-load-main/*" -d /data
mv /data/dbus-opendtu-DC-load-main /data/dbus-opendtu
chmod a+x /data/dbus-opendtu/install.sh
nano /data/dbus-opendtu/config.ini
```

⚠️**Edit the following configuration file according to your needs before proceeding**⚠️ see [Configuration](#configuration) for details.

After editing the ini file, install the service and remove the downloaded files and check disk usage:

```bash
/data/dbus-opendtu/install.sh
rm main.zip
df -h
```

Check configuration after that - because the service is already installed and running. In case of wrong connection data (host, username, pwd) you will spam the log-file! Also, check to **set a** proper (minimal) **log level**

### Usage of the alarm /Alarms/LowStarterVoltage to raise an communication error to the OpenDTU at DC load (HMs) 

The alarm is set with an communication error during a http post request and reset with each succesfully post request. 

Remember the alarm should be reset in the remote console in the "Motifications" menu. 

### SOC is read to to calculate a seasonal SOC min value to deactivate HM feeding to grid via stopping life signal

The SOC of the battery is read and a floating max value has been added to realize an automatic increase of the generel min SOC in winter when the SOC hub is small and decrease in sommer when SOC hub (max) is bigger.

Get the value for e.g. Max:

```bash
dbus -y com.victronenergy.acload.http_59 /SocFloatingMax GetValue
```

And setting the value is also possible, the % makes dbus evaluate what comes behind it, resulting in an int instead of the default (a string).:

```bash
dbus -y com.victronenergy.acload.http_59 /SocFloatingMax SetValue %73
```

In this example, the 0 indicates succes. When trying an unsupported value the result is not 0.

Set and get max. feed in value in watts

```bash
dbus -y com.victronenergy.acload.http_59 /MaxFeedIn GetValue
```

And setting the value is also possible, the % makes dbus evaluate what comes behind it, resulting in an int instead of the default (a string).:

```bash
dbus -y com.victronenergy.acload.http_59 /MaxFeedIn SetValue %700
```
---

## Usage

This are some useful commands which helps to use the script or to debug.

### Check if script is running

`svstat /service/dbus-opendtu` show if the service (our script) is running. If number of seconds show is low, the it is probably restarting and you should look into `/data/dbus-opendtu/current.log`.

### How to debug

`dbus-spy` show all DBus values interactively.

This is useful to check if the script is running and sending values to Venus OS.

### How to install

`/data/dbus-opendtu/install.sh` installs the service persistently (see above).

This also activates the service, so you don't need to run `svcadm enable /service/dbus-opendtu` manually.

### How to restart

`/data/dbus-opendtu/restart.sh` restarts the service - e.g. after a config.ini change.

This also clears the logfile, so you can see the latest output in `nano /data/dbus-opendtu/current.log`. 

### How to uninstall

`/data/dbus-opendtu/uninstall.sh` stops the service and prevents it from being restarted (e.g. after a reboot).

If you want to remove the service completely, you can do so by running `rm -rf /data/dbus-opendtu`.

---

## Tested Devices

HM-300 and HM-400 connected to battery and Shelly EM as single phase grid meter together with a Multiplus II GX. For the HMs a OpenDTU ESP32 gateway is used.

![title-image](img/Devices.png)
---

## Inspiration

Idea is inspired on @fabian-lauer & @vikt0rm project linked below.
This project is my first on GitHub and with the Victron Venus OS, so I took some ideas and approaches from the following projects - many thanks for sharing the knowledge:

* [dbus-shelly-3em-smartmeter](https://github.com/fabian-lauer/dbus-shelly-3em-smartmeter)
* [Zero Grid (Nulleinspeisung Hoymiles HM-1500 mit OpenDTU & Python Steuerung)](https://github.com/Selbstbau-PV/Selbstbau-PV-Hoymiles-nulleinspeisung-mit-OpenDTU-und-Shelly3EM)
* [OpenDTU](https://github.com/tbnobody/OpenDTU )
* [henne49/dbus-opendtu](https://github.com/henne49/dbus-opendtu)

---

## Further reading

If you like to read more about the Venus OS and the DBus, please check the following links and sites.

### used Documentation

* [DBus paths for Victron namespace](https://github.com/victronenergy/venus/wiki/dbus#pv-inverters)
* [DBus API from Victron](https://github.com/victronenergy/venus/wiki/dbus-api)
* [How to get root access on GX device/Venus OS](https://www.victronenergy.com/live/ccgx:root_access)
* [General python library within Victron, related to D-Bus and the GX](https://github.com/victronenergy/velib_python/tree/master)
* [OpenDTU Web-API](https://github.com/tbnobody/OpenDTU/blob/master/docs/Web-API.md)
* [shelly-api-docs](https://shelly-api-docs.shelly.cloud/gen1/#shelly1-shelly1pm)
* [OpenDTU Web-API Docs](https://github.com/tbnobody/OpenDTU/blob/master/docs/Web-API.md)


