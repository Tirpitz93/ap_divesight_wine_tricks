# Compatibility patches

Patches are referenced by profiles (`[patches]` in `../profiles/*.toml`) and applied by `shim run`
(or `shim apply`) before the app starts. They are **persistent** in the prefix.

| Kind | Where | Applied with | Idempotent |
|---|---|---|---|
| Registry | `reg/*.reg` (REGEDIT4 / "Windows Registry Editor Version 5.00") | `wine reg import` | yes |
| Winetricks verb | name in `patches.winetricks` | `winetricks -q <verb>` | skipped if listed in `$WINEPREFIX/winetricks.log` |
| DLL override | `[dll_overrides]` in the profile | `WINEDLLOVERRIDES` env, per run | not persisted |
| Serial port | `[serial]` in the profile | `dosdevices/comN` symlink + `HKLM\Software\Wine\Ports` | yes |

Rules
- One concern per `.reg` file; header comment says *why* and whether it is verified.
- Every patch that changes app behaviour gets an inverse patch (`*-off.reg`) so profiles can reset state.
- Native DLLs dropped into the app folder are not managed yet: document them here if needed.

Current patches
- `reg/ap-message-level-debug.reg` — raise `Message Level` for both apps (value 3, UNVERIFIED range).
- `reg/ap-message-level-off.reg` — restore the factory value 0.
