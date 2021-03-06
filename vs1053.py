# VS1053 driver for MicroPython
# (C) Peter Hinch 2020
# Released under the MIT licence

# Driver is based on the following sources
# Adafruit https://github.com/adafruit/Adafruit_CircuitPython_VS1053
# Uri Shaked https://github.com/urish/vs1053-circuitpython

import time
import io
import uasyncio as asyncio

__version__ = (0, 1, 0)

# Before setting, the internal clock runs at 12.288MHz. Data P7: "the
# maximum speed for SCI reads is CLKI/7" hence max initial baudrate is
# 12.288/7 = 1.75MHz
_INITIAL_BAUDRATE = const(1_000_000)
# 12.288*3.5/4 = 10MHz for data read (using _SCI_CLOCKF,0x8800)
_DATA_BAUDRATE = const(10_752_000)  # Speed for data transfers. On Pyboard D
# actual rate is 4.5MHz shared with SD card - sdcard.py uses 1.32MHz.
_SCI_BAUDRATE = const(5_000_000)

# SCI Registers
_SCI_MODE = const(0x0)
_SCI_STATUS = const(0x1)
_SCI_BASS = const(0x2)
_SCI_CLOCKF = const(0x3)
_SCI_DECODE_TIME = const(0x4)
# _SCI_AUDATA = const(0x5)
_SCI_WRAM = const(0x6)
_SCI_WRAMADDR= const(0x7)
_SCI_HDAT0 = const(0x8)
_SCI_HDAT1 = const(0x9)
# _SCI_AIADDR = const(0xa)
_SCI_VOL = const(0xb)
# _SCI_AICTRL0 = const(0xc)
# _SCI_AICTRL1 = const(0xd)
# _SCI_AICTRL2 = const(0xe)
# _SCI_AICTRL3 = const(0xf)

# Mode register bits: Public
SM_DIFF = const(0x01)  # Invert left channel (why?)
SM_LAYER12 = const(0x02)  # Enable MPEG
SM_EARSPEAKER_LO = const(0x10)  # EarSpeaker spatial processing
SM_EARSPEAKER_HI = const(0x80)
SM_LINE_IN = const(0x4000)  # Line/Mic in
# Private bits
_SM_RESET = const(0x04)
_SM_CANCEL = const(0x08)
_SM_TESTS = const(0x20)
_SM_SDINEW = const(0x800)
# Unused and private
# _SM_STREAM = const(0x40)
# _SM_DACT = const(0x100)
# _SM_SDIORD = const(0x200)
# _SM_SDISHARE = const(0x400)
# _SM_ADPCM = const(0x1000)
# _SM_ADPCM_HP = const(0x2000)
# _SM_CLK_RANGE = const(0x8000)

# Common parameters section 10.11.1 (RAM locations)
_END_FILL_BYTE = const(0x1e06)
_BYTE_RATE = const(0x1e05)
_POS_MS_LS = const(0x1e27)
_POS_MS_MS = const(0x1e28)
_IO_DIRECTION = const(0xc017)  # Datasheet 11.10
_IO_READ = const(0xc018)
_IO_WRITE = const(0xc019)

# I/O
# MP_STREAM_POLL_RD = const(1)
MP_STREAM_POLL_WR = const(4)
MP_STREAM_POLL = const(3)
MP_STREAM_ERROR = const(-1)

