"""Text checks shared by both engines: sentence units, tolerant number matching, identifier labels.

All checks are deterministic. A failed check only removes a sentence when the signal is reliable
(a number that appears in no evidence at all); everything else re-points or removes the citation."""

import re

# ------------------------------------------------------------------ sentence units
_ABBR = re.compile(
    r"(?:\b(?:Abs|Nr|Art|Ziff|lit|bzw|ca|vgl|resp|Dr|Prof|St|Mio|Mrd|Fr|Bd|Kap|Anm|ff|al|inkl|exkl|evtl|ggf|sog|usw|etc"
    r"|Jan|Feb|Mär|Apr|Jun|Jul|Aug|Sep|Sept|Okt|Nov|Dez|No|Vol|vs|approx|Univ|Hr|Hrsg|Mr|Mrs|Ms|Jr|Sr|Inc|Ltd|Co|Corp"
    r"|Dept|Gov|Fig|Abb|Tab|Bst|Rz|N|S|Bl|gem|sog|Stv|Dir|Präs|Kt|Bsp|bspw|insb|u\.a|z\.T|o\.ä|u\.U|v\.a)"
    r"|\be\.g|\bi\.e|\bz\.\s?B|\bd\.\s?h|\bu\.\s?a|\bi\.\s?d\.\s?R|\b[A-ZÄÖÜa-zäöü]|(?<!\d)\d{1,2})\.$")  # ordinals, not years


def split_units(text: str) -> list[str]:
    """Sentences and lines for citation checks. Splits only before an uppercase letter or quote and not
    after abbreviations, ordinals or initials, so a removal never cuts a sentence in half. Errs towards
    too few splits: a unit with two sentences is checked as one, which is safe."""
    units = []
    for line in text.split("\n"):
        start = 0
        for m in re.finditer(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ„\"«(\[])", line):
            head = line[start:m.start()]
            if _ABBR.search(head):
                continue
            units.append(head)
            start = m.end()
        units.append(line[start:])
    return [u for u in units if u.strip()]


# ------------------------------------------------------------------ numbers
_MONTHS = {m: i for i, ms in enumerate([
    ("januar", "january", "jan", "janvier", "gennaio"), ("februar", "february", "feb", "février", "fevrier", "febbraio"),
    ("märz", "march", "mär", "mars", "marzo"), ("april", "apr", "avril", "aprile"), ("mai", "may", "maggio"),
    ("juni", "june", "jun", "juin", "giugno"), ("juli", "july", "jul", "juillet", "luglio"), ("august", "aug", "août", "agosto"),
    ("september", "sep", "sept", "septembre", "settembre"), ("oktober", "october", "okt", "oct", "octobre", "ottobre"),
    ("november", "nov", "novembre"), ("dezember", "december", "dez", "dec", "décembre", "dicembre")], 1) for m in ms}
