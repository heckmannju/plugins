#!/usr/bin/env python3

#########################################################################
# Copyright 2015 Henning Behrend; Version 0.2
#########################################################################
#
#  SmartHome.py is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHome.py is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHome.py.  If not, see <http://www.gnu.org/licenses/>.
#########################################################################

import threading
import time

import lib.plugin

# pymodbus library from https://code.google.com/p/pymodbus/

from pluggit.sync import ModbusTcpClient
from pluggit.constants import Endian
from pluggit.payload import BinaryPayloadDecoder


class PluggitException(Exception):
    pass


class Pluggit(lib.plugin.Plugin):

    _myTempReadDict = {}
    _myTempWriteDict = {}
    # ============================================================================#
    # define variables for most important modbus registers of KWL "Pluggit AP310"
    #
    # Important: In the PDU Registers are addressed starting at zero.
    # Therefore registers numbered 1-16 are addressed as 0-15.
    # that means e.g. holding register "40169" is "40168" and so on
    # ============================================================================#

    # dictionary for modbus registers
    _modbusRegisterDic = {
        # 'prmDateTime': 108,                   # 40109: Current Date/time in Unix time (amount of seconds from 1.1.1970)
        'prmRamIdxT1': 133,                     # 40133: T1, °C
        'prmRamIdxT2': 135,                     # 40135: T2, °C
        'prmRamIdxT3': 137,                     # 40137: T3, °C
        'prmRamIdxT4': 139,                     # 40139: T4, °C
        # 'prmRamIdxT5': 140,                   # 40141: T5, °C
        # 40169: Active Unit mode> 0x0004 Manual Mode; 0x0008 WeekProgram
        'prmRamIdxUnitMode': 168,
        # 40199: Bypass state> Closed 0x0000; In process 0x0001; Closing
        # 0x0020; Opening 0x0040; Opened 0x00FF
        'prmRamIdxBypassActualState': 198,
        # 40325: Speed level of Fans in Manual mode; shows a current speed
        # level [4-0]; used for changing of the fan speed level
        'prmRomIdxSpeedLevel': 324,
        # 'prmVOC': 430,                        # 40431: VOC sensor value (read from VOC); ppm. If VOC is not installed, then 0.
        # 'prmBypassTmin': 444,                 # 40445: Minimum temperature of Bypass openning (°C), if T1 < Tmin then bypass should be closed
        # 'prmBypassTmax': 446,                 # 40447: Maximum temperature of Bypass openning (°C), if T1 > Tmax or Tmax is 0 then bypass should be closed
        # 'prmWorkTime': 624                    # 40625: Work time of system, in hour (UNIX)
        # 40467: Number of the Active Week Program (for Week Program mode)
        'prmNumOfWeekProgram': 466,
        # 40555: Remaining time of the Filter Lifetime (Days)
        'prmFilterRemainingTime': 554
    }

    # Initialize connection
    def __init__(self, core, conf):
        lib.plugin.Plugin.__init__(self, core, conf)
        self._cd = core
        self._host = conf.get('host')
        self._port = conf.get('port')
        self._cycle = conf.get('cycle')
        self._lock = threading.Lock()
        self._is_connected = False
        self._items = {}
        self.connect()
        self.disconnect()
        # pydevd.settrace("192.168.0.125")

    def connect(self):
        start_time = time.time()
        if self._is_connected:
            return True
        self._lock.acquire()
        try:
            self.logger.info("Pluggit: connecting to {0}:{1}".format(
                self._host, self._port))
            self._Pluggit = ModbusTcpClient(self._host, self._port)
        except Exception as e:
            self.logger.error("Pluggit: could not connect to {0}:{1}: {2}".format(
                self._host, self._port, e))
            return
        finally:
            self._lock.release()
        self.logger.info("Pluggit: connected to {0}:{1}".format(
            self._host, self._port))
        self._is_connected = True
        end_time = time.time()
        self.logger.info("Pluggit: connection took {0} seconds".format(
            end_time - start_time))

    def disconnect(self):
        start_time = time.time()
        if self._is_connected:
            try:
                self._Pluggit.close()
            except:
                pass
        self._is_connected = False
        end_time = time.time()
        self.logger.info("Pluggit: disconnect took {0} seconds".format(
            end_time - start_time))

    def start(self):
        self.alive = True
        self._cd.scheduler.add('Pluggit', self._refresh, cycle=self._cycle)

    def stop(self):
        self.alive = False

    # parse items in pluggit.conf
    def pre_stage(self):
        # check for smarthome.py attribute 'pluggit_listen' in pluggit.conf
        for item in self._core.config.query_nodes('pluggit_listen'):
            # self.logger.debug("Pluggit: parse read item: {0}".format(item))
            pluggit_key = item.attr['pluggit_listen']
            if pluggit_key in self._modbusRegisterDic:
                self._myTempReadDict[pluggit_key] = item
                # self.logger.debug("Pluggit: Inhalt des dicts _myTempReadDict nach Zuweisung zu item: '{0}'".format(self._myTempReadDict))
            else:
                self.logger.warn(
                    "Pluggit: invalid key {0} configured".format(pluggit_key))
        for item in self._core.config.query_nodes('pluggit_send'):
            # self.logger.debug("Pluggit: parse send item: {0}".format(item))
            pluggit_sendKey = item.attr['pluggit_send']
            if pluggit_sendKey is not None:
                self._myTempWriteDict[pluggit_sendKey] = item
                # self.logger.debug("Pluggit: Inhalt des dicts _myTempWriteDict nach Zuweisung zu send item: '{0}'".format(self._myTempWriteDict))
                item.add_method_trigger(self.update_item)

    def update_item(self, value=None, trigger=None):
        if trigger['caller'] != 'Pluggit':
            item = trigger['node']
            if 'pluggit_send' in item.attr:
                command = item.attr['pluggit_send']
                self.logger.info("Pluggit: {0} set {1} to {2} for {3}".format(
                    trigger['caller'], command, value, item.id))
                if(command == 'activatePowerBoost') and (isinstance(value, bool)):
                    if value:
                        self._activatePowerBoost()
                    else:
                        self._activateWeekProgram()

    def _activatePowerBoost(self):

        active_unit_mode_value = 4, 0
        fan_speed_level_value = 4, 0

        # Change Unit Mode to manual
        # self.logger.debug("Pluggit: Start => Change Unit mode to manual: {0}".format(active_unit_mode_value))
        self._Pluggit.write_registers(
            self._modbusRegisterDic['prmRamIdxUnitMode'],
            active_unit_mode_value)
        # self.logger.debug("Pluggit: Finished => Change Unit mode to manual: {0}".format(active_unit_mode_value))

        # wait 100ms before changing fan speed
        # self.logger.debug("Pluggit: Wait 100ms before changing fan speed")
        time.sleep(0.1)

        # Change Fan Speed to highest speed
        # self.logger.debug("Pluggit: Start => Change Fan Speed to Level 4")
        self._Pluggit.write_registers(
            self._modbusRegisterDic['prmRomIdxSpeedLevel'],
            fan_speed_level_value)
        # self.logger.debug("Pluggit: Finished => Change Fan Speed to Level 4")

        # self._refresh()
        # check new active unit mode
        active_unit_mode = self._Pluggit.read_holding_registers(
            self._modbusRegisterDic['prmRamIdxUnitMode'], read_qty=1).getRegister(0)

        if active_unit_mode == 8:
            self.logger.debug("Pluggit: Active Unit Mode: Week program")
        elif active_unit_mode == 4:
            self.logger.debug("Pluggit: Active Unit Mode: Manual")

        # check new fan speed
        fan_speed_level = self._Pluggit.read_holding_registers(
            self._modbusRegisterDic['prmRomIdxSpeedLevel'], read_qty=1).getRegister(0)
        self.logger.debug("Pluggit: Fan Speed: {0}".format(fan_speed_level))

    def _activateWeekProgram(self):

        active_unit_mode_value = 8, 0

        # Change Unit Mode to "Week Program"
        # self.logger.debug("Pluggit: Start => Change Unit mode to 'Week Program': {0}".format(active_unit_mode_value))
        self._Pluggit.write_registers(
            self._modbusRegisterDic['prmRamIdxUnitMode'],
            active_unit_mode_value)
        # self.logger.debug("Pluggit: Finished => Change Unit mode to 'Week Program': {0}".format(active_unit_mode_value))

        # self._refresh()

        # check new active unit mode
        active_unit_mode = self._Pluggit.read_holding_registers(
            self._modbusRegisterDic['prmRamIdxUnitMode'], read_qty=1).getRegister(0)

        if active_unit_mode == 8:
            self.logger.debug("Pluggit: Active Unit Mode: Week program")
        elif active_unit_mode == 4:
            self.logger.debug("Pluggit: Active Unit Mode: Manual")

        # wait 100ms before checking fan speed
        time.sleep(0.1)

        # check new fan speed
        fan_speed_level = self._Pluggit.read_holding_registers(
            self._modbusRegisterDic['prmRomIdxSpeedLevel'], read_qty=1).getRegister(0)
        self.logger.debug("Pluggit: Fan Speed: {0}".format(fan_speed_level))

    def _refresh(self, value=None, trigger=None):
        self.disconnect()
        time.sleep(1)
        self.connect()
        start_time = time.time()
        try:
            # myCounter = 1
            for pluggit_key in self._myTempReadDict:
                # self.logger.debug("Pluggit: ---------------------------------> Wir sind in der Refresh Schleife")
                values = self._modbusRegisterDic[pluggit_key]
                # self.logger.debug("Pluggit: Refresh Schleife: Inhalt von values ist {0}".format(values))
                # 2015-01-07 23:53:08,296 DEBUG    Pluggit      Pluggit:
                # Refresh Schleife: Inhalt von values ist 168 --
                # __init__.py:_refresh:158
                item = self._myTempReadDict[pluggit_key]
                # self.logger.debug("Pluggit: Refresh Schleife: Inhalt von item ist {0}".format(item))
                # 2015-01-07 23:53:08,316 DEBUG    Pluggit      Pluggit:
                # Refresh Schleife: Inhalt von item ist pluggit.unitMode --
                # __init__.py:_refresh:160

                # =======================================================#
                # read values from pluggit via modbus client registers
                # =======================================================#

                # self.logger.debug("Pluggit: ------------------------------------------> Wir sind vor dem Auslesen der Werte")
                registerValue = None
                registerValue = self._Pluggit.read_holding_registers(
                    values, read_qty=1).getRegister(0)
                # self.logger.debug("Pluggit: Read parameter '{0}' with register '{1}': Value is '{2}'".format(pluggit_key, values, registerValue))

                # week program: possible values 0-10
                if values == self._modbusRegisterDic['prmNumOfWeekProgram']:
                    registerValue += 1
                    item(registerValue, trigger=self.get_trigger())
                    # 2015-01-07 23:53:08,435 DEBUG    Pluggit      Item pluggit.unitMode = 8 via Pluggit None None -- item.py:__update:363
                    # self.logger.debug("Pluggit: Week Program Number: {0}".format(registerValue))
                    # 2015-01-07 23:53:08,422 DEBUG    Pluggit      Pluggit:
                    # Active Unit Mode: Week program --
                    # __init__.py:_refresh:177

                # active unit mode
                if values == self._modbusRegisterDic[
                        'prmRamIdxUnitMode'] and registerValue == 8:
                    # self.logger.debug("Pluggit: Active Unit Mode: Week program")
                    item('Woche', trigger=self.get_trigger())
                if values == self._modbusRegisterDic[
                        'prmRamIdxUnitMode'] and registerValue == 4:
                    # self.logger.debug("Pluggit: Active Unit Mode: Manual")
                    item('Manuell', trigger=self.get_trigger())

                # fan speed
                if values == self._modbusRegisterDic['prmRomIdxSpeedLevel']:
                    # self.logger.debug("Pluggit: Fan Speed: {0}".format(registerValue))
                    item(registerValue, trigger=self.get_trigger())

                # remaining filter lifetime
                if values == self._modbusRegisterDic['prmFilterRemainingTime']:
                    # self.logger.debug("Pluggit: Remaining filter lifetime: {0}".format(registerValue))
                    item(registerValue, trigger=self.get_trigger())

                # bypass state
                if values == self._modbusRegisterDic[
                        'prmRamIdxBypassActualState'] and registerValue == 255:
                    # self.logger.debug("Pluggit: Bypass state: opened")
                    item('geöffnet', trigger=self.get_trigger())
                if values == self._modbusRegisterDic[
                        'prmRamIdxBypassActualState'] and registerValue == 0:
                    # self.logger.debug("Pluggit: Bypass state: closed")
                    item('geschlossen', trigger=self.get_trigger())

                # Temperatures
                # Frischluft außen
                if values == self._modbusRegisterDic['prmRamIdxT1']:
                    t1 = self._Pluggit.read_holding_registers(
                        values, 2, unit=22)
                    decodert1 = BinaryPayloadDecoder.fromRegisters(
                        t1.registers, endian=Endian.Big)
                    t1 = decodert1.decode_32bit_float()
                    t1 = round(t1, 2)
                    # self.logger.debug("Pluggit: Frischluft außen: {0:4.1f}".format(t1))
                    # self.logger.debug("Pluggit: Frischluft außen: {0}".format(t1))
                    item(t1, trigger=self.get_trigger())

                # Zuluft innen
                if values == self._modbusRegisterDic['prmRamIdxT2']:
                    t2 = self._Pluggit.read_holding_registers(
                        values, 2, unit=22)
                    decodert2 = BinaryPayloadDecoder.fromRegisters(
                        t2.registers, endian=Endian.Big)
                    t2 = decodert2.decode_32bit_float()
                    t2 = round(t2, 2)
                    # self.logger.debug("Pluggit: Zuluft innen: {0:4.1f}".format(t2))
                    # self.logger.debug("Pluggit: Zuluft innen: {0}".format(t2))
                    item(t2, trigger=self.get_trigger())

                # Abluft innen
                if values == self._modbusRegisterDic['prmRamIdxT3']:
                    t3 = self._Pluggit.read_holding_registers(
                        values, 2, unit=22)
                    decodert3 = BinaryPayloadDecoder.fromRegisters(
                        t3.registers, endian=Endian.Big)
                    t3 = decodert3.decode_32bit_float()
                    t3 = round(t3, 2)
                    # self.logger.debug("Pluggit: Abluft innen: {0:4.1f}".format(t3))
                    # self.logger.debug("Pluggit: Abluft innen: {0}".format(t3))
                    item(t3, trigger=self.get_trigger())

                # Fortluft außen
                if values == self._modbusRegisterDic['prmRamIdxT4']:
                    t4 = self._Pluggit.read_holding_registers(
                        values, 2, unit=22)
                    decodert4 = BinaryPayloadDecoder.fromRegisters(
                        t4.registers, endian=Endian.Big)
                    t4 = decodert4.decode_32bit_float()
                    t4 = round(t4, 2)
                    # self.logger.debug("Pluggit: Fortluft außen: {0:4.1f}".format(t4))
                    # self.logger.debug("Pluggit: Fortluft außen: {0}".format(t4))
                    item(t4, trigger=self.get_trigger())

                # self.logger.debug("Pluggit: ------------------------------------------> Ende der Schleife vor sleep, Durchlauf Nr. {0}".format(myCounter))
                time.sleep(0.1)
                # myCounter += 1

        except Exception as e:
            self.logger.error(
                "Pluggit: something went wrong in the refresh function: {0}".format(e))
            return
        end_time = time.time()
        cycletime = end_time - start_time
        self.logger.debug("Pluggit: cycle took {0} seconds".format(cycletime))
