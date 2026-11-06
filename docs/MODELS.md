# Optional audio-analysis models

Kulak Kesil works without any model. The models only help in two places:

- **Genre guess** for songs whose artist/title could not be found anywhere
  (built-in list, iTunes, Deezer, YouTube tags, MusicBrainz).
- **Music vs. speech check** for browser streams that carry no usable metadata
  (e.g. a YouTube video with a vague title): is it a song or a talk/video?

Everything runs locally on the CPU (one thread, ~10 ms per window). Audio is held
in memory only for the analysis — it is never written to disk or sent anywhere.

## Install

```bash
./tools/download_models.sh        # downloads + verifies SHA-256
kulak saglik                      # "audio analysis model" line should be ✔
```

You also need `onnxruntime` for the system Python (`/usr/bin/python3`, which the
service runs; a virtualenv is not seen): `python-onnxruntime` on Arch, otherwise
`pip install --user onnxruntime`. Debian 12+ / Ubuntu 23.04+ refuse that by default
(PEP 668, "externally-managed-environment"); there you can add `--break-system-packages`
if you accept that pip installs into your user site next to the distro packages.
The service picks the models up on its next start (`systemctl --user restart oto-eq`).
`./uninstall.sh --purge` deletes them again.

Files go to `${XDG_DATA_HOME:-~/.local/share}/oto-eq/models/`:

| File | Source (Essentia model zoo) | Size | SHA-256 |
|---|---|---|---|
| `discogs-effnet-bsdynamic-1.onnx` + `.json` | `https://essentia.upf.edu/models/feature-extractors/discogs-effnet/` | 18.0 MB | `a280825b334797cf677939db8cd5762c0392aedd0ca6415dbc1cd083f045e43c` |
| `audioset-yamnet-1.onnx` + `.json` (optional helper) | `https://essentia.upf.edu/models/audio-event-recognition/yamnet/` | 14.9 MB | `9bc15ac91426e431527196ee6663de78dffcc7db53ac002d5afbda61429b456f` |

Without YAMNet, speech detection falls back to EffNet's "Non-Music" styles (weaker).

## License — please read

These model files are **not** part of this repository and are **not** covered by
its GPL-3.0 license. They are published by the Music Technology Group (Universitat
Pompeu Fabra) under a **Creative Commons non-commercial** license (see
[essentia.upf.edu/models](https://essentia.upf.edu/models/)). Downloading them for
your own personal use is fine; do not redistribute or sell them. The original
YAMNet model by Google is Apache-2.0, but the ONNX file used here is Essentia's
conversion and comes with Essentia's terms.
