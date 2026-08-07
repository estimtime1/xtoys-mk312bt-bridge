"""
xtoys_bridge.py -- XToys -> MK-312BT bridge.

XToys's "Audio E-Stim" block renders stereo audio (per channel: amplitude =
intensity, tone frequency = the Left/Right Freq setting). This bridge WASAPI-
loopback-captures that audio, extracts per-channel intensity + tone frequency,
and drives the MK-312BT over the Link (serial / COM port) using mk312.py --
Level A/B from intensity, Freq A/B mapped from the XToys tone.

    pip install pyserial soundcard numpy keyboard
    python xtoys_bridge.py
"""

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
import soundcard as sc

try:
    import keyboard
except ImportError:
    keyboard = None

from mk312 import MK312, MK312Error, FREQ_MIN, FREQ_MAX, FREQ_HZ_K, freq_value_to_hz

DEFAULT_PORT = "COM3"
RATE = 48000
BLOCK = 512              # ~10.7 ms analysis window
OUTPUT_MS = 20          # level write rate (~50 Hz)
FREQ_MS = 120           # frequency write cadence (writes are costly)
GUI_MS = 120            # readout refresh
RMS_FULL = 0.707        # RMS of a full-scale sine = 100% intensity
SMOOTH = 0.15           # intensity EMA (~60 ms) to kill RMS ripple -> steady hold
WIDTH_SOLID = 160
MODE_WAVES = 0x76
INT_GATE = 0.02         # below this intensity, hold frequency (no clean tone)

# defaults for the frequency mapping (all editable in the UI)
DEF_XT_MIN, DEF_XT_MAX = 400.0, 1000.0     # XToys audio-tone range (Hz)
DEF_312_MIN, DEF_312_MAX = 15.0, 330.0     # 312 pulse-rate range (Hz)


def rms(x):
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def dominant_hz(x, rate):
    if rms(x) < 1e-4:
        return 0.0
    w = x * np.hanning(len(x))
    mag = np.abs(np.fft.rfft(w))
    k = int(np.argmax(mag))
    if 0 < k < len(mag) - 1:                 # parabolic interpolation
        a, b, c = mag[k - 1], mag[k], mag[k + 1]
        d = a - 2 * b + c
        if abs(d) > 1e-12:
            k = k + 0.5 * (a - c) / d
    return float(k * rate / len(x))


class Analyzer(threading.Thread):
    """Loopback-captures a device and continuously publishes per-channel
    intensity (0..1) and tone frequency (Hz)."""

    def __init__(self, device_name, gain=1.0):
        super().__init__(daemon=True)
        self.device_name = device_name
        self.gain = gain
        self.running = False
        self.error = None
        self.int_a = self.int_b = 0.0
        self.hz_a = self.hz_b = 0.0

    def run(self):
        self.running = True
        try:
            mic = sc.get_microphone(self.device_name, include_loopback=True)
            with mic.recorder(samplerate=RATE, channels=2, blocksize=BLOCK) as rec:
                while self.running:
                    data = rec.record(numframes=BLOCK)
                    left, right = data[:, 0], data[:, 1]
                    raw_a = min(1.0, rms(left) / RMS_FULL * self.gain)
                    raw_b = min(1.0, rms(right) / RMS_FULL * self.gain)
                    self.int_a += SMOOTH * (raw_a - self.int_a)   # EMA smoothing
                    self.int_b += SMOOTH * (raw_b - self.int_b)
                    ha, hb = dominant_hz(left, RATE), dominant_hz(right, RATE)
                    if ha > 0:
                        self.hz_a = ha
                    if hb > 0:
                        self.hz_b = hb
        except Exception as exc:               # pylint: disable=broad-except
            self.error = f"{type(exc).__name__}: {exc}"
            self.running = False

    def stop(self):
        self.running = False


