"""install.sh / uninstall.sh in a fake HOME with stub tools (nothing real is touched).

Most cases use --dry-run and read the generated files from its output; the few real
runs stop at a check before anything is changed (that is what they test).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARK = "# oto-eq wrapper (install.sh)"
UNITS = ("oto-eq.service", "oto-eq-sinks.service", "oto-eq-mic.service")


def blocks(out: str) -> dict[str, str]:
    """{path: content} of every '[dry-run] write PATH (mode M):' block in a dry-run."""
    files, cur = {}, None
    for ln in out.splitlines():
        if ln.startswith("[dry-run] write ") and ln.endswith("):"):
            cur = ln[len("[dry-run] write "):ln.rindex(" (mode ")]
            files[cur] = []
        elif cur is not None and ln.startswith("           | "):
            files[cur].append(ln[len("           | "):])
        elif cur is not None and ln == "           |":
            files[cur].append("")
        else:
            cur = None
    return {k: "\n".join(v) + "\n" for k, v in files.items()}


def directives(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for ln in text.splitlines():
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            out.setdefault(k, []).append(v)
    return out


class ScriptCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="oto-eq-kurulum-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.stubs = self.tmp / "stubs"
        self.stubs.mkdir()
        self.log = self.tmp / "calls.log"
        self.stub("systemctl", 'case "$*" in "--user show-environment") '
                               '[ -n "$MGR_ENV" ] && printf "%s\\n" "$MGR_ENV" ;; esac; exit 0')
        self.stub("pactl", 'case "$1" in info) echo "Server Name: ${SERVER:-PulseAudio (on PipeWire 1.0)}" ;; '
                           'get-default-source) echo "alsa_input.x" ;; *) exit 0 ;; esac')
        for t in ("gio", "kbuildsycoca6", "setsid", "pkill"):
            self.stub(t, "exit 0")

    def stub(self, name: str, body: str, where: Path | None = None) -> None:
        f = (where or self.stubs) / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> "{self.log}"\n{body}\n')
        f.chmod(0o755)

    def home(self, name: str = "home") -> Path:
        h = self.tmp / name
        h.mkdir(parents=True, exist_ok=True)
        return h

    def run_script(self, script: str, home: Path, *args: str, path: str | None = None,
                   src: Path = ROOT, env: dict | None = None) -> subprocess.CompletedProcess:
        e = {"HOME": str(home), "LANG": "C.UTF-8", "XDG_RUNTIME_DIR": str(home),
             "PATH": path or f"{self.stubs}:/usr/bin:/bin", **(env or {})}
        return subprocess.run(["bash", str(src / script), *args], env=e,
                              capture_output=True, text=True, timeout=120)

    def dry(self, home: Path, *args: str, **kw) -> tuple[str, dict[str, str]]:
        r = self.run_script("install.sh", home, "--dry-run", *args, **kw)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout + r.stderr, blocks(r.stdout)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


class TestUnitPaths(ScriptCase):
    def test_default_layout_installs_templates_unchanged(self):
        home = self.home()
        (home / ".local/src").mkdir(parents=True)
        (home / ".local/src/oto-eq").symlink_to(ROOT)       # the documented location
        _out, files = self.dry(home, src=home / ".local/src/oto-eq")
        for u in UNITS:
            got = files[f"{home}/.config/systemd/user/{u}"]
            self.assertEqual(got, (ROOT / "systemd" / u).read_text(encoding="utf-8"), u)

    def test_source_elsewhere_fills_in_real_paths(self):
        home = self.home("ev dizini")           # a space in $HOME as well
        out, files = self.dry(home)              # source = ROOT, not ~/.local/src/oto-eq
        self.assertIn("the systemd services will use", out)
        router = directives(files[f"{home}/.config/systemd/user/oto-eq.service"])
        self.assertEqual(router["WorkingDirectory"], [str(ROOT)])
        self.assertIn(f'"PYTHONPATH={ROOT}"', router["Environment"])
        self.assertEqual(router["ExecStart"], ["/usr/bin/python3 -m oto_eq.router"])
        for u, conf in (("oto-eq-sinks.service", "oto-eq-sinks.conf"),
                        ("oto-eq-mic.service", "mikrofon.conf")):
            d = directives(files[f"{home}/.config/systemd/user/{u}"])
            path = f"{home}/.config/oto-eq/{conf}"
            self.assertEqual(d["ConditionPathExists"], [path])
            self.assertEqual(d["ExecStart"], [f'/usr/bin/pipewire -c "{path}"'])
        for text in (files[f"{home}/.config/systemd/user/{u}"] for u in UNITS):
            self.assertNotIn("%h/", text)

    def test_systemd_escaping(self):
        home = self.home('a b%c"d$e')
        _out, files = self.dry(home)
        sinks = directives(files[f"{home}/.config/systemd/user/oto-eq-sinks.service"])
        esc = str(home).replace("%", "%%")
        self.assertEqual(sinks["ConditionPathExists"], [f"{esc}/.config/oto-eq/oto-eq-sinks.conf"])
        q = esc.replace('"', '\\"').replace("$", "$$")
        self.assertEqual(sinks["ExecStart"], [f'/usr/bin/pipewire -c "{q}/.config/oto-eq/oto-eq-sinks.conf"'])

    def test_custom_xdg_config_home(self):
        home = self.home()
        cfg = home / "ayarlar"
        _out, files = self.dry(home, env={"XDG_CONFIG_HOME": str(cfg)})
        # the user manager does not know this XDG_CONFIG_HOME: units go where it looks,
        # and the router is told about the config dir
        router = directives(files[f"{home}/.config/systemd/user/oto-eq.service"])
        self.assertIn(f'"XDG_CONFIG_HOME={cfg}"', router["Environment"])
        sinks = directives(files[f"{home}/.config/systemd/user/oto-eq-sinks.service"])
        self.assertEqual(sinks["ConditionPathExists"], [f"{cfg}/oto-eq/oto-eq-sinks.conf"])
        self.assertIn(f"{cfg}/autostart/oto-eq-tray.desktop", files)

    def test_manager_xdg_config_home(self):
        home = self.home()
        cfg = home / "ayarlar"
        _out, files = self.dry(home, env={"XDG_CONFIG_HOME": str(cfg),
                                          "MGR_ENV": f"XDG_CONFIG_HOME={cfg}"})
        router = directives(files[f"{cfg}/systemd/user/oto-eq.service"])
        self.assertFalse(any("XDG_CONFIG_HOME" in v for v in router["Environment"]))


class TestGeneratedFiles(ScriptCase):
    def test_wrapper_and_desktop_quoting(self):
        home = self.home("a b'c")
        _out, files = self.dry(home)
        wrapper = files[f"{home}/.local/bin/kulak"]
        sq = "'" + str(ROOT).replace("'", "'\\''") + "'"
        self.assertIn(f"exec env PYTHONPATH={sq} /usr/bin/python3 -m oto_eq.cli", wrapper)
        app = files[f"{home}/.local/share/applications/oto-eq.desktop"]
        self.assertIn(f'Exec="{home}/.local/bin/kulak-app"', app.splitlines())
        self.assertIn("X-OtoEQ-Installed=true", app)

    def test_desktop_entries_english_with_turkish_keys(self):
        _out, files = self.dry(self.home())
        app = next(v for k, v in files.items() if k.endswith("applications/oto-eq.desktop"))
        d = directives(app)
        self.assertEqual(d["GenericName"], ["Smart Equalizer"])
        self.assertEqual(d["GenericName[tr]"], ["Akıllı Ekolayzır"])
        self.assertEqual(d["Comment"], ["Automatic per-app headphone EQ for PipeWire"])
        tray = directives(next(v for k, v in files.items() if k.endswith("oto-eq-tray.desktop")))
        self.assertEqual(tray["Name"], ["Kulak Kesil Tray"])
        self.assertEqual(tray["Name[tr]"], ["Kulak Kesil Tepsisi"])
        self.assertEqual(tray["Comment"], ["Headphone EQ status in the system tray"])

    def test_unit_descriptions_are_english(self):
        for u in UNITS:
            desc = directives((ROOT / "systemd" / u).read_text(encoding="utf-8"))["Description"][0]
            self.assertTrue(desc.isascii(), desc)

    def test_update_keeps_removed_autostart_and_shortcut_off(self):
        home = self.home()
        (home / ".local/bin").mkdir(parents=True)
        (home / ".local/bin/kulak-tepsi").write_text(f"#!/bin/sh\n{MARK}\n")   # earlier install
        desk = home / "Desktop"
        desk.mkdir()
        self.stub("xdg-user-dir", f'echo "{desk}"')
        out, files = self.dry(home)
        self.assertNotIn(f"{home}/.config/autostart/oto-eq-tray.desktop", files)
        self.assertNotIn(f"{desk}/Kulak Kesil.desktop", files)
        self.assertIn(f"{home}/.local/share/applications/oto-eq.desktop", files)  # menu entry stays
        self.assertEqual(out.count("not added again"), 2)

    def test_first_install_adds_autostart_and_shortcut(self):
        home = self.home()
        desk = home / "Desktop"
        desk.mkdir()
        self.stub("xdg-user-dir", f'echo "{desk}"')
        _out, files = self.dry(home)
        self.assertIn(f"{home}/.config/autostart/oto-eq-tray.desktop", files)
        self.assertIn(f"{desk}/Kulak Kesil.desktop", files)


class TestChecksBeforeChanges(ScriptCase):
    def assert_nothing_changed(self, home: Path, keep: tuple[str, ...] = ()):
        for d in (".config/oto-eq", ".config/systemd", ".config/autostart", ".local/bin",
                  ".local/share/applications"):
            if d not in keep:
                self.assertFalse((home / d).exists(), d)
        self.assertFalse(any(c.startswith("systemctl --user") and "show-environment" not in c
                             for c in self.calls()), self.calls())

    def test_foreign_command_stops_before_anything(self):
        home = self.home()
        (home / ".local/bin").mkdir(parents=True)
        (home / ".local/bin/kulak").write_text("#!/bin/sh\necho someone else\n")
        r = self.run_script("install.sh", home)
        self.assertEqual(r.returncode, 1)
        self.assertIn("exists and is not ours", r.stderr)
        self.assert_nothing_changed(home, keep=(".local/bin",))
        self.assertEqual(sorted(p.name for p in (home / ".local/bin").iterdir()), ["kulak"])
        self.assertIn("someone else", (home / ".local/bin/kulak").read_text())

    def test_pulseaudio_server_is_rejected(self):
        home = self.home()
        r = self.run_script("install.sh", home, env={"SERVER": "pulseaudio"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("the sound server is not PipeWire (pulseaudio)", r.stderr)
        self.assert_nothing_changed(home)

    def test_numpy_is_required(self):
        # hide numpy from /usr/bin/python3 through a usercustomize import blocker
        ub = self.tmp / "userbase"
        ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
        r = subprocess.run(["/usr/bin/python3", "-c", "import sys; print('python%d.%d' % sys.version_info[:2])"],
                           capture_output=True, text=True)
        ver = r.stdout.strip() or ver
        site = ub / "lib" / ver / "site-packages"
        site.mkdir(parents=True)
        (site / "usercustomize.py").write_text(
            "import sys\n"
            "class _B:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] == 'numpy':\n"
            "            raise ModuleNotFoundError(name)\n"
            "sys.meta_path.insert(0, _B())\n")
        env = {"PYTHONUSERBASE": str(ub)}
        probe = subprocess.run(["/usr/bin/python3", "-c", "import numpy"], env={**os.environ, **env},
                               capture_output=True)
        if probe.returncode == 0:
            self.skipTest("user site disabled: cannot hide numpy")
        home = self.home()
        r = self.run_script("install.sh", home, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("required dependency missing: Python module numpy not found", r.stderr)
        self.assertIn("nothing was installed", r.stderr)
        self.assert_nothing_changed(home)

    def test_root_is_refused(self):
        if not shutil.which("unshare") or subprocess.run(
                ["unshare", "-r", "true"], capture_output=True).returncode != 0:
            self.skipTest("no user namespaces")
        home = self.home()
        for script in ("install.sh", "uninstall.sh"):
            r = subprocess.run(["unshare", "-r", "bash", str(ROOT / script), "--dry-run"],
                               env={"HOME": str(home), "LANG": "C.UTF-8",
                                    "PATH": f"{self.stubs}:/usr/bin:/bin"},
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 1, script)
            self.assertIn("do not run as root", r.stderr)


class TestDistroHints(ScriptCase):
    def shadow_path_without_pactl(self) -> str:
        shadow = self.tmp / "shadow"
        shadow.mkdir()
        for tool in ("bash", "dirname", "cat", "sed", "grep", "date", "id", "mkdir", "env",
                     "chmod", "mv", "pw-cli", "pw-dump", "pw-metadata"):
            w = shutil.which(tool)
            if w:
                (shadow / tool).symlink_to(w)
        os.unlink(self.stubs / "pactl")
        return f"{self.stubs}:{shadow}"

    def test_pactl_hint_per_family(self):
        path = self.shadow_path_without_pactl()
        expect = {"ID=fedora\n": "Fedora:        sudo dnf install pipewire-pulseaudio pulseaudio-utils",
                  "ID=ubuntu\nID_LIKE=debian\n": "Debian/Ubuntu: sudo apt install pipewire-pulse pulseaudio-utils",
                  "ID=cachyos\nID_LIKE=arch\n": "Arch:          sudo pacman -S --needed pipewire-pulse"}
        for i, (osr, line) in enumerate(expect.items()):
            f = self.tmp / f"os-release-{i}"
            f.write_text(osr)
            out, _ = self.dry(self.home(f"h{i}"), path=path, env={"OTO_EQ_OS_RELEASE": str(f)})
            self.assertIn("pactl not found", out)
            self.assertIn(line, out)


class TestUninstall(ScriptCase):
    def test_purge_deletes_models_keeps_backups(self):
        home = self.home()
        data = home / ".local/share/oto-eq"
        (data / "models").mkdir(parents=True)
        (data / "yedekler").mkdir()
        (home / ".config/oto-eq").mkdir(parents=True)
        r = self.run_script("uninstall.sh", home, "--dry-run", "--purge")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("[dry-run]")]
        self.assertTrue(any("rm -rf" in ln and ln.endswith("/oto-eq/models") for ln in lines), lines)
        self.assertFalse(any("yedekler" in ln for ln in lines))
        self.assertIn(f"Backups kept: {data}/yedekler", r.stdout)

    def test_units_found_in_manager_and_shell_dirs(self):
        home = self.home()
        cfg = home / "ayarlar"
        for d in (home / ".config/systemd/user", cfg / "systemd/user"):
            d.mkdir(parents=True)
            (d / "oto-eq.service").write_text("[Unit]\n")
        r = self.run_script("uninstall.sh", home, "--dry-run", env={"XDG_CONFIG_HOME": str(cfg)})
        self.assertEqual(r.returncode, 0, r.stderr)
        rm = [ln for ln in r.stdout.splitlines() if ln.startswith("[dry-run] rm -f") and "oto-eq.service" in ln]
        self.assertEqual(len(rm), 2, rm)


if __name__ == "__main__":
    unittest.main()
