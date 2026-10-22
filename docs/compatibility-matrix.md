# Compatibility Matrix

From `research/DECISIONS.md`, verified on 2026-07-17.

| Component | Version / Contract |
| --- | --- |
| MTProto library | Kurigram 2.2.24, `pyrogram` namespace |
| `.session` format | Pyrogram schema version 3 |
| Python | 3.12+ |
| opentele | 1.15.1 |
| Telethon | 1.44.0 |
| Telegram Desktop for `tdata` prototype | 5.7.2 |

MVP exports Pyrogram `.session`. `tdata` remains feature-flagged by
`EXPORTS_TDATA_ENABLED=false` unless the operator enables and verifies it.