class Bridge:
    def __init__(self, root):
        self.root = root
        self.box = None
        self.analyzer = None
        self.target_a = self.target_b = 0.0        # desired level (float register)
        self._last_wa = self._last_wb = -1         # last written level (write-on-change)
        self._last_freg_a = self._last_freg_b = None
        self.key_queue = queue.Queue()
        root.title("XToys → MK-312BT Bridge")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if keyboard is not None:
            try:
                keyboard.add_hotkey("esc", lambda: self.key_queue.put("esc"))
            except Exception:
                pass
        else:
            root.bind("<Escape>", lambda _e: self.panic())
        self._build()
        self._set_connected(False)
        self.output_tick()
        self.freq_tick()
        self.gui_tick()

    # ---------- layout --------------------------------------------------------
    def _build(self):
        pad = dict(padx=5, pady=3)

        src = ttk.LabelFrame(self.root, text="Audio source  (device XToys outputs to)")
        src.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 3))
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(src, textvariable=self.device_var, width=42,
                                       state="readonly")
        self.device_box.grid(row=0, column=0, **pad)
        ttk.Button(src, text="↻", width=3, command=self._refresh_devices).grid(row=0, column=1)
        self._refresh_devices()

        conn = ttk.Frame(self.root)
        conn.grid(row=1, column=0, sticky="ew", padx=8, pady=3)
        ttk.Label(conn, text="COM:").grid(row=0, column=0, **pad)
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        ttk.Entry(conn, textvariable=self.port_var, width=7).grid(row=0, column=1, **pad)
        self.connect_btn = ttk.Button(conn, text="Connect", command=self.toggle_connect)
        self.connect_btn.grid(row=0, column=2, **pad)
        self.status_var = tk.StringVar(value="● disconnected")
        self.status_lbl = ttk.Label(conn, textvariable=self.status_var, foreground="#b00")
        self.status_lbl.grid(row=0, column=3, **pad)

        live = ttk.LabelFrame(self.root, text="Live  (XToys → 312)")
        live.grid(row=2, column=0, sticky="ew", padx=8, pady=3)
        self.live = {}
        cols = [("", "XToys in"), ("312", "→ 312 out")]
        ttk.Label(live, text="").grid(row=0, column=0)
        ttk.Label(live, text="XToys intensity / tone", foreground="#555").grid(row=0, column=1, **pad)
        ttk.Label(live, text="312 level / freq", foreground="#555").grid(row=0, column=2, **pad)
        for i, ch in enumerate(("Left (A)", "Right (B)")):
            ttk.Label(live, text=ch).grid(row=i + 1, column=0, sticky="e", **pad)
            v_in = tk.StringVar(value="-")
            v_out = tk.StringVar(value="-")
            self.live[ch] = (v_in, v_out)
            ttk.Label(live, textvariable=v_in, width=18, anchor="w").grid(row=i + 1, column=1, sticky="w", **pad)
            ttk.Label(live, textvariable=v_out, width=20, anchor="w").grid(row=i + 1, column=2, sticky="w", **pad)

        mx = ttk.LabelFrame(self.root, text="Maximum  (safety cap, % of 312 output)")
        mx.grid(row=3, column=0, sticky="ew", padx=8, pady=3)
        self.max_a_var = tk.IntVar(value=100)
        self.max_b_var = tk.IntVar(value=100)
        ttk.Label(mx, text="Left:").grid(row=0, column=0, **pad)
        tk.Scale(mx, from_=0, to=100, orient="horizontal", length=150,
                 variable=self.max_a_var).grid(row=0, column=1, **pad)
        ttk.Label(mx, text="Right:").grid(row=0, column=2, **pad)
        tk.Scale(mx, from_=0, to=100, orient="horizontal", length=150,
                 variable=self.max_b_var).grid(row=0, column=3, **pad)

        adv = ttk.LabelFrame(self.root, text="Mapping")
        adv.grid(row=4, column=0, sticky="ew", padx=8, pady=3)
        self.gain_var = tk.DoubleVar(value=1.0)
        self.xt_min_var = tk.DoubleVar(value=DEF_XT_MIN)
        self.xt_max_var = tk.DoubleVar(value=DEF_XT_MAX)
        self.hz_min_var = tk.DoubleVar(value=DEF_312_MIN)
        self.hz_max_var = tk.DoubleVar(value=DEF_312_MAX)
        for c, (lbl, var, w) in enumerate([
            ("Input gain", self.gain_var, 5),
            ("XToys Hz min", self.xt_min_var, 6), ("max", self.xt_max_var, 6),
            ("312 Hz min", self.hz_min_var, 5), ("max", self.hz_max_var, 5),
        ]):
            ttk.Label(adv, text=lbl + ":").grid(row=0, column=2 * c, sticky="e", padx=(6, 1), pady=3)
            ttk.Entry(adv, textvariable=var, width=w).grid(row=0, column=2 * c + 1, padx=(0, 4), pady=3)

        self.panic_btn = tk.Button(self.root, text="PANIC — ZERO OUTPUT  (Esc)",
                                   command=self.panic, bg="#c0392b", fg="white",
                                   activebackground="#e74c3c", font=("Segoe UI", 11, "bold"))
        self.panic_btn.grid(row=5, column=0, sticky="ew", padx=8, pady=(4, 10))

    def _refresh_devices(self):
        try:
            mics = [m.name for m in sc.all_microphones(include_loopback=True)]
        except Exception:
            mics = []
        self.device_box.configure(values=mics)
        if mics and not self.device_var.get():
            self.device_var.set(mics[0])

    def _set_connected(self, on):
        self.connect_btn.configure(text="Disconnect" if on else "Connect")
        self.status_var.set("● connected" if on else "● disconnected")
        self.status_lbl.configure(foreground="#0a0" if on else "#b00")
        state = "normal" if on else "disabled"
        self.panic_btn.configure(state=state)
        if not on:
            for v_in, v_out in self.live.values():
                v_in.set("-")
                v_out.set("-")

    # ---------- connection ----------------------------------------------------
    def toggle_connect(self):
        if self.box and self.box.connected:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        dev = self.device_var.get().strip()
        if not dev:
            messagebox.showerror("No audio device", "Pick the device XToys outputs to.")
            return
        try:
            self.box = MK312(self.port_var.get().strip())
            self.box.open()
            self.box.disable_pots(True)
            self.box.zero_output()
            self.box.set_mode(MODE_WAVES)
            time.sleep(0.05)
            self.box.disable_pots(True)
            self.box.zero_output()
            self.box.set_width(WIDTH_SOLID)      # solid base; freq set live
        except MK312Error as exc:
            self.box = None
            messagebox.showerror("312 connect failed", str(exc))
            return
        except Exception as exc:
            self.box = None
            messagebox.showerror("312 connect failed", f"{type(exc).__name__}: {exc}")
            return
        self.target_a = self.target_b = 0.0
        self._last_wa = self._last_wb = -1
        self._last_freg_a = self._last_freg_b = None
        try:
            gain = max(0.1, self.gain_var.get())
        except tk.TclError:
            gain = 1.0
        self.analyzer = Analyzer(dev, gain=gain)
        self.analyzer.start()
        self._set_connected(True)

    def disconnect(self):
        if self.analyzer:
            self.analyzer.stop()
            self.analyzer = None
        if self.box:
            try:
                self.box.close()
            except Exception:
                pass
        self.box = None
        self._set_connected(False)

    # ---------- output engine -------------------------------------------------
    def _fail(self, exc):
        self.disconnect()
        messagebox.showerror("Link lost", str(exc))

    def output_tick(self):
        # pull latest intensity, cap by Maximum, write the two channels' levels
        if self.box and self.box.connected and self.analyzer:
            if self.analyzer.error:
                err = self.analyzer.error
                self.disconnect()
                messagebox.showerror("Audio capture failed", err)
                self.root.after(OUTPUT_MS, self.output_tick)
                return
            self.analyzer.gain = max(0.1, self._safe(self.gain_var, 1.0))
            la = min(self.analyzer.int_a * 100.0, self.max_a_var.get())
            lb = min(self.analyzer.int_b * 100.0, self.max_b_var.get())
            self.target_a = la * 2.55
            self.target_b = lb * 2.55
            va = round(self.target_a)     # steady hold: rounded level, held (no toggling)
            vb = round(self.target_b)
            try:
                if va != self._last_wa:
                    self.box.set_level_a(va)
                    self._last_wa = va
                if vb != self._last_wb:
                    self.box.set_level_b(vb)
                    self._last_wb = vb
            except Exception as exc:
                self._fail(exc)
                return
        self.root.after(OUTPUT_MS, self.output_tick)

    def _map_freq(self, xt_hz):
        if xt_hz <= 0:
            return None
        xt_min, xt_max = self._safe(self.xt_min_var, DEF_XT_MIN), self._safe(self.xt_max_var, DEF_XT_MAX)
        hz_min, hz_max = self._safe(self.hz_min_var, DEF_312_MIN), self._safe(self.hz_max_var, DEF_312_MAX)
        t = (xt_hz - xt_min) / max(1.0, xt_max - xt_min)
        t = max(0.0, min(1.0, t))
        hz312 = hz_min + t * (hz_max - hz_min)
        reg = round(FREQ_HZ_K / max(1.0, hz312))
        return max(FREQ_MIN, min(FREQ_MAX, reg))

    def freq_tick(self):
        if self.box and self.box.connected and self.analyzer:
            try:
                if self.analyzer.int_a >= INT_GATE:
                    reg = self._map_freq(self.analyzer.hz_a)
                    if reg is not None and reg != self._last_freg_a:
                        self.box.set_frequency_a(reg)
                        self._last_freg_a = reg
                if self.analyzer.int_b >= INT_GATE:
                    reg = self._map_freq(self.analyzer.hz_b)
                    if reg is not None and reg != self._last_freg_b:
                        self.box.set_frequency_b(reg)
                        self._last_freg_b = reg
            except Exception as exc:
                self._fail(exc)
                return
        self.root.after(FREQ_MS, self.freq_tick)

    # ---------- readout / keys ------------------------------------------------
    def gui_tick(self):
        while not self.key_queue.empty():
            try:
                if self.key_queue.get_nowait() == "esc":
                    self.panic()
            except queue.Empty:
                break
        if self.box and self.box.connected and self.analyzer:
            a = self.analyzer
            self.live["Left (A)"][0].set(f"{a.int_a * 100:5.1f}%   {a.hz_a:5.0f} Hz")
            self.live["Right (B)"][0].set(f"{a.int_b * 100:5.1f}%   {a.hz_b:5.0f} Hz")
            self.live["Left (A)"][1].set(
                f"L {self._last_wa if self._last_wa >= 0 else 0}   "
                f"{freq_value_to_hz(self._last_freg_a) if self._last_freg_a else 0:.0f} Hz")
            self.live["Right (B)"][1].set(
                f"L {self._last_wb if self._last_wb >= 0 else 0}   "
                f"{freq_value_to_hz(self._last_freg_b) if self._last_freg_b else 0:.0f} Hz")
        self.root.after(GUI_MS, self.gui_tick)

    def _safe(self, var, default):
        try:
            return float(var.get())
        except (tk.TclError, ValueError):
            return default

    # ---------- safety --------------------------------------------------------
    def panic(self):
        self.target_a = self.target_b = 0.0
        if self.box and self.box.connected:
            try:
                self.box.zero_output()
            except Exception:
                pass

    def on_close(self):
        if keyboard is not None:
            try:
                keyboard.remove_all_hotkeys()
            except Exception:
                pass
        self.disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    Bridge(root)
    root.mainloop()


if __name__ == "__main__":
    main()
