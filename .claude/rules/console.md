---
paths:
  - "station/console_log.py"
  - "station/hid.py"
---
# Editing the console tap / HID layer

- ⚠️ **A console read is a report-sized FRAGMENT, not a line** — reassemble across
  reads (`ConsoleTap.feed`) and classify only `\n`-terminated lines.
- ⚠️ **Most firmware diagnostics are gated on `debug_enable`, which defaults FALSE.**
  `Failed to sync …` and `Bridge sync retry` never appear on the rig; the `Split link:`
  summary is deliberately ungated. Check the gate in `bridge_helper.c` before
  designing around any console line.
- ⚠️ **`HIDConsole.stop()`'s join is TIMED**, so it can return with the reader still
  alive. The handle is then **abandoned, not closed** — closing it under a live reader
  is a use-after-free (SIGABRT, exit 134, which once turned green HIL runs red).
- **Lines printed while the device is away are lost and always will be** — the
  firmware drops console output nobody is draining.
