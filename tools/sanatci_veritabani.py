#!/usr/bin/env python3
"""Kulak Kesil: Wikidata'dan çevrimdışı "sanatçı → tür grubu" veritabanı üretir.

Veri: Wikidata (CC0), query.wikidata.org SPARQL. Hiçbir ad elle yazılmaz; liste tamamen
Wikidata'dan gelir. Türler (P136) tur.py'nin kendi etiket tablolarıyla (score_tags) 5 müzik
grubuna eşlenir; böylece çevrimiçi kaynaklarla aynı kurallar geçerli olur.

Kullanım (proje kökünden):
    python3 -I tools/sanatci_veritabani.py                 # indir (önbellekli) + derle
    python3 -I tools/sanatci_veritabani.py --yalniz-derle  # yalnız indirilmiş ham veriden derle
    python3 -I tools/sanatci_veritabani.py --ham /yol/ham --esik 12

Ham yanıtlar ``--ham`` klasöründe sorgu başına bir JSON dosyası olarak saklanır (sorgu
metninin özeti adıyla); yeniden çalıştırınca yalnız eksik sorgular atılır. Ham veri
güvenilmez sayılır: yalnız JSON olarak okunur, QID'ler ve etiketler doğrulanır.

Ağ nezaketi: ASCII User-Agent (urllib Türkçe harfi başlığa yazamaz), sorgular SIRAYLA,
her sorgu Wikidata'nın 60 sn sınırına sığacak boyutta parçalara bölünür, HTTP 429/503'te
Retry-After (yoksa 65 sn) beklenip tekrar denenir; bir kez 429 alınınca sorgu aralığı
dakikada bire çekilir.

Çıktı: oto_eq/data/sanatcilar.json.gz
    {"surum": 1, "kaynak": ..., "uretim": "YYYY-MM-DD", "gruplar": [...], "turler": [...],
     "s": {"<anahtar>": <paketli tamsayı>}}
    paket = grup | güven<<3 | türk<<10 | takma<<11 | tür1<<12 | tür2<<28
      grup: "gruplar" içindeki sıra; güven 0..100; türk 1 = Türkiye kökenli;
      takma 1 = anahtar bir takma ad/yan ad; tür1/tür2 = "turler" sırası + 1 (0 = yok).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from oto_eq import tur  # noqa: E402  (proje kökü yukarıda eklendi)

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "KulakKesil-repertuar/0.1 (kisisel kullanim; offline genre list builder)"
TIMEOUT = 75.0
DEFAULT_HAM = Path(tempfile.gettempdir()) / "oto-eq-repertuar" / "raw"
OUT = ROOT / "oto_eq" / "data" / "sanatcilar.json.gz"

GROUPS = ("metal_rock", "elektronik", "klasik_akustik", "pop_vokal")   # konusma: müzisyen değil
TURKEY = ("Q43", "Q12560", "Q23681")      # Türkiye, Osmanlı İmparatorluğu, KKTC
OCC_ROOTS = ("Q639669", "Q177220", "Q36834", "Q2252262", "Q130857", "Q183945", "Q753110",
             "Q488205")                    # müzisyen, şarkıcı, besteci, rapçi, DJ, yapımcı, söz yazarı…
ENSEMBLE_ROOT = "Q2088357"                 # müzik topluluğu (grup, orkestra, koro…)
BIG_OCCS = ("Q177220", "Q639669", "Q36834", "Q753110", "Q488205")   # ayrı ayrı sorgulanır
LABEL_LANGS = ("en", "tr", "mul", "de", "fr", "es", "ja", "ko", "ru")
ALIAS_LANGS = ("en", "tr", "mul")
QID = re.compile(r"^Q[1-9][0-9]{0,11}$")

# meslek etiketi (en) → (grup, ağırlık): türü (P136) olmayan Türk sanatçılar ve ek oy için
OCC_RULES: list[tuple[re.Pattern, str, float]] = [
    (re.compile(r"\brapper\b|hip hop|beatboxer"), "elektronik", 1.0),
    (re.compile(r"disc jockey|\bdj\b|electronic music|techno|turntablist"), "elektronik", 1.0),
    (re.compile(r"opera|tenor|soprano|baritone|mezzo|contralto|countertenor|castrato"),
     "klasik_akustik", 1.0),
    (re.compile(r"conductor|violinist|cellist|violist|harpsichordist|organist|oboist|"
                r"flautist|flutist|clarinetist|clarinettist|bassoonist|harpist|"
                r"classical|kapellmeister|music theorist"), "klasik_akustik", 0.8),
    (re.compile(r"\bney\b|neyzen|oud|kanun|qanun|kemenche|kemençe|bağlama|baglama|saz\b|"
                r"ashik|aşık|ozan|tanbur|hafız|hafiz|müezzin|muezzin|folk"), "klasik_akustik", 1.0),
    (re.compile(r"jazz|blues"), "klasik_akustik", 1.0),
    (re.compile(r"pianist|composer"), "klasik_akustik", 0.3),
    (re.compile(r"rock|metal|punk"), "metal_rock", 1.0),
    (re.compile(r"pop singer|pop musician|k-pop|idol"), "pop_vokal", 1.0),
]

# genel sözcükler: oynatıcıda "sanatçı" diye görünebilen ama sanatçı olmayan dizgiler
STOP_KEYS = {
    "music", "müzik", "muzik", "live", "canlı", "canli", "mix", "remix", "official", "topic",
    "various artists", "various", "unknown", "unknown artist", "bilinmeyen", "radio", "radyo",
    "podcast", "dj", "mc", "the", "a", "tv", "vevo", "records", "record", "lofi", "lo fi",
    "chill", "nightcore", "karaoke", "cover", "covers", "instrumental", "piano", "piyano",
    "orchestra", "orkestra", "playlist", "best", "top", "hits", "sound", "sounds", "beats",
    "beat", "songs", "song", "şarkı", "sarki", "şarkılar", "love", "intro", "outro", "youtube",
    "spotify", "artist", "sanatçı", "sanatci", "band", "grup", "group", "türkü", "turku",
    "slowed", "reverb", "sped up", "8d", "bass", "bass boosted", "remastered", "video",
    "audio", "lyrics", "film", "movie", "trailer", "news", "haber", "asmr", "gaming", "game",
    "oyun", "anime", "ost", "soundtrack", "various artist", "va", "feat", "ft", "x", "vs",
    "no", "ok", "hey", "oh", "baby", "girl", "boy", "man", "woman", "kids", "children",
    "nature", "rain", "sleep", "focus", "study", "relax", "relaxing", "meditation", "yoga",
    "anonymous", "anonim", "traditional", "geleneksel", "halk", "unknown composer",
}


# ============================================================================
#  SPARQL
# ============================================================================

class Sparql:
    def __init__(self, ham: Path, offline: bool = False):
        self.ham = ham
        self.ham.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.interval = 3.0
        self._last = 0.0
        self.sent = 0

    def _path(self, name: str, query: str) -> Path:
        h = hashlib.sha1(query.encode("utf-8")).hexdigest()[:12]
        return self.ham / f"{name}-{h}.json"

    def run(self, name: str, query: str) -> list[dict]:
        p = self._path(name, query)
        if p.exists():
            try:
                return _bindings(json.loads(p.read_text("utf-8")))
            except (OSError, ValueError):
                p.unlink(missing_ok=True)
        if self.offline:
            raise SystemExit(f"ham veri yok: {p.name} (--yalniz-derle ile ağ kapalı)")
        body = urllib.parse.urlencode({"query": query}).encode("ascii")
        for attempt in range(8):
            wait = self.interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.sent += 1
            req = urllib.request.Request(ENDPOINT, data=body, method="POST", headers={
                "User-Agent": USER_AGENT, "Accept": "application/sparql-results+json",
                "Content-Type": "application/x-www-form-urlencoded"})
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    raw = r.read()
            except urllib.error.HTTPError as e:
                if e.code in (429, 503, 502, 504):
                    ra = e.headers.get("Retry-After", "") if e.headers else ""
                    delay = float(ra) if ra.isdigit() else 65.0
                    self.interval = max(self.interval, 61.0)
                    print(f"  {name}: HTTP {e.code}, {delay:.0f} sn bekleniyor", flush=True)
                    time.sleep(min(delay, 300.0))
                    continue
                raise
            except (TimeoutError, urllib.error.URLError) as e:
                print(f"  {name}: {e}; 30 sn sonra tekrar", flush=True)
                time.sleep(30.0)
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                # WDQS sends a 200 with a truncated body when the query times out
                print(f"  {name}: yanıt bozuk/kesik ({len(raw)} bayt), tekrar", flush=True)
                time.sleep(65.0)
                continue
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(p)
            rows = _bindings(data)
            print(f"  {name}: {len(rows)} satır, {time.monotonic() - t0:.1f} sn", flush=True)
            return rows
        raise SystemExit(f"{name}: sorgu tekrar tekrar başarısız")


def _bindings(data) -> list[dict]:
    out = []
    for b in ((data or {}).get("results") or {}).get("bindings") or []:
        if isinstance(b, dict):
            out.append({k: str((v or {}).get("value", "")) for k, v in b.items()
                        if isinstance(v, dict)})
    return out


def _qid(v: str) -> str:
    q = v.rsplit("/", 1)[-1]
    return q if QID.match(q) else ""


def _qids(s: str) -> list[str]:
    return [q for q in (x.strip() for x in (s or "").split("|")) if QID.match(q)]


def _values(qids) -> str:
    return " ".join(f"wd:{q}" for q in qids)


def _chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ============================================================================
#  queries
# ============================================================================

def q_subclasses(roots) -> str:
    return f"""SELECT DISTINCT ?c ?l WHERE {{
  VALUES ?root {{ {_values(roots)} }}
  ?c wdt:P279* ?root .
  OPTIONAL {{ ?c rdfs:label ?l FILTER(lang(?l) = "en") }}
}}"""


def q_turk_humans(occs) -> str:
    return f"""SELECT ?a (SAMPLE(?sl) AS ?s)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?g), "entity/"); separator="|") AS ?gs)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?o), "entity/"); separator="|") AS ?os) WHERE {{
  VALUES ?c {{ {_values(TURKEY)} }}
  VALUES ?o {{ {_values(occs)} }}
  ?a wdt:P27 ?c ; wdt:P106 ?o ; wikibase:sitelinks ?sl .
  OPTIONAL {{ ?a wdt:P136 ?g }}
}} GROUP BY ?a"""


def q_turk_groups(classes) -> str:
    return f"""SELECT ?a (SAMPLE(?sl) AS ?s)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?g), "entity/"); separator="|") AS ?gs) WHERE {{
  VALUES ?t {{ {_values(classes)} }}
  ?a wdt:P31 ?t ; wikibase:sitelinks ?sl .
  {{ ?a wdt:P495 wd:Q43 }} UNION {{ ?a wdt:P17 wd:Q43 }} UNION
  {{ ?a wdt:P740 ?loc . ?loc wdt:P17 wd:Q43 }}
  OPTIONAL {{ ?a wdt:P136 ?g }}
}} GROUP BY ?a"""


def q_world_humans(occs, esik: int) -> str:
    return f"""SELECT ?a (SAMPLE(?sl) AS ?s)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?g), "entity/"); separator="|") AS ?gs) WHERE {{
  VALUES ?o {{ {_values(occs)} }}
  ?a wdt:P106 ?o ; wikibase:sitelinks ?sl .
  FILTER(?sl >= {esik})
  ?a wdt:P136 ?g ; wdt:P31 wd:Q5 .
}} GROUP BY ?a"""


def q_world_groups(classes, esik: int) -> str:
    return f"""SELECT ?a (SAMPLE(?sl) AS ?s)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?g), "entity/"); separator="|") AS ?gs) WHERE {{
  VALUES ?t {{ {_values(classes)} }}
  ?a wdt:P31 ?t ; wikibase:sitelinks ?sl .
  FILTER(?sl >= {esik})
  ?a wdt:P136 ?g .
}} GROUP BY ?a"""


def q_trwiki_humans(occs) -> str:
    """Music people with a genre and a Turkish Wikipedia article (diaspora, any popularity).
    Join order forced (optimizer None): occupation → genre → trwiki article."""
    return f"""SELECT ?a (SAMPLE(?sl) AS ?s)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?g), "entity/"); separator="|") AS ?gs) WHERE {{
  hint:Query hint:optimizer "None" .
  VALUES ?o {{ {_values(occs)} }}
  ?a wdt:P106 ?o ; wikibase:sitelinks ?sl .
  FILTER(?sl >= 2)
  ?a wdt:P136 ?g .
  ?art schema:about ?a ; schema:isPartOf <https://tr.wikipedia.org/> .
}} GROUP BY ?a"""


def q_trwiki_groups(classes) -> str:
    return f"""SELECT ?a (SAMPLE(?sl) AS ?s)
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?g), "entity/"); separator="|") AS ?gs) WHERE {{
  hint:Query hint:optimizer "None" .
  VALUES ?t {{ {_values(classes)} }}
  ?a wdt:P31 ?t ; wikibase:sitelinks ?sl ; wdt:P136 ?g .
  ?art schema:about ?a ; schema:isPartOf <https://tr.wikipedia.org/> .
}} GROUP BY ?a"""


# composers / musicians born before 1900: classical (Western art music, Ottoman/TSM,
# opera, early folk); Chopin and Brahms have no P136 at all in Wikidata
OLD_OCCS = ("Q36834", "Q639669", "Q177220", "Q486748", "Q765778", "Q1259917", "Q158852",
            "Q2865819", "Q13219637", "Q855091")


def q_old(esik: int, turkish: bool) -> str:
    where = (f"VALUES ?c {{ {_values(TURKEY)} }} ?a wdt:P27 ?c ." if turkish else "")
    flt = "" if turkish else f"FILTER(?sl >= {esik})"
    return f"""SELECT ?a (SAMPLE(?sl) AS ?s) WHERE {{
  hint:Query hint:optimizer "None" .
  VALUES ?o {{ {_values(OLD_OCCS)} }}
  ?a wdt:P106 ?o ; wikibase:sitelinks ?sl .
  {flt}
  {where}
  ?a wdt:P569 ?b .
  FILTER(YEAR(?b) < 1900)
}} GROUP BY ?a"""


def q_occ_of(qids, occs) -> str:
    """Music occupations of (world) humans: extra votes (rapper, DJ, opera singer)."""
    return f"""SELECT ?a (GROUP_CONCAT(DISTINCT STRAFTER(STR(?o), "entity/"); separator="|") AS ?os)
