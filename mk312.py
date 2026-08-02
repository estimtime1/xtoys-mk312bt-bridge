"""
mk312.py -- Driver for the MK-312BT / ErosTek ET-312B over the USB link cable.

Speaks the ET-312B serial "link" protocol:
  * 19200 baud, 8 data bits, no parity, 1 stop bit
  * sync (send 0x00, expect 0x07), then key exchange + XOR encryption
  * read / write of the box's internal memory registers

Protocol reference: buttplug.io "stpihkal" ET-312B docs and the
buttshock-protocol-docs project. This box's registers are a virtual memory
map exposed over the link port.

Run standalone as a connection self-test:

    python mk312.py COM3
"""

from __future__ import annotations

import sys
import time

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover - clearer error at call time
    serial = None


# --- Register map (ET-312B virtual memory addresses) --------------------------
ADDR_ADC_DISABLE = 0x400F   # bit0 = 1 -> ignore front-panel pots (our writes stick)
ADDR_LEVEL_A     = 0x4064   # channel A output level, 0..255
ADDR_LEVEL_B     = 0x4065   # channel B output level, 0..255
ADDR_EXECUTE     = 0x4070   # "box command" execute register (2 bytes: 0x4070/0x4071)
ADDR_MODE        = 0x407B   # current mode number
ADDR_MA_MIN      = 0x4086   # multi-adjust minimum for the active mode
ADDR_MA_MAX      = 0x4087   # multi-adjust maximum for the active mode
ADDR_BATTERY     = 0x4203   # battery level, 0..255
ADDR_MULTIADJUST = 0x420D   # current multi-adjust value
# Frequency parameter block (per channel). The running mode continuously
# recomputes the frequency value, so writing FREQ_VAL alone does NOT hold.
# Clamping min == max == value (plus a static select source) pins it. This
# holds in steady modes (Waves, etc.); sweep-heavy modes (e.g. Intense) are
# designed to override it and will not fully hold.
ADDR_FREQ_A      = 0x40AE   # channel A frequency value, 8..255 (8 = fastest)
ADDR_FREQ_A_MAX  = 0x40AF
ADDR_FREQ_A_MIN  = 0x40B0
ADDR_FREQ_A_SEL  = 0x40B5   # frequency source select (0x00 = static)
ADDR_FREQ_B      = 0x41AE   # channel B frequency value
ADDR_FREQ_B_MAX  = 0x41AF
ADDR_FREQ_B_MIN  = 0x41B0
ADDR_FREQ_B_SEL  = 0x41B5

# Pulse-width parameter block (per channel). Like frequency, the mode modulates
# it unless min==max==value is clamped with a static select. Pinning both
# frequency and width (intensity is already static) gives a truly solid tone.
ADDR_WIDTH_A     = 0x40B7
ADDR_WIDTH_A_MIN = 0x40B8
ADDR_WIDTH_A_MAX = 0x40B9
ADDR_WIDTH_A_SEL = 0x40BE
ADDR_WIDTH_B     = 0x41B7
ADDR_WIDTH_B_MIN = 0x41B8
ADDR_WIDTH_B_MAX = 0x41B9
ADDR_WIDTH_B_SEL = 0x41BE

# Frequency register bounds. The register is an inverse "period": 8 is the
# fastest, larger is slower. The hardware documents NO exact value->Hz formula
# (buttplug/buttshock docs both annotate it "(?!)"); community-reported span is
# roughly 15-330 Hz. FREQ_HZ_K below is an approximate inverse constant only.
FREQ_MIN = 8
FREQ_MAX = 200
FREQ_HZ_K = 2640            # approx: hz ~= K / value (labelling only, not exact)


def freq_value_to_hz(value: int) -> float:
    """Approximate Hz for a frequency register value. NOT hardware-exact."""
    value = max(1, value)
    return FREQ_HZ_K / value

# Values written to ADDR_EXECUTE to switch modes
CMD_EXIT_MENU   = 0x04
CMD_SELECT_MODE = 0x12

# Mode number -> name (standard ET-312B firmware)
MODES = {
    0x76: "Waves",   0x77: "Stroke",  0x78: "Climb",    0x79: "Combo",
    0x7A: "Intense", 0x7B: "Rhythm",  0x7C: "Audio 1",  0x7D: "Audio 2",
    0x7E: "Audio 3", 0x7F: "Split",   0x80: "Random 1", 0x81: "Random 2",
    0x82: "Toggle",  0x83: "Orgasm",  0x84: "Torment",  0x85: "Phase 1",
    0x86: "Phase 2", 0x87: "Phase 3",
}


