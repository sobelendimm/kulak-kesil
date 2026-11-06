# Contributing

Thanks for helping! Bug reports, distro/desktop test reports and translations are the
most useful contributions right now.

## Reporting a bug

Please include:

- the output of `kulak saglik` and `kulak durum`
- distro, desktop environment, headphone model
- what you expected vs. what happened (`kulak gunluk` shows the live log)

## Pull requests

- Run the tests before sending: `python3 -m unittest discover -s tests`.
  They never write to your real `~/.config`, but a few live tests load temporary null
  sinks into the running PipeWire (skipped without `pactl` / `pipewire-pulse`).
  Test-only dependencies: `scipy` and `jeepney`.
- Keep user-facing text translatable: wrap it in `_()` (Turkish is the source language;
  catalogs live in `oto_eq/i18n/`). Log messages can stay as they are.
- One topic per pull request, please.

## Licensing of contributions

Kulak Kesil is licensed under GPL-3.0-or-later. By submitting a contribution you
confirm that you wrote it (or have the right to submit it), and you agree that:

1. your contribution is licensed under GPL-3.0-or-later, **and**
2. the project owner may also distribute your contribution under other license terms,
   including commercial ones.

Point 2 keeps the project able to offer things like a paid edition or a commercial
license in the future without having to track down every contributor. The GPL-3.0
version stays available.
