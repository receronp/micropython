from machine import Pin, SoftI2C
from lilygo_oled import OLED

from micropython import const


# Base class for T3S3
class T3S3Base:
    def __init__(self):
        # Define pins for LED, OLED, and SD card
        # LED
        self.LED = const(37)

        # OLED
        self.OLED_SDA = const(18)
        self.OLED_SCL = const(17)

        # SD
        self.SD_CS = const(13)
        self.SD_MOSI = const(11)
        self.SD_MISO = const(2)
        self.SD_SCLK = const(14)

        # Initialize helpers
        self.create_helpers()

    def create_helpers(self):
        # Initialize LED and OLED
        self.led = Pin(self.LED, Pin.OUT)
        self.i2c = SoftI2C(scl=Pin(self.OLED_SCL), sda=Pin(self.OLED_SDA))
        self.oled = OLED(self.i2c)


# T3S3 class
class T3S3(T3S3Base):
    def __init__(self):
        super().__init__()
