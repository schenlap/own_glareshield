#!/usr/bin/env python3
VERSION = "v0.1"

# IP Address of machine running X-Plane. 
UDP_IP = "127.0.0.1"
UDP_PORT = 49000

import binascii
from dataclasses import dataclass
from enum import Enum, IntEnum
import os
import socket
import struct

#for raw usb
import re
import subprocess

from threading import Thread, Event, Lock
from time import sleep

from pcf8575 import PCF8575
from pcf8575 import IOPort

import usb.core
import usb.backend.libusb1
import usb.util

import XPlaneUdp

# TODOLIST
#  * show vertslew_key

BUTTONS_CNT = 99 # TODO


#@unique
class DEVICEMASK(IntEnum):
    NONE =  0
    GLARE_FO =  0x01
    GLARE_CAPT = 0x02

class ButtonType(Enum):
    SWITCH = 0
    TOGGLE = 1
    SEND_0 = 2
    SEND_1 = 3
    SEND_2 = 4
    SEND_3 = 5
    SEND_4 = 6
    SEND_5 = 7
    NONE = 5 # for testing


class Leds(Enum):
    MASTER_WARN_UPPER = 0
    MASTER_WARN_LOWER = 1
    MASTER_CAUTION_LOWER = 5 # ok
    MASTER_CAUTION_UPPER = 3 # ok
    AUTOLAND_UPPER = 4
    #AUTOLAND_LOWER = 2
    FO_UPPER = 6
    FO_LOWER = 7


class DrefType(Enum):
    DATA = 0
    CMD = 1
    NONE = 2 # for testing


@dataclass
class Button:
    id: int
    label: str
    dataref: str = None
    dreftype: DrefType = DrefType.DATA
    type: ButtonType = ButtonType.NONE
    led: Leds = None

values_processed = Event()
xplane_connected = False
buttonlist = []
values = []

led_brightness = 180

device_config = DEVICEMASK.NONE


class Byte(Enum):
    H0 = 0


@dataclass
class Flag:
    name : str
    byte : Byte
    mask : int
    value : bool = False


#flags = dict([("spd", Flag('spd-mach_spd', Byte.H0, 0x01)),
#              ])


def glare_set_leds(device, leds, brightness):
    if isinstance(leds, list):
        for i in range(len(leds)):
            glare_set_led(device, leds[i], brightness)
    else:
        glare_set_led(device, leds, brightness)

def glare_set_led(device, led, brightness):
    #print(f"set led {led.name} to brightness {brightness} on device {device}")
    device.port[15-led.value] = not brightness

    def __init__(self, device):
        self.device = device

    def clear(self):
        for i in range(16):
            self.device.port[i] = 1 # clear all leds


glare_device = None # usb /dev/inputx device

datacache = {}
ledtest = False

# List of datarefs without led connection to request.
# Text Dataref format:  <MCDU[1,2]><Line[title/label/cont/etc]><Linenumber[1...6]><Color[a,b,m,s,w,y]>.
# We must read all 25 Bytes per dataref!
array_datarefs = [
    #("AirbusFBW/MCDU1titleb", None),
    ("AirbusFBW/MCDU1titleg", None),
    ("AirbusFBW/MCDU1titles", None),
    ("AirbusFBW/MCDU1titlew", None),
  ]

datarefs = [
    ("AirbusFBW/AnnunMode", None) # 0 .. dim, 1 .. bright, 2 .. test
  ]

buttons_press_event = [0] * BUTTONS_CNT
buttons_release_event = [0] * BUTTONS_CNT

usb_retry = False

xp = None