_DATE_NUM = re.compile(r"\b(\d{1,2})\.\s?(\d{1,2})\.\s?((?:19|20)\d{2})\b")
_DATE_TXT = re.compile(r"\b(\d{1,2})\.?\s+(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?\s+((?:19|20)\d{2})\b", re.I)
_DATE_ISO = re.compile(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b")
# a number with optional thousands separators (' ’ space . ,) and decimals; not part of a word
_NUM = re.compile(r"(?<![\w])\d{1,3}(?:(?:['’   ]|\.(?=\d{3}(?!\d))|,(?=\d{3}(?!\d)))\d{3})+(?:[.,]\d+)?(?![\w])"
                  r"|(?<![\w])\d+(?:[.,]\d+)?(?![\w])")
_SCALE = re.compile(r"\s*(mio|millionen|million|millions|mrd|milliarden|milliarde|billion|billions|bn|tsd|tausend)\b\.?", re.I)
_SCALES = {"mio": 1e6, "millionen": 1e6, "million": 1e6, "millions": 1e6, "mrd": 1e9, "milliarden": 1e9,
           "milliarde": 1e9, "billion": 1e9, "billions": 1e9, "bn": 1e9, "tsd": 1e3, "tausend": 1e3}
_CITE = re.compile(r"\[(?:S|E)?\d+(?:\s*[,;]\s*(?:S|E)?\d+)*\]")


def _iso_dates(text: str) -> tuple[set[str], str]:
    """ISO dates found in the text, and the text with those dates blanked out (so their parts are not
    checked again as separate numbers)."""
    out = set()

    def keep(y, m, d):
        try:
            if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
                out.add(f"{int(y):04d}-{int(m):02d}-{int(d):02d}")
                out.add(str(int(y)))
        except ValueError:
            pass
        return " "

    text = _DATE_ISO.sub(lambda m: keep(m.group(1), m.group(2), m.group(3)), text)
    text = _DATE_NUM.sub(lambda m: keep(m.group(3), m.group(2), m.group(1)), text)
    text = _DATE_TXT.sub(lambda m: keep(m.group(3), _MONTHS[m.group(2).lower()], m.group(1)), text)
    return out, text


def _canon(x: float) -> str:
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def _forms(token: str, dec: int, scale: float = 1.0) -> dict[str, float]:
    """Canonical values a number token can stand for ('1.234' is 1234 or 1.234), each with the absolute
    tolerance its precision implies ('2,3' +-0.05, '1,2 Mio.' +-50'000, '1'200' +-5 %, years exact)."""
    t = re.sub(r"['’\u202f\u2009 ]", "", token)
    cands = []
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", t):            # 1.234 / 153.260: thousands or decimal
        cands += [(t.replace(".", ""), 0), (t, 3)]
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+", t):           # 1,234: English thousands or decimal comma
        cands += [(t.replace(",", ""), 0), (t.replace(",", "."), 3)]
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+", t):      # 1.234,5
        cands += [(t.replace(".", "").replace(",", "."), dec)]
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+\.\d+", t):      # 1,234.5
        cands += [(t.replace(",", ""), dec)]
    else:
        cands += [(t.replace(",", "."), dec)]
    out: dict[str, float] = {}
    for c, d in cands:
        try:
            v = float(c)
        except ValueError:
            continue
        if d:
            tol = 0.5 * 10 ** -d
        elif v >= 1000 and not (1900 <= v <= 2100 and scale == 1.0):
            tol = 0.05 * v
        else:
            tol = 0.0
        out[_canon(v)] = tol if d <= 2 else 0.0          # 3+ decimals: identifiers (23.5544), exact
        if scale != 1.0:
            out[_canon(v * scale)] = max(tol, 0.5 * 10 ** -d if d else 0.5) * scale
    return out


def numbers(text: str) -> list[tuple[str, dict[str, float]]]:
    """Numbers in a text as (token, {canonical form: tolerance}); dates as ISO strings. Citation markers
    are ignored."""
    text = _CITE.sub(" ", text)
    dates, rest = _iso_dates(text)
    out = [(d, {d: 0.0}) for d in dates if "-" in d]
    for m in _NUM.finditer(rest):
        tok = m.group(0)
        sm = _SCALE.match(rest, m.end())
        scale = _SCALES[sm.group(1).lower()] if sm else 1.0
        dm = re.search(r"[.,](\d+)$", re.sub(r"['’\u202f\u2009 ]", "", tok))
        dec = len(dm.group(1)) if dm and len(dm.group(1)) != 3 else (3 if dm else 0)
        out.append((tok, _forms(tok, dec, scale)))
    return out


class Evidence:
    """All number forms of a body of evidence text, for fast membership checks."""

    def __init__(self, text: str = ""):
        self.forms: set[str] = set()
        self.values: list[float] = []
        self.add(text)

    def add(self, text: str) -> None:
        dates, _ = _iso_dates(text)
        self.forms |= dates
        for _, forms in numbers(text):
            self.forms |= set(forms)
            for f in forms:
                try:
                    self.values.append(float(f))
                except ValueError:
                    pass

    def supports(self, forms: dict[str, float]) -> bool:
        if set(forms) & self.forms:
            return True
        for f, tol in forms.items():           # rounding: "2,3 %" by "2,34 %", "1,2 Mio." by "1'234'567"
            if not tol:
                continue
            x = float(f)
            if any(abs(v - x) <= tol for v in self.values):
                return True
        return False


def checked_numbers(sentence: str, ignore: Evidence | None = None) -> list[tuple[str, dict[str, float]]]:
    """The numbers of a sentence that need evidence: not small counts (<= 12 without decimals), not
    numbers from the task text (`ignore`)."""
    out = []
    for tok, forms in numbers(sentence):
        if "-" not in tok and re.fullmatch(r"\d{1,2}", tok) and int(tok) <= 12:
            continue
        if ignore and set(forms) & ignore.forms:
            continue
        out.append((tok, forms))
    return out


def unsupported(sentence: str, evidence: Evidence, ignore: Evidence | None = None) -> list[str]:
    return [tok for tok, forms in checked_numbers(sentence, ignore) if not evidence.supports(forms)]


# ------------------------------------------------------------------ identifiers
# Cantonal law collections do not use SR numbers (SR = Bund). opencaselaw labels every number "SR".
CANTON_PREFIX = {"BS": "SG", "BL": "SGS", "ZH": "LS", "BE": "BSG", "LU": "SRL", "AG": "SAR", "SO": "BGS",
                 "SG": "sGS", "TG": "RB", "GR": "BR", "ZG": "BGS"}
_CANTON_NAMES = {"Basel-Stadt": "BS", "Basel-Landschaft": "BL", "Zürich": "ZH", "Bern": "BE", "Luzern": "LU",
                 "Aargau": "AG", "Solothurn": "SO", "St. Gallen": "SG", "Thurgau": "TG", "Graubünden": "GR", "Zug": "ZG"}


def cantonal_labels(text: str, canton: str | None = None) -> str:
    """Rewrite 'SR <nr>' for cantonal enactments in law-tool output: per line when the line names the
    canton ('[BS]', '(BS)', 'Basel-Stadt'), and in the header of a single-law result for `canton`."""
    out = []
    for i, line in enumerate(text.split("\n")):
        code = next((c for c in CANTON_PREFIX if re.search(rf"\[{c}\]|\({c}\)|^# {c} ", line)), None)
        code = code or next((c for n, c in _CANTON_NAMES.items() if n in line and "SR" in line), None)
        if code is None and canton in CANTON_PREFIX and i < 12:
            code = canton
        if code:
            line = re.sub(r"\*\*SR Number:\*\*", f"**{CANTON_PREFIX[code]}-Nummer:**", line)
            line = re.sub(r"\bSR(\s+)(?=\d)", CANTON_PREFIX[code] + r"\1", line)
        out.append(line)
    return "\n".join(out)


def fix_sr_labels(report: str, cantonal_numbers: dict[str, str]) -> str:
    """'SR 153.260' -> 'SG 153.260' when the evidence shows 153.260 is a cantonal number."""
    def sub(m):
        prefix = cantonal_numbers.get(m.group(2))
        return f"{prefix}{m.group(1)}{m.group(2)}" if prefix else m.group(0)
    return re.sub(r"\bSR(\s+)(\d{1,3}(?:\.\d+)+)", sub, report)