# xcs is chip XSS/
# xdcs is chipXDCS/BSYNC/
# sdcs is SD card CS/
class VS1053(io.IOBase):

    def __init__(self, spi, reset, dreq, xdcs, xcs, sdcs=None, mp=None):
        self._reset = reset
        self._dreq = dreq  # Data request
        self._xdcs = xdcs  # Data CS
        self._xcs = xcs  # Register CS
        self._spi = spi
        self._cbuf = bytearray(4)  # Command buffer
        self.reset()
        if ((sdcs is not None) and (mp is not None)):
            import sdcard
            import os
            sd = sdcard.SDCard(spi, sdcs)
            vfs = os.VfsFat(sd)
            os.mount(vfs, mp)
        self._cancnt = 0  # If >0 cancellation in progress
        self._playing = False
        self._spi.init(baudrate=_DATA_BAUDRATE)
        self._swriter = asyncio.StreamWriter(self, {})

    def _wait_ready(self):
        self._xdcs(1)
        self._xcs(1)
        while not self._dreq():
            pass

    def _write_reg(self, addr, value):  # Datasheet 7.4
        self._wait_ready()
        self._spi.init(baudrate=_SCI_BAUDRATE)
        b = self._cbuf
        b[0] = 2  # WRITE
        b[1] = addr & 0xff
        b[2] = (value >> 8) & 0xff
        b[3] = value & 0xff
        self._xcs(0)
        self._spi.write(b)
        self._xcs(1)
        self._spi.init(baudrate=_DATA_BAUDRATE)

    def _read_reg(self, addr):  # Datasheet 7.4
        self._wait_ready()
        self._spi.init(baudrate=_SCI_BAUDRATE)
        b = self._cbuf
        b[0] = 3  # READ
        b[1] = addr & 0xff
        b[2] = 0xff
        b[3] = 0xff
        self._xcs(0)
        self._spi.write_readinto(b, b)
        self._xcs(1)
        self._spi.init(baudrate=_DATA_BAUDRATE)
        return (b[2] << 8) | b[3]

    def _read_ram(self, addr):
        self._write_reg(_SCI_WRAMADDR, addr)
        return self._read_reg(_SCI_WRAM)

    def _write_ram(self, addr, data):
        self._write_reg(_SCI_WRAMADDR, addr)
        return self._write_reg(_SCI_WRAM, data)

    # Datasheet section 10.5.1: procedure for normal end of play
    async def _end_play(self, buf):
        sw = self._swriter
        efb = self._read_ram(_END_FILL_BYTE) & 0xff
        for n in range(len(buf)):
            buf[n] = efb
        for n in range(65):  # send 2080 bytes of end fill byte
            await sw.awrite(buf)
            if n & 7 == 0:
                await asyncio.sleep_ms(0)
        self.mode_set(_SM_CANCEL)
        for n in range(64):  # send up to 2048 bytes
            await sw.awrite(buf)
            if n & 7 == 0:
                await asyncio.sleep_ms(0)
            if not self.mode() & _SM_CANCEL:
                break
        else:  # Cancel has not been acknowledged
            self.soft_reset()
            return
        if self._read_reg(_SCI_HDAT0) or self._read_reg(_SCI_HDAT1):
            raise RuntimeError('Invalid HDAT value.')

    # IOBase interface
    # Called via .play with a buffer of 32 bytes max. The VS1053
    # can always accept this (because ioct has checked dreq)
    def write(self, buf):
        self._xdcs(0)
        self._spi.write(buf)
        self._xdcs(1)
        return len(buf)

    def ioctl(self, req, arg):
        ret = MP_STREAM_ERROR
        if req == MP_STREAM_POLL:
            ret = 0
            if arg & MP_STREAM_POLL_WR:
                if self._dreq():
                    ret |= MP_STREAM_POLL_WR
        return ret