def create_button_list_mcdu():
    buttonlist.append(Button(0, "MASTER_WARN", "sim/annunciator/clear_master_warning", DrefType.CMD, ButtonType.TOGGLE))
    buttonlist.append(Button(None, "None", "AirbusFBW/MasterWarn", DrefType.DATA, ButtonType.NONE, [Leds.MASTER_WARN_UPPER, Leds.MASTER_WARN_LOWER]))
    buttonlist.append(Button(3, "MASTER_CAUTION", "sim/annunciator/clear_master_caution", DrefType.CMD, ButtonType.TOGGLE))
    buttonlist.append(Button(None, "None", "AirbusFBW/MasterCaut", DrefType.DATA, ButtonType.NONE, [Leds.MASTER_CAUTION_UPPER, Leds.MASTER_CAUTION_LOWER]))
    #buttonlist.append(Button(None, "None", "TODO", DrefType.DATA, ButtonType.NONE, [Leds.FO_UPPER, Leds.FO_LOWER]))
    #buttonlist.append(Button(None, "None", "TODO", DrefType.DATA, ButtonType.NONE, [Leds.AUTOLAND_UPPER, Leds.AUTOLAND_LOWER]))

def RequestDataRefs(xp, config):
    dataref_cnt = 0
    for idx,b in enumerate(buttonlist):
        datacache[b.dataref] = None
        if b.dreftype != DrefType.CMD and b.led != None:
            print(f"register dataref {b.dataref}")
            xp.AddDataRef(b.dataref, 3)
            dataref_cnt += 1
    #print(f"register array datarefs ", end='' )
    #for d in array_datarefs:
    #    for i in range(PAGE_CHARS_PER_LINE):
    #        freq = d[1]
    #        if freq == None:
    #            freq = 2
    #        xp.AddDataRef(dataref_switch_mcdu(d[0]+'['+str(i)+']', config), freq)
    #        dataref_cnt += 1
    #        if dataref_cnt % 100 == 0:
    #            print(".", end='', flush=True)

    print("")
    for d in datarefs:
        print(f"register dataref {d[0]}")
        datacache[d[0]] = None
        freq = d[1]
        if freq == None:
            freq = 2
        xp.AddDataRef(d[0], freq)
        dataref_cnt += 1
    print(f"registered {dataref_cnt} datarefs")


def xor_bitmask(a, b, bitmask):
    return (a & bitmask) != (b & bitmask)


def glare_button_event():
    #print(f'events: press: {buttons_press_event}, release: {buttons_release_event}')
    for b in buttonlist:

        if not any(buttons_press_event) and not any(buttons_release_event):
            break
        if b.id == None:
            continue
        if buttons_press_event[b.id]:
            buttons_press_event[b.id] = 0

            #print(f'button {b.label} pressed')
            if b.type == ButtonType.TOGGLE:
                val = datacache[b.dataref]
                if b.dreftype== DrefType.DATA:
                    print(f'set dataref {b.dataref} from {bool(val)} to {not bool(val)}')
                    xp.WriteDataRef(b.dataref, not bool(val))
                elif b.dreftype== DrefType.CMD:
                    print(f'send command {b.dataref}')
                    xp.SendCommand(b.dataref)
            elif b.type == ButtonType.SWITCH:
                val = datacache[b.dataref]
                if b.dreftype== DrefType.DATA:
                    print(f'set dataref {b.dataref} to 1')
                    xp.WriteDataRef(b.dataref, 1)
                elif b.dreftype== DrefType.CMD:
                    print(f'send command {b.dataref}')
                    xp.SendCommand(b.dataref)
            elif b.type == ButtonType.SEND_0:
                if b.dreftype== DrefType.DATA:
                    print(f'set dataref {b.dataref} to 0')
                    xp.WriteDataRef(b.dataref, 0)
            elif b.type == ButtonType.SEND_1:
                if b.dreftype== DrefType.DATA:
                    print(f'set dataref {b.dataref} to 1')
                    xp.WriteDataRef(b.dataref, 1)
            elif b.type == ButtonType.SEND_2:
                if b.dreftype== DrefType.DATA:
                    print(f'set dataref {b.dataref} to 2')
                    xp.WriteDataRef(b.dataref, 2)
            elif b.type == ButtonType.SEND_3:
                if b.dreftype== DrefType.DATA:
                    print(f'set dataref {b.dataref} to 3')
                    xp.WriteDataRef(b.dataref, 3)
            elif b.type == ButtonType.SEND_4:
                if b.dreftype== DrefType.DATA:
                    print(f'set dataref {b.dataref} to 4')
                    xp.WriteDataRef(b.dataref, 4)
            elif b.type == ButtonType.SEND_5:
                if b.dreftype== DrefType.DATA:
                    print(f'set dataref {b.dataref} to 5')
                    xp.WriteDataRef(b.dataref, 5)
            else:
                print(f'no known button type for button {b.label}')
        if buttons_release_event[b.id]:
            buttons_release_event[b.id] = 0
            print(f'button {b.label} released')
            if b.type == ButtonType.SWITCH:
                xp.WriteDataRef(b.dataref, 0)