WHERE {{
  VALUES ?a {{ {_values(qids)} }}
  ?a wdt:P106 ?o .
}} GROUP BY ?a"""


def q_labels(qids) -> str:
    langs = ", ".join(f'"{x}"' for x in LABEL_LANGS)
    return f"""SELECT ?a ?l WHERE {{
  VALUES ?a {{ {_values(qids)} }}
  ?a rdfs:label ?l FILTER(lang(?l) IN ({langs}))
}}"""


def q_aliases(qids) -> str:
    langs = ", ".join(f'"{x}"' for x in ALIAS_LANGS)
    return f"""SELECT ?a ?l WHERE {{
  VALUES ?a {{ {_values(qids)} }}
  {{ ?a skos:altLabel ?l FILTER(lang(?l) IN ({langs})) }} UNION {{ ?a wdt:P742 ?l }}
}}"""


def q_genres(qids) -> str:
    return f"""SELECT ?g ?en ?tr
  (GROUP_CONCAT(DISTINCT STRAFTER(STR(?p), "entity/"); separator="|") AS ?ps) WHERE {{
  VALUES ?g {{ {_values(qids)} }}
  OPTIONAL {{ ?g rdfs:label ?en FILTER(lang(?en) = "en") }}
  OPTIONAL {{ ?g rdfs:label ?tr FILTER(lang(?tr) = "tr") }}
  OPTIONAL {{ ?g wdt:P279 ?p }}
}} GROUP BY ?g ?en ?tr"""


# ============================================================================
#  fetch
# ============================================================================

def fetch(sp: Sparql, esik_insan: int, esik_grup: int) -> dict:
    occ_rows = sp.run("meslekler", q_subclasses(OCC_ROOTS))
    occ_labels = {}
    for r in occ_rows:
        q = _qid(r.get("c", ""))
        if q:
            occ_labels[q] = r.get("l", "")
    occs = sorted(occ_labels)
    cls_rows = sp.run("topluluk-siniflari", q_subclasses([ENSEMBLE_ROOT, "Q215380"]))
    classes = sorted({q for q in (_qid(r.get("c", "")) for r in cls_rows) if q})
    print(f"meslek: {len(occs)}, topluluk sınıfı: {len(classes)}", flush=True)

    ents: dict[str, dict] = {}

    def add(rows, turk: bool, kind: str, with_occ: bool = False):
        for r in rows:
            q = _qid(r.get("a", ""))
            if not q:
                continue
            e = ents.setdefault(q, {"sl": 0, "g": set(), "o": set(), "turk": False, "kind": kind})
            try:
                e["sl"] = max(e["sl"], int(r.get("s", "0") or 0))
            except ValueError:
                pass
            e["g"].update(_qids(r.get("gs", "")))
            if with_occ:
                e["o"].update(_qids(r.get("os", "")))
            e["turk"] = e["turk"] or turk

    add(sp.run("turk-insan", q_turk_humans(occs)), True, "insan", with_occ=True)
    add(sp.run("turk-grup", q_turk_groups(classes)), True, "grup")
    small = [o for o in occs if o not in BIG_OCCS]
    for o in BIG_OCCS:
        add(sp.run(f"dunya-insan-{o}", q_world_humans([o], esik_insan)), False, "insan")
    for i, part in enumerate(_chunks(small, 300)):
        add(sp.run(f"dunya-insan-diger{i}", q_world_humans(part, esik_insan)), False, "insan")
    add(sp.run("dunya-grup", q_world_groups(classes, esik_grup)), False, "grup")
    base = set(ents)                       # stage 1 (its per-entity chunks stay cached)
    for o in BIG_OCCS:
        add(sp.run(f"trwiki-insan-{o}", q_trwiki_humans([o])), False, "insan")
    for i, part in enumerate(_chunks(small, 300)):
        add(sp.run(f"trwiki-insan-diger{i}", q_trwiki_humans(part)), False, "insan")
    add(sp.run("trwiki-grup", q_trwiki_groups(classes)), False, "grup")
    for name, turkish in (("eski-dunya", False), ("eski-turk", True)):
        rows = sp.run(name, q_old(esik_insan, turkish))
        add(rows, turkish, "insan")
        for r in rows:
            q = _qid(r.get("a", ""))
            if q in ents:
                ents[q]["eski"] = True
    extra = set(ents) - base
    print(f"varlık: {len(ents)} (Türk {sum(e['turk'] for e in ents.values())},"
          f" trwiki eki {len(extra)})", flush=True)

    def staged(name, pick, size, qfun):
        """Per-entity queries in chunks: stage-1 entities, then the extras separately."""
        for tag, part_set in (("", base), ("-ek", extra)):
            for i, part in enumerate(_chunks(sorted(q for q in part_set if pick(q)), size)):
                yield from sp.run(f"{name}{tag}{i}", qfun(part))

    # occupations of world humans (Turkish ones came with theirs)
    occset = set(occs)
    for r in staged("meslek-of", lambda q: ents[q]["kind"] == "insan" and not ents[q]["turk"],
                    20000, lambda part: q_occ_of(part, occs)):
        q = _qid(r.get("a", ""))
        if q in ents:
            ents[q]["o"].update(o for o in _qids(r.get("os", "")) if o in occset)

    labels: dict[str, set] = defaultdict(set)
    aliases: dict[str, set] = defaultdict(set)
    for r in staged("etiket", lambda q: True, 12000, q_labels):
        q = _qid(r.get("a", ""))
        if q and r.get("l"):
            labels[q].add(r["l"])
    for r in staged("takma", lambda q: True, 12000, q_aliases):
        q = _qid(r.get("a", ""))
        if q and r.get("l"):
            aliases[q].add(r["l"])

    genres: dict[str, dict] = {}
    base_g = {g for q in base for g in ents[q]["g"]}
    todo_sets = [("", sorted(base_g)),
                 ("-ek", sorted({g for q in extra for g in ents[q]["g"]} - base_g))]
    for depth in range(4):                 # genres + up to 3 levels of parents (P279)
        if not any(t for _tag, t in todo_sets):
            break
        found: dict[str, set[str]] = {"": set(), "-ek": set()}
        for tag, todo in todo_sets:
            for i, part in enumerate(_chunks(todo, 6000)):
                for r in sp.run(f"turler{depth}{tag}-{i}", q_genres(part)):
                    g = _qid(r.get("g", ""))
                    if not g:
                        continue
                    d = genres.setdefault(g, {"en": "", "tr": "", "p": set()})
                    d["en"] = d["en"] or r.get("en", "")
                    d["tr"] = d["tr"] or r.get("tr", "")
                    ps = set(_qids(r.get("ps", "")))
                    d["p"] |= ps
                    found[tag] |= ps
        nb = found[""] - set(genres)
        todo_sets = [("", sorted(nb)), ("-ek", sorted(found["-ek"] - set(genres) - nb))]
    return {"ents": ents, "labels": labels, "aliases": aliases, "genres": genres,
            "occ_labels": occ_labels}


# ============================================================================
#  build
# ============================================================================

# genres too broad to say anything ("popular music" → its parent "music"…): no vote, and
# the parent walk never passes through them
GENERIC_GENRES = {
    "music", "popular music", "experimental music", "avant-garde music", "avant-garde",
    "song", "songs", "vocal music", "instrumental music", "instrumental", "contemporary music",
    "musical genre", "genre", "sound", "work", "art", "performing arts", "entertainment",
    "musical composition", "composition", "recording", "album", "film", "television",
    "music of turkey", "turkish music", "music of the united states", "traditional music",
    "regional music", "ethnic music", "national music", "art music", "light music",
    "commercial music", "underground music", "independent music", "modern music",
}
_SUFFIX = re.compile(r"\s+(music|musics|songs?|müziği|müzik|müzikleri|musik|musique)$", re.I)


def _dist(label: str) -> dict[str, float]:
    """Group distribution of one genre label via tur.py's own tables ({} = no decision).
    "gospel music" → also tried as "gospel" (Wikidata labels carry a "music" suffix)."""
    if not label or label.strip().lower() in GENERIC_GENRES:
        return {}
    for cand in (label, _SUFFIX.sub("", label.strip())):
        sc = tur.score_tags("muzik", [cand])
        sc = {g: v for g, v in sc.items() if g in GROUPS and v > 0}
        if sc and max(sc.values()) >= tur.MIN_SCORE:
            tot = sum(sc.values())
            return {g: v / tot for g, v in sc.items()}
    return {}


def _generic(d: dict) -> bool:
    return (d.get("en") or "").strip().lower() in GENERIC_GENRES


def genre_map(genres: dict) -> tuple[dict[str, dict], dict[str, str]]:
    """QID → group distribution (own label, else nearest mapped parents, at most 2 levels,
    never through a generic genre) and the display label (Turkish if Wikidata has one)."""
    own: dict[str, dict] = {}
    for g, d in genres.items():
        if _generic(d):
            continue
        dist = _dist(d["en"]) or _dist(d["tr"])
        if dist:
            own[g] = dist
    out: dict[str, dict] = {}
    for g, d0 in genres.items():
        if g in own:
            out[g] = own[g]
            continue
        if _generic(d0):
            continue
        frontier, seen = [g], {g}
        for _depth in range(2):
            nxt = []
            for x in frontier:
                # sorted: "p" is a set; its order (PYTHONHASHSEED) must not change the
                # float sums / tie-breaks below (deterministic output)
                nxt += [p for p in sorted(genres.get(x, {}).get("p", ()))
                        if p not in seen and not _generic(genres.get(p, {}))]
            seen.update(nxt)
            hits = [own[p] for p in nxt if p in own]
            if hits:
                acc: Counter = Counter()
                for h in hits:
                    acc.update(h)
                tot = sum(acc.values())
                out[g] = {k: v / tot for k, v in acc.items()}
                break
            frontier = nxt
    shown = {g: (d["tr"] or d["en"]) for g, d in genres.items()}
    return out, shown


def occ_vote(label: str) -> tuple[str, float] | None:
    t = (label or "").lower()
    for rx, g, w in OCC_RULES:
        if rx.search(t):
            return g, w
    return None


def classify(e: dict, gmap: dict, occ_labels: dict) -> tuple[str, float, list[str]] | None:
    votes: Counter = Counter()
    why: dict[str, list[str]] = defaultdict(list)
    n = 0
    for g in sorted(e["g"]):
        dist = gmap.get(g)
        if not dist:
            continue
        n += 1
        votes.update(dist)
        top = max(dist, key=dist.get)
        why[top].append(g)
    if e.get("eski"):                       # born before 1900: classical / art music
        n += 1
        votes["klasik_akustik"] += 1.0
        why.setdefault("klasik_akustik", [])
    occw = 0.5 if n else 1.0
    nocc = 0
    for o in sorted(e["o"]):
        v = occ_vote(occ_labels.get(o, ""))
        if v:
            votes[v[0]] += v[1] * occw
            nocc += 1
    tot = sum(votes.values())
    if tot <= 0:
        return None
    group, top = max(votes.items(), key=lambda kv: kv[1])
    share = top / tot
    if n == 0 and top < 0.8:               # only a weak occupation hint ("composer")
        return None
    evidence = n + 0.5 * nocc
    conf = share * min(1.0, 0.55 + 0.2 * evidence)
    return group, conf, why.get(group, [])


_STOP_FOLDED = {tur._fold(k) for k in STOP_KEYS}


def _valid_key(key: str, alias: bool) -> bool:
    """``key`` is already folded (tur._fold)."""
    if not key or key in _STOP_FOLDED:
        return False
    if key in tur._GENRE_INDEX:                  # "Phonk", "Trap": a genre, not an artist
        return False
    if key.isdigit() or len(key) > 60:
        return False
    if alias and (len(key) < 4 or len(key.split()) > 5):
        return False
    return True


def keys_of(name: str) -> str:
    """DB key (tur._fold) of a label; '' if clean_artist() would cut the name
    ("Simon & Garfunkel" → "Simon": the runtime never sees the full name)."""
    k = tur.norm(name)
    if not k or tur.normalize_artist(name) != k:
        return ""
    f = tur._fold(name)
    return f if tur._fold(f) == f else ""          # lookups fold again: must be stable


def build(raw: dict) -> tuple[dict, dict]:
    ents, labels, aliases = raw["ents"], raw["labels"], raw["aliases"]
    gmap, shown = genre_map(raw["genres"])
    res: dict[str, tuple] = {}
    for q, e in ents.items():
        c = classify(e, gmap, raw["occ_labels"])
        if c:
            res[q] = c
    # candidate keys
    cand: dict[str, list[tuple]] = defaultdict(list)    # key -> [(tier, turk, sl, qid)]
    for q in res:
        prim = {keys_of(n) for n in labels.get(q, ())} - {""}
        for k in sorted(prim):
            if _valid_key(k, False):
                cand[k].append((1, ents[q]["turk"], ents[q]["sl"], q))
        for n in sorted(aliases.get(q, ())):
            k = keys_of(n)
            if k and k not in prim and _valid_key(k, True):
                cand[k].append((0, ents[q]["turk"], ents[q]["sl"], q))
    turler: list[str] = []
    tidx: dict[str, int] = {}
    table: dict[str, int] = {}
    stats = Counter()
    for k, cs in cand.items():
        best_tier = max(c[0] for c in cs)
        cs = [c for c in cs if c[0] == best_tier]
        if any(c[1] for c in cs):
            cs = [c for c in cs if c[1]]               # a Turkish artist wins the name
        cs.sort(key=lambda c: (-c[2], c[3]))
        tier, turk, sl, q = cs[0]
        group, conf, why = res[q]
        rivals = [c for c in cs[1:] if res[c[3]][0] != group]
        if rivals:
            if tier == 0:
                stats["takma_cakisma_atlandi"] += 1
                continue
            r_sl = rivals[0][2]
            if r_sl >= 0.5 * sl or sl - r_sl < 3:     # about as well known, other genre: unsure
                conf *= 0.5
                stats["cakisma_belirsiz"] += 1
            else:
                conf *= 0.85
                stats["cakisma_populer_secildi"] += 1
        gids = []
        for g in why[:2]:
            lab = shown.get(g, "")
            if not lab:
                continue
            if lab not in tidx:
                tidx[lab] = len(turler)
                turler.append(lab)
            gids.append(tidx[lab] + 1)
        gids += [0, 0]
        c100 = max(0, min(100, round(conf * 100)))
        table[k] = tur.pack_db_value(GROUPS.index(group), c100, turk, tier == 0, gids[0], gids[1])
        stats["anahtar"] += 1
        stats["takma" if tier == 0 else "ana"] += 1
    data = {"surum": 1, "kaynak": "Wikidata (CC0), query.wikidata.org",
            "uretim": date.today().isoformat(), "gruplar": list(GROUPS), "turler": turler,
            "s": dict(sorted(table.items()))}
    sanatci = Counter()
    for q, (g, _c, _w) in res.items():
        sanatci["turk" if ents[q]["turk"] else "yabanci"] += 1
        sanatci[g] += 1
    stats.update({f"sanatci_{k}": v for k, v in sanatci.items()})
    stats["sanatci_toplam"] = len(res)
    stats["varlik_toplam"] = len(ents)
    stats["tur_eslenen"] = len(gmap)
    stats["tur_toplam"] = len(raw["genres"])
    return data, dict(stats)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--ham", type=Path, default=DEFAULT_HAM, help="ham yanıt klasörü")
    ap.add_argument("--cikti", type=Path, default=OUT)
    ap.add_argument("--esik", type=int, default=10, help="yabancı kişi: en az sitelink sayısı")
    ap.add_argument("--esik-grup", type=int, default=6, help="yabancı grup: en az sitelink")
    ap.add_argument("--yalniz-derle", action="store_true", help="ağa çıkma, ham veriden derle")
    a = ap.parse_args(argv)
    sp = Sparql(a.ham, offline=a.yalniz_derle)
    raw = fetch(sp, a.esik, a.esik_grup)
    data, stats = build(raw)
    a.cikti.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    tmp = a.cikti.with_suffix(".tmp")
    with open(tmp, "wb") as raw_f, gzip.GzipFile(fileobj=raw_f, mode="wb", compresslevel=9,
                                                  mtime=0) as f:
        f.write(blob)
    tmp.replace(a.cikti)
    print(json.dumps(stats, ensure_ascii=False, indent=1, sort_keys=True))
    print(f"yazıldı: {a.cikti} ({a.cikti.stat().st_size / 1e6:.2f} MB, ham {len(blob) / 1e6:.2f} MB),"
          f" istek: {sp.sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
