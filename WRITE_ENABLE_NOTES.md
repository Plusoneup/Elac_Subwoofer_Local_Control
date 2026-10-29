# What is verified on hardware

Tested on an ELAC SUB3030, firmware v1.1.956.23, rev.A. Every write is read back and compared.

| Setting | Command | Status |
|---|---|---|
| Volume | `0040` | read + write verified |
| Preset (Flat, Cinema, Music, Night) | `004A` | read + write verified |
| Low-pass (LPF) | `0049` index 0, filter type 4 | read + write verified |
| Phase, 0–360° | `0049` index 1, filter type 7, degrees × 10 | read + write verified |
| PEQ 1–8 | `0049` indices 8–15, filter type 9 | read + write verified |
| Delay, 0–20 ms | `004B`, raw = ms × 10 | read + write verified |
| Auto-On threshold, 1–10 mV | `0023`, one byte = mV | read + write verified |
| Auto-On mode | `0022` | read only (always 0 on the SUB3030) |
| AutoEQ on/off | `0172` | write allowed only when calibrated (`0170` = 1) |
| AutoEQ calibration | `0174` start, `0177` state, `0175` stop, `0048` upload | works; see below |

AutoEQ on the SUB3030: both uploads are acknowledged (`0048 [00 00]` near field, `[00 01]` listening
position); about 3 s later the subwoofer announces `0170 = 1` (calibrated). It keeps the correction
internally and does not send the values back. Repeating the test signal several times per step
before one upload works.

BLE writes longer than 20 bytes are sent in 20-byte pieces, 50 ms apart.

Writes are off until enabled in the header, each batch is confirmed, demo mode never writes.
