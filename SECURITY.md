# Security policy

## Supported versions

Only the latest `main` receives security fixes.

## Reporting a vulnerability

Please do **not** open a public issue for a security problem.

Report it privately through GitHub:
<https://github.com/sobelendimm/kulak-kesil/security/advisories/new>

Include your distribution, the PipeWire/WirePlumber versions, the output of `kulak saglik`, and the steps
that reproduce the problem. Kulak Kesil is a one-person project; reports are answered as soon as possible
and the fix is credited to the reporter unless they prefer otherwise.

## What counts

- Anything that lets another program or a crafted input (stream or player metadata, a profile or rules
  file, a response from an online genre source) run code, write outside Kulak Kesil's own folders, or
  escape the sandboxing of the systemd user services.
- Microphone or playback audio, or what you are listening to, leaving the machine beyond the documented
  genre lookups.
- The installer or uninstaller touching files that do not belong to Kulak Kesil.

Ordinary bugs (wrong profile, routing, sound quality) belong in normal issues.