def glare_create_events(usb_mgr):
        global values
        sleep(2) # wait for values to be available
        buttons_last = 0
        while True:
            if not xplane_connected: # wait for x-plane
                sleep(1)
                continue

            set_datacache(usb_mgr, values.copy())
            values_processed.set()
            sleep(0.005)
            #print('#', end='', flush=True) # TEST1: should print many '#' in console
            try:
                data_in = usb_mgr.device.port
                #print(f"data_in: {data_in}")
            except Exception as error:
                print(f' *** continue after usb-in error: {error} ***') # TODO
                sleep(0.5) # TODO remove
                continue
            if len(data_in) != 16:
                print(f'rx data count {len(data_in)} not valid')
                continue
            #print(f"data_in: {data_in}")

            #create button bit-pattern
            buttons = 0
            for i in range(12):
                buttons |= (not data_in[i]) << i
            #print(hex(buttons)) # TEST2: you should see a difference when pressing buttons
            for i in range (BUTTONS_CNT):
                mask = 0x01 << i
                if xor_bitmask(buttons, buttons_last, mask):
                    #print(f"buttons: {format(buttons, "#04x"):^14}")
                    if buttons & mask:
                        buttons_press_event[i] = 1
                    else:
                        buttons_release_event[i] = 1
                    glare_button_event()
            buttons_last = buttons


def set_button_led_lcd(ep, dataref, v):
    global led_brightness
    for b in buttonlist:
        if b.dataref == dataref:
            if b.led == None:
                break
            if v >= 255:
                v = 255
            print(f'led: {b.led}, value: {v}')

            glare_set_leds(ep, b.led, int(v))
            break


def set_datacache(usb_mgr, values):
    global datacache
    global exped_led_state
    global ledtest

    new = False
    for v in values:
        #print(f'cache: v:{v} val:{values[v]}')
        if v == 'AirbusFBW/AnnunMode':
            if int(values[v]) == 2:
                if not ledtest:
                    ledtest = True
                    print(f'ledtest: on')
                    for led in Leds:
                        glare_set_leds(usb_mgr.device, led, 1)
            elif ledtest:
                ledtest = False
                print(f'ledtest: off')
                for led in Leds:
                    glare_set_leds(usb_mgr.device, led, 0)
            continue
        if datacache[v] != int(values[v]):
            new = True
            print(f'cache: v:{v} val:{int(values[v])}')
            datacache[v] = int(values[v])
            set_button_led_lcd(usb_mgr.device, v, int(values[v]))
    if new == True or usb_retry == True:

        #if True:
        #    try: # dataref may not be received already, even when connected
        #        exped_led_state_desired = datacache['AirbusFBW/APVerticalMode'] >= 112
        #    except:
        #        exped_led_state_desired = False
        #    if exped_led_state_desired != exped_led_state:
        #        exped_led_state = exped_led_state_desired
        #        glare_set_led(usb_mgr.device, Leds.EXPED_GREEN, led_brightness * exped_led_state_desired)

        sleep(0.05)


