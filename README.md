# Galreshield for Toliss A319 in X-Plane #
Self made glareshield for winwing fcu

## Setup ##
 1. `git submodul init`
 2. `cd i2c-ch341-usb`
 3. `make`

## Startup ##
 1. `sudo modprobe i2c-dev`
 1. `sudo insmod i2c-ch341-usb/i2c-ch341-usb.ko`
