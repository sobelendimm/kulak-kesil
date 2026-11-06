"""yedek (settings backup/restore) tests — temp dirs only, never the real ~/.config."""
from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from oto_eq import config, yedek as Y

SHIPPED = config.DEFAULTS_DIR


def _eqgen_ok() -> bool:
    try:
        from oto_eq import eqgen  # noqa: F401
        return hasattr(eqgen, "load_profiles")
    except Exception:  # noqa: BLE001
        return False


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.cdir = self.root / "config" / "oto-eq"
        self.cdir.mkdir(parents=True)
        self.data = self.root / "data"
        p1 = mock.patch.object(config, "CONFIG_DIR", self.cdir)
        p2 = mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(self.data)})
        for p in (p1, p2):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._td.cleanup)
        self.ydir = self.data / "oto-eq" / "yedekler"
        # a realistic config dir: user files + generated files that must NOT be backed up
        (self.cdir / "kurallar.toml").write_bytes((SHIPPED / "kurallar.toml").read_bytes())
        (self.cdir / "profiller.toml").write_bytes((SHIPPED / "profiller.toml").read_bytes())
        (self.cdir / "kisisel.json").write_text('{"genel": {"bas": 1.5}}\n')
        (self.cdir / "atamalar.json").write_text('{"atamalar": []}\n')
        (self.cdir / "oto-eq-sinks.conf").write_text("üretilmiş\n")
        (self.cdir / "egriler.png").write_bytes(b"\x89PNG")
        (self.cdir / "kurallar.toml.yedek-1").write_text("eski\n")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        rc = Y.run(list(argv), out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def make_tar(self, members, manifest=True, bicim=Y.BICIM, surum=Y.SURUM, hashes=None):
        """Hand-made archive for the negative tests. members: {name: bytes}."""
        path = self.root / f"el-{len(list(self.root.glob('el-*')))}.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            items = dict(members)
            if manifest:
                files = {n: {"sha256": (hashes or {}).get(n, hashlib.sha256(b).hexdigest()),
                             "boyut": len(b)} for n, b in members.items()
                         if not n.startswith("../") and "/" not in n}
                items = {"manifest.json": json.dumps({"bicim": bicim, "surum": surum,
                                                      "dosyalar": files}).encode(), **members}
            for n, b in items.items():
                ti = tarfile.TarInfo(n if n.startswith("../") else f"{Y.KOK}/{n}")
                ti.size = len(b)
                tar.addfile(ti, io.BytesIO(b))
        return path


class TestAl(Base):
    def test_backup_only_user_files_with_manifest(self):
        path, m = Y.al()
        self.assertEqual(path.parent, self.ydir)
        self.assertTrue(path.name.startswith("kulak-yedek-") and path.name.endswith(".tar.gz"))
        with tarfile.open(path) as tar:
            names = sorted(tar.getnames())
        self.assertEqual(names, sorted(f"{Y.KOK}/{n}" for n in
                                       ["manifest.json", "atamalar.json", "kisisel.json",
                                        "kurallar.toml", "profiller.toml"]))
        self.assertEqual(m["bicim"], Y.BICIM)
        self.assertEqual(m["surum"], 1)
        self.assertEqual(m["dosyalar"]["kisisel.json"]["sha256"],
                         hashlib.sha256((self.cdir / "kisisel.json").read_bytes()).hexdigest())
        self.assertEqual(list(self.ydir.glob(".yedek-*")), [])          # no temp leftovers
        man, files = Y.oku(path)
        self.assertEqual(set(files), {"atamalar.json", "kisisel.json", "kurallar.toml",
                                      "profiller.toml"})

    def test_target_forms(self):
        p, _ = Y.al(self.root / "benim")
        self.assertEqual(p, self.root / "benim.tar.gz")
        p2, _ = Y.al(self.root)
        self.assertEqual(p2.parent, self.root)
        with self.assertRaises(Y.YedekHatasi):
            Y.al(self.root / "benim.tar.gz")                               # never overwrite

    def test_nothing_to_backup(self):
        with self.assertRaises(Y.YedekHatasi):
            Y.al(cdir=self.root / "bos")


class TestOku(Base):
    def test_tampered_and_foreign_archives(self):
        good = {"kisisel.json": b'{"genel": {}}'}
        cases = {
            "sağlama": self.make_tar(good, hashes={"kisisel.json": "0" * 64}),
            "beklenmeyen dosya": self.make_tar({**good, "oto-eq-sinks.conf": b"x"}),
            "manifest.json yok": self.make_tar(good, manifest=False),
            "Kulak Kesil yedeği değil": self.make_tar(good, bicim="baska"),
            "desteklenmiyor": self.make_tar(good, surum=2),
            "beklenmeyen dosya: ../": self.make_tar({**good, "../kisisel.json": b"{}"}),
        }
        for beklenen, path in cases.items():
            with self.subTest(beklenen):
                with self.assertRaises(Y.YedekHatasi) as cm:
                    Y.oku(path)
                self.assertIn(beklenen.split(": ")[0], str(cm.exception))

    def test_symlink_member_rejected(self):
        path = self.root / "link.tar.gz"
        with tarfile.open(path, "w:gz") as tar:
            ti = tarfile.TarInfo(f"{Y.KOK}/kisisel.json")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tar.addfile(ti)
        with self.assertRaises(Y.YedekHatasi):
            Y.oku(path)

    def test_manifest_mismatch_and_garbage(self):
        p = self.make_tar({"kisisel.json": b"{}"})
        # add a file the manifest does not list
        with tarfile.open(p) as t:
            members = {m.name: t.extractfile(m).read() for m in t if m.isfile()}
        members[f"{Y.KOK}/atamalar.json"] = b"{}"
        with tarfile.open(p, "w:gz") as t:
            for n, b in members.items():
                ti = tarfile.TarInfo(n)
                ti.size = len(b)
                t.addfile(ti, io.BytesIO(b))
        with self.assertRaisesRegex(Y.YedekHatasi, "uyuşmuyor"):
            Y.oku(p)
        junk = self.root / "junk.tar.gz"
        junk.write_bytes(b"bu bir arsiv degil")
        with self.assertRaisesRegex(Y.YedekHatasi, "okunamadı"):
            Y.oku(junk)
        with self.assertRaisesRegex(Y.YedekHatasi, "bulunamadı"):
            Y.oku(self.root / "yok.tar.gz")

    def test_content_validation(self):
        for ad, data, msg in (("kurallar.toml", b"[genel\nx=", "TOML"),
                              ("kisisel.json", b"{bozuk", "JSON"),
                              ("atamalar.json", b"[1, 2]", "JSON nesnesi"),
                              ("kisma.json", b"\xff\xfe", "UTF-8")):
            with self.subTest(ad):
                with self.assertRaisesRegex(Y.YedekHatasi, msg):
                    Y.oku(self.make_tar({ad: data}))

    @unittest.skipUnless(_eqgen_ok(), "eqgen yüklenemedi")
    def test_profiles_semantic_validation(self):
        bad = b"[genel]\ntavan_dbfs = 5.0\n"
        with self.assertRaisesRegex(Y.YedekHatasi, "geçersiz profil"):
            Y.oku(self.make_tar({"profiller.toml": bad}))
        Y.oku(self.make_tar({"profiller.toml": (SHIPPED / "profiller.toml").read_bytes()}))


class TestYukle(Base):
    def test_dry_run_writes_nothing(self):
        path, _ = Y.al()
        (self.cdir / "kisisel.json").write_text('{"genel": {"bas": -2}}\n')
        before = {p.name: p.read_bytes() for p in self.cdir.iterdir()}
        s = Y.yukle(path, dene=True)
        self.assertEqual(s["degisen"], ["kisisel.json"])
        self.assertIsNone(s["on_yedek"])
        self.assertEqual({p.name: p.read_bytes() for p in self.cdir.iterdir()}, before)
        self.assertEqual(sorted(self.ydir.glob(Y.OTO_ONEK + "*")), [])

    def test_restore_with_pre_backup(self):
        path, _ = Y.al()
        orig = (self.cdir / "kisisel.json").read_bytes()
        (self.cdir / "kisisel.json").write_text('{"genel": {"bas": -2}}\n')
        (self.cdir / "atamalar.json").unlink()
        (self.cdir / "kisma.json").write_text('{"db": 6}\n')             # not in the backup
        s = Y.yukle(path)
        self.assertEqual(s["degisen"], ["kisisel.json"])
        self.assertEqual(s["yeni"], ["atamalar.json"])
        self.assertEqual(s["ayni"], ["kurallar.toml", "profiller.toml"])
        self.assertEqual((self.cdir / "kisisel.json").read_bytes(), orig)
        self.assertTrue((self.cdir / "atamalar.json").exists())
        self.assertEqual((self.cdir / "kisma.json").read_text(), '{"db": 6}\n')   # untouched
        self.assertEqual((self.cdir / "oto-eq-sinks.conf").read_text(), "üretilmiş\n")
        _, pre = Y.oku(s["on_yedek"])                                   # pre-restore state
        self.assertEqual(pre["kisisel.json"], b'{"genel": {"bas": -2}}\n')
        self.assertIn("kisma.json", pre)
        self.assertEqual([p.name for p in self.cdir.glob(".*.tmp")], [])
        self.assertEqual(Y.sonraki_adimlar(s["degisen"] + s["yeni"]),
                         ["kulak yenile", "kulak eq-uret"])

    def test_nothing_changes_no_pre_backup(self):
        path, _ = Y.al()
        s = Y.yukle(path)
        self.assertEqual(s["degisen"] + s["yeni"], [])
        self.assertIsNone(s["on_yedek"])

    def test_invalid_archive_never_writes(self):
        before = {p.name: p.read_bytes() for p in self.cdir.iterdir()}
        with self.assertRaises(Y.YedekHatasi):
            Y.yukle(self.make_tar({"kurallar.toml": b"[bozuk"}))
        self.assertEqual({p.name: p.read_bytes() for p in self.cdir.iterdir()}, before)
        self.assertFalse(self.ydir.exists())

    def test_pruning_keeps_recent_automatic_backups(self):
        self.ydir.mkdir(parents=True)
        for i in range(Y.OTO_SAKLA + 3):
            (self.ydir / f"{Y.OTO_ONEK}2020010{i:02d}.tar.gz").write_bytes(b"x")
        (self.ydir / "kulak-yedek-elle.tar.gz").write_bytes(b"x")
        path, _ = Y.al(self.root / "a.tar.gz")
        (self.cdir / "kisisel.json").write_text("{}\n")
        Y.yukle(path)
        self.assertEqual(len(list(self.ydir.glob(Y.OTO_ONEK + "*"))), Y.OTO_SAKLA)
        self.assertTrue((self.ydir / "kulak-yedek-elle.tar.gz").exists())   # manual kept


class TestCli(Base):
    def test_flow(self):
        rc, out, _ = self.run_cli("al", str(self.root / "y"))
        self.assertEqual(rc, 0)
        self.assertIn("Yedek alındı", out)
        f = str(self.root / "y.tar.gz")
        (self.cdir / "kurallar.toml").write_text("[genel]\n")
        (self.cdir / "mikrofon.toml").write_text("[genel]\n")    # not in backup → untouched
        rc, out, _ = self.run_cli("yukle", f, "--dene")
        self.assertEqual(rc, 0)
        self.assertIn("değişecek: kurallar.toml", out)
        self.assertIn("hiçbir dosya yazılmadı", out)
        self.assertEqual((self.cdir / "kurallar.toml").read_text(), "[genel]\n")
        rc, out, _ = self.run_cli("yukle", f)
        self.assertEqual(rc, 0)
        self.assertIn("değişti: kurallar.toml", out)
        self.assertIn("Önceki ayarlar yedeklendi", out)
        self.assertIn("  kulak yenile", out)
        self.assertNotIn("eq-uret", out)
        rc, out, _ = self.run_cli("yukle", f)
        self.assertIn("Değişecek bir şey yok", out)
        rc, out, _ = self.run_cli("liste")
        self.assertEqual(rc, 0)
        self.assertIn(Y.OTO_ONEK, out)

    def test_errors_and_usage(self):
        rc, _, err = self.run_cli("yukle", str(self.root / "yok.tar.gz"))
        self.assertEqual(rc, 1)
        self.assertIn("hata: yedek dosyası bulunamadı", err)
        self.assertEqual(self.run_cli("yukle")[0], 2)
        self.assertEqual(self.run_cli("al", "a", "b")[0], 2)
        self.assertEqual(self.run_cli("bilinmeyen")[0], 2)
        self.assertEqual(self.run_cli()[0], 2)
        rc, out, _ = self.run_cli("--help")
        self.assertEqual(rc, 0)
        self.assertIn("kulak yedek al", out)
        rc, out, _ = self.run_cli("liste")
        self.assertIn("yedek yok", out)



class TestCeviri(unittest.TestCase):
    def tearDown(self):
        from oto_eq import i18n
        i18n.sifirla()

    def test_forced_catalog_reaches_cli(self):
        import io
        from oto_eq import i18n
        i18n.sifirla("de", {"bilinmeyen alt komut: {komut}": "unbekannter Befehl: {komut}"})
        err = io.StringIO()
        self.assertEqual(Y.run(["xyz"], out=io.StringIO(), err=err), 2)
        self.assertIn("unbekannter Befehl: xyz", err.getvalue())


if __name__ == "__main__":
    unittest.main()