# *** API ***

    def reset(self):  # Issue hardware reset to VS1053
        self._xcs(1)
        self._xdcs(1)
        self._reset(0)
        time.sleep_ms(20)
        self._reset(1)
        time.sleep_ms(20)
        self.soft_reset()

    def soft_reset(self):
        self._spi.init(baudrate=_INITIAL_BAUDRATE)
        self.mode_set(_SM_RESET)
        # This has many interesting settings data P39
        time.sleep_ms(20)  # Adafruit have a total of 200ms
        while not self._dreq():
            pass
        # Data P42. P7 footnote 4 recommends xtal * 3.5 + 1: using that.
        self._write_reg(_SCI_CLOCKF, 0x8800)
        if self._read_reg(_SCI_CLOCKF) != 0x8800:
            raise OSError('No VS1053 device found.')
        time.sleep_ms(1)  # Clock setting can take 100us
        # Datasheet suggests writing to SPI_BASS. 
        self._write_reg(_SCI_BASS, 0)  # 0 is flat response
        self.volume(0, 0)
        while not self._dreq():
            pass

    # Range is 0 to -63.5 dB
    def volume(self, left, right, powerdown=False):
        bits = [0, 0]
        obits = 0xffff  # powerdown
        if not powerdown:
            for n, l in enumerate((left, right)):
                bits[n] = round(min(max(2 * -l, 0), 127))
            obits = bits[0] << 8 | bits[1]
        self._write_reg(_SCI_VOL, obits)

    def response(self, *, bass_freq=10, treble_freq=1000, bass_amp=0, treble_amp=0):
        bits = 0
        # Treble amplitude in dB range -12..10.5
        ta = round(min(max(treble_amp, -12.0), 10.5) / 1.5) & 0x0f
        bits |= ta << 12
        # Treble freq 1000-15000
        tf = round(min(max(treble_freq, 1000), 15000) / 1000) if ta else 0
        bits |= tf << 8
        # Bass amplitude in dB range 0..15
        ba = round(min(max(bass_amp, 0), 15))
        bits |= ba << 4
        # Bass freq 20Hz-150Hz
        bf = round(min(max(bass_freq, 20), 150) / 10) if ba else 0
        bits |= bf
        self._write_reg(_SCI_BASS, bits)

    def pins_direction(self, bits):
        self._write_ram(_IO_DIRECTION, bits & 0xff)

    def pins(self, data=None):
        if data is not None:
            self._write_ram(_IO_WRITE, data & 0xff)
        return self._read_ram(_IO_READ) & 0x3ff

    def version(self):
        return (self._read_reg(_SCI_STATUS) >> 4) & 0x0F

    def decode_time(self):  # Number of seconds into the stream
        return self._read_reg(_SCI_DECODE_TIME)

    def byte_rate(self):  # Data rate in bytes/sec
        return self._read_ram(_BYTE_RATE)

    def mode(self):
        return self._read_reg(_SCI_MODE)

    def mode_set(self, bits):
        bits |= self.mode() | _SM_SDINEW
        self._write_reg(_SCI_MODE, bits)

    def mode_clear(self, bits):
        bits ^= 0xffff
        bits &= self.mode()
        self._write_reg(_SCI_MODE, _SM_SDINEW | bits)  # Ensure new bit always set

# Doesn't return anything useful for MP3
#    def pos_ms(self):  # Position into stream in ms
#        return self._read_ram(_POS_MS_LS) | (self._read_ram(_POS_MS_MS) << 16)

    async def cancel(self):
        if self._playing:
            self._cancnt = 1  # Request
            while self._cancnt:  # In progress
                await asyncio.sleep_ms(50)

    async def play(self, s, buf = bytearray(32)):
        self._playing = True
        self._cancnt = 0
        cnt = 0
        sw = self._swriter
        while s.readinto(buf):  # Read <=32 bytes
            cnt += 1
            await sw.awrite(buf)
            # Check for cancelling. Datasheet section 10.5.2
            if self._cancnt:
                if self._cancnt == 1:  # Just cancelled
                    self.mode_set(_SM_CANCEL)
                if not self.mode() & _SM_CANCEL:  # Cancel done
                    efb = self._read_ram(_END_FILL_BYTE) & 0xff
                    for n in range(len(buf)):
                        buf[n] = efb
                    for n in range(64):  # send 2048 bytes of end fill byte
                        await sw.awrite(buf)
                        if n & 7 == 0:
                            await asyncio.sleep_ms(0)
                    await sw.awrite(buf[:4])  # Take to 2052 bytes
                    if self._read_reg(_SCI_HDAT0) or self._read_reg(_SCI_HDAT1):
                        raise RuntimeError('Invalid HDAT value.')
                    break
                if self._cancnt > 64:  # Cancel has failed
                    self.soft_reset()
                    break
                self._cancnt += 1  # keep feeding data from stream
        else:
            await self._end_play(buf)
        self._cancnt = 0
        self._playing = False

    # Produce a 517Hz sine wave
    async def sine_test(self, seconds=10):
        sw = self._swriter
        self.soft_reset()
        self.mode_set(_SM_TESTS)
        # 0x66-> Sample rate 22050 * 6/128 = 1034Hz 0x63->517Hz
        await sw.awrite(b'\x53\xef\x6e\x66\0\0\0\0')
        await asyncio.sleep(seconds)
        await sw.awrite(b'\x45\x78\x69\x74\0\0\0\0')
        self.mode_clear(_SM_TESTS)