def kb_wait_quit_event():
    print(f"*** Press ENTER to quit this script ***\n")
    while True:
        c = input() # wait for ENTER (not worth to implement kbhit for differnt plattforms, so make it very simple)
        print(f"Exit")
        os._exit(0)


class UsbManager:
    def __init__(self):
        self.device = None
        self.device_config = 0
        self.pcf = None

    def connect_device(self, vid: int, pid: int, i2c_port_num: int, pcf_address: int):
        self.device = PCF8575(i2c_port_num, pcf_address)

        if self.device is None:
            raise RuntimeError("Device not found")

        print("Device connected.")

    def find_device(self):
        device_config = 0
        devlist = [
            {'vid': 0x1a86, 'pid': 0x5512, 'name': 'GLARESHIELD - FO', 'mask': DEVICEMASK.GLARE_FO},
        ]
        
        for d in devlist:
            print(f"searching for {d['name']} ... ", end='')
            device = usb.core.find(idVendor=d['vid'], idProduct=d['pid'])
            if device is not None:
                print(f"found")
                return d['vid'], d['pid'], self.device_config
            else:
                print(f"not found")
        return None, None, 0


def main():
    global xp
    global values, xplane_connected
    global device_config

    usb_mgr = UsbManager()
    vid, pid, device_config = usb_mgr.find_device()

    if pid is None:
        exit(f"No compatible glareshiele device found, quit")
    else:
        usb_mgr.connect_device(vid=vid, pid=pid, i2c_port_num=14, pcf_address=0x20)

    print('compatible with X-Plane 11/12 and all Toliss Airbus')

    #display_mgr = DisplayManager(usb_mgr.device)
    #display_mgr.startupscreen(new_version)

    create_button_list_mcdu()

    usb_event_thread = Thread(target=glare_create_events, args=[usb_mgr])
    usb_event_thread.start()

    kb_quit_event_thread = Thread(target=kb_wait_quit_event)
    kb_quit_event_thread.start()

    xp = XPlaneUdp.XPlaneUdp()
    xp.BeaconData["IP"] = UDP_IP # workaround to set IP and port
    xp.BeaconData["Port"] = UDP_PORT
    xp.UDP_PORT = xp.BeaconData["Port"]
    print(f'waiting for X-Plane to connect on port {xp.BeaconData["Port"]}')
    glare_set_leds(usb_mgr.device, [Leds.MASTER_WARN_UPPER, Leds.MASTER_WARN_LOWER], 1)

    while True:
        if not xplane_connected:
            try:
                xp.AddDataRef("sim/aircraft/view/acf_tailnum")
                values = xp.GetValues()

                print(f"X-Plane connected")
                RequestDataRefs(xp, device_config)
                xp.AddDataRef("sim/aircraft/view/acf_tailnum", 0)
                glare_set_leds(usb_mgr.device, [Leds.MASTER_WARN_UPPER, Leds.MASTER_WARN_LOWER], 0)
                xplane_connected = True
            except XPlaneUdp.XPlaneTimeout:
                glare_set_leds(usb_mgr.device, [Leds.MASTER_WARN_UPPER, Leds.MASTER_WARN_LOWER], 1)
                xplane_connected = False
                sleep(1)
            continue

        try:
            values = xp.GetValues()
            values_processed.wait()
            #print(values)
            #values will be handled in glare_create_events to write to usb only in one thread.
            # see function set_datacache(values)
        except XPlaneUdp.XPlaneTimeout:
            print(f'X-Plane timeout, could not connect on port {xp.BeaconData["Port"]}, waiting for X-Plane')
            glare_set_leds(usb_mgr.device, [Leds.MASTER_WARN_UPPER, Leds.MASTER_WARN_LOWER], 1)
            xplane_connected = False
            sleep(2)

if __name__ == '__main__':
  main() 