class MK312Error(Exception):
    pass


class MK312:
    """Encrypted link session with an MK-312BT / ET-312B."""

    def __init__(self, port: str = "COM3", timeout: float = 0.6):
        if serial is None:
            raise MK312Error("pyserial not installed. Run: pip install pyserial")
        self.port = port
        self.timeout = timeout
        self.ser = None
        self.key = None  # XOR key established during handshake

    # ---- connection ----------------------------------------------------------
    def open(self):
        self.ser = serial.Serial(
            self.port, 19200, timeout=self.timeout,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
        try:
            self._handshake()
        except Exception:
            self.ser.close()
            self.ser = None
            self.key = None
            raise

    def close(self):
        """Zero output, re-enable the front-panel pots, then close the port."""
        if self.ser is not None:
            try:
                if self.key is not None and self.ser.is_open:
                    try:
                        self.zero_output()
                        self.write(ADDR_ADC_DISABLE, 0x00)
                    except Exception:
                        pass
            finally:
                if self.ser.is_open:
                    self.ser.close()
        self.ser = None
        self.key = None

    @property
    def connected(self) -> bool:
        return self.ser is not None and self.ser.is_open and self.key is not None

    # ---- handshake / encryption ---------------------------------------------
    def _handshake(self):
        self.key = None
        time.sleep(0.25)                 # let the FTDI adapter settle after open
        self.ser.reset_input_buffer()
        # 1) Sync. The box answers 0x07 whether or not it is already keyed.
        if not self._sync():
            raise MK312Error(
                "No sync (0x07) from box. Check the COM port, link cable, and "
                "that the MK-312 is powered on."
            )
        # 2) Fresh key exchange. Host key 0x00 keeps derivation trivial:
        #    key = flip_nibbles(host_key) ^ box_key ^ 0x55  ->  box_key ^ 0x55.
        #    This only gets a valid reply when the box is NOT already keyed.
        host_key = 0x00
        self.ser.write(bytes([0x2F, host_key, (0x2F + host_key) & 0xFF]))
        resp = self.ser.read(3)
        if len(resp) == 3 and resp[0] == 0x21 and resp[2] == ((0x21 + resp[1]) & 0xFF):
            flipped = ((host_key & 0x0F) << 4) | ((host_key & 0xF0) >> 4)
            self.key = flipped ^ resp[1] ^ 0x55
            return
        # 3) No valid key-exchange reply -> the box is already keyed from a prior
        #    session (these boxes have no soft key-reset). Our 0x2f garbled its
        #    framing, so re-sync to reset it, then adopt this clone's known key.
        #    Replies are plaintext, so a probe read verifies the key blind.
        self.ser.reset_input_buffer()
        self._sync()
        if self._adopt_key(0x55):
            return
        raise MK312Error(
            "Box is keyed with an unknown key. Power-cycle the MK-312 and retry."
        )

    def _sync(self) -> bool:
        """Send unencrypted 0x00 until the box answers 0x07."""
        for _ in range(16):
            self.ser.reset_input_buffer()
            self.ser.write(b"\x00")
            if self.ser.read(1) == b"\x07":
                return True
            time.sleep(0.04)
        return False

    def _adopt_key(self, candidate: int) -> bool:
        """Assume the box is already keyed with `candidate` and verify by a
        probe read. Returns True and keeps the key on success."""
        self.key = candidate
        try:
            self.read(ADDR_MODE)          # succeeds only if the key is right
            return True
        except MK312Error:
            self.key = None
            return False

    def _xor(self, data):
        return bytes(b ^ self.key for b in data)

    # ---- low-level register access ------------------------------------------
    def read(self, addr: int) -> int:
        if self.key is None:
            raise MK312Error("Not handshaked")
        hi, lo = (addr >> 8) & 0xFF, addr & 0xFF
        packet = [0x3C, hi, lo]
        packet.append(sum(packet) & 0xFF)
        self.ser.reset_input_buffer()
        self.ser.write(self._xor(packet))
        # Replies come back in the clear -- only host->box traffic is encrypted.
        raw = self.ser.read(3)
        if len(raw) != 3:
            raise MK312Error(f"Short read reply for ${addr:04x}: {raw!r}")
        if raw[0] != 0x22:
            raise MK312Error(f"Unexpected read reply {raw[0]:#04x} for ${addr:04x}")
        if raw[2] != ((0x22 + raw[1]) & 0xFF):
            raise MK312Error(f"Read checksum mismatch for ${addr:04x}")
        return raw[1]

    def write(self, addr: int, values) -> None:
        if self.key is None:
            raise MK312Error("Not handshaked")
        if isinstance(values, int):
            values = [values]
        hi, lo = (addr >> 8) & 0xFF, addr & 0xFF
        cmd = (((len(values) + 3) & 0x0F) << 4) | 0x0D
        packet = [cmd, hi, lo] + [v & 0xFF for v in values]
        packet.append(sum(packet) & 0xFF)
        self.ser.reset_input_buffer()
        self.ser.write(self._xor(packet))
        raw = self.ser.read(1)                       # ACK is plaintext 0x06
        if len(raw) != 1:
            raise MK312Error(f"No ACK writing ${addr:04x}")
        if raw[0] != 0x06:
            raise MK312Error(f"Bad ACK writing ${addr:04x}: {raw[0]:#04x}")

    # ---- high-level helpers --------------------------------------------------
    def get_state(self) -> dict:
        mode = self.read(ADDR_MODE)
        return {
            "mode": mode,
            "mode_name": MODES.get(mode, f"?({mode:#04x})"),
            "level_a": self.read(ADDR_LEVEL_A),
            "level_b": self.read(ADDR_LEVEL_B),
            "multi_adjust": self.read(ADDR_MULTIADJUST),
            "battery": self.read(ADDR_BATTERY),
        }

    def ma_range(self):
        return self.read(ADDR_MA_MIN), self.read(ADDR_MA_MAX)

    def disable_pots(self, disable: bool = True):
        self.write(ADDR_ADC_DISABLE, 0x01 if disable else 0x00)

    def set_level_a(self, value: int):
        self.write(ADDR_LEVEL_A, value & 0xFF)

    def set_level_b(self, value: int):
        self.write(ADDR_LEVEL_B, value & 0xFF)

    def set_level(self, value: int):
        value &= 0xFF
        self.write(ADDR_LEVEL_A, value)
        self.write(ADDR_LEVEL_B, value)

    def set_multi_adjust(self, value: int):
        self.write(ADDR_MULTIADJUST, value & 0xFF)

    def _pin_frequency(self, sel, mx, mn, val, value):
        """Pin a channel's frequency by clamping min==max==value with a static
        source. Holds in steady modes; sweep modes may still override it."""
        value = max(FREQ_MIN, min(0xFF, value))
        self.write(sel, 0x00)
        self.write(mx, value)
        self.write(mn, value)
        self.write(val, value)

    def set_frequency_a(self, value: int):
        self._pin_frequency(ADDR_FREQ_A_SEL, ADDR_FREQ_A_MAX, ADDR_FREQ_A_MIN,
                            ADDR_FREQ_A, value)

    def set_frequency_b(self, value: int):
        self._pin_frequency(ADDR_FREQ_B_SEL, ADDR_FREQ_B_MAX, ADDR_FREQ_B_MIN,
                            ADDR_FREQ_B, value)

    def set_frequency(self, value: int):
        self.set_frequency_a(value)
        self.set_frequency_b(value)

    def get_frequency(self):
        return self.read(ADDR_FREQ_A), self.read(ADDR_FREQ_B)

    def set_width_a(self, value: int):
        self._pin_frequency(ADDR_WIDTH_A_SEL, ADDR_WIDTH_A_MAX, ADDR_WIDTH_A_MIN,
                            ADDR_WIDTH_A, value)

    def set_width_b(self, value: int):
        self._pin_frequency(ADDR_WIDTH_B_SEL, ADDR_WIDTH_B_MAX, ADDR_WIDTH_B_MIN,
                            ADDR_WIDTH_B, value)

    def set_width(self, value: int):
        """Pin pulse width steady on both channels (stops width modulation)."""
        self.set_width_a(value)
        self.set_width_b(value)

    def zero_output(self):
        self.set_level(0)

    def set_mode(self, mode: int):
        self.write(ADDR_MODE, mode & 0xFF)
        self.write(ADDR_EXECUTE, [CMD_EXIT_MENU, CMD_SELECT_MODE])
        time.sleep(0.02)


def _selftest(port: str):
    print(f"Opening {port} ...")
    box = MK312(port)
    box.open()
    print(f"Handshake OK.  key = {box.key:#04x}")
    print("Current state:")
    for k, v in box.get_state().items():
        print(f"  {k:14}: {v}")
    lo, hi = box.ma_range()
    print(f"  {'MA range':14}: {lo}..{hi}")
    box.close()
    print("Closed. Link works.")


if __name__ == "__main__":
    port_arg = sys.argv[1] if len(sys.argv) > 1 else "COM3"
    try:
        _selftest(port_arg)
    except MK312Error as exc:
        print("ERROR:", exc)
        sys.exit(1)
