"""Rapprochement des annonces diffusées sur plusieurs sites.

Une entreprise publie souvent la même offre sur LinkedIn, HelloWork, WTTJ,
France Travail… Chaque site produit son propre ``external_id`` ; ce module
reconnaît qu'il s'agit de la même annonce et la rattache à **une seule** ligne
``jobs`` (la plus complète), chaque diffusion étant tracée dans
``job_sources`` (site, lien, dates).

Critères (tous requis) :

* même entreprise après normalisation (formes juridiques, « Groupe »,
  « France », accents et ponctuation retirés) — ou, entreprise inconnue des
  deux côtés, descriptions très proches ;
* intitulés proches (mots communs, sans « H/F » ni mention de contrat) ;
* villes compatibles (même ville, ou ville absente d'un côté) ;
* contrats compatibles et niveaux de séniorité non contradictoires (un stage
  et une alternance de la même entreprise restent deux offres).
"""

from __future__ import annotations

import re
from datetime import datetime

from . import contracts
from .contracts import fold

_LEGAL = {"sas", "sasu", "sa", "sarl", "eurl", "sca", "snc", "scop", "sci", "gie",
          "selarl", "groupe", "group", "holding", "france", "fr", "inc", "ltd", "llc",
          "gmbh", "bv", "nv", "plc", "corp", "co", "cie", "the", "et", "and"}
_UNKNOWN_COMPANY = {"", "inconnue", "inconnu", "unknown", "confidentiel",
                    "confidentielle", "entreprise confidentielle", "non communique",
                    "nc", "na", "n a", "anonyme", "societe confidentielle"}
_GENDER = re.compile(r"\(?\b(?:h\s*/\s*f(?:\s*/\s*[xd])?|f\s*/\s*h(?:\s*/\s*[xd])?|"
                     r"m\s*/\s*f(?:\s*/\s*[xd])?|m\s*/\s*w\s*/\s*d|homme\s*/\s*femme|"
                     r"femme\s*/\s*homme)\b\)?")
_CONTRACT_WORDS = re.compile(
    r"\b(?:cdi|cdd|stages?|stagiaires?|alternances?|alternant\w*|apprenti\w*|"
    r"apprentissage|internship|intern|freelance|interim|v\.?i\.?e|"
    r"temps (?:plein|partiel)|full[- ]time|part[- ]time|"
    r"contrat de professionnalisation)\b")
_TITLE_STOP = {"de", "du", "des", "d", "en", "et", "l", "la", "le", "les", "a", "au",
               "aux", "pour", "un", "une", "with", "and", "of", "the", "in", "for", "sur",
               "par", "ou", "or", "mois", "month", "months"}
_PLACE_STOP = {"france", "ile", "de", "du", "des", "la", "le", "les", "sur", "en", "et",
               "region", "metropole", "grand", "area", "greater", "remote",
               "teletravail", "hybride", "arrondissement", "cedex", "er", "e", "eme",
               "auvergne", "rhone", "alpes", "hauts", "normandie", "bretagne",
               "occitanie", "nouvelle", "aquitaine", "provence", "cote", "azur",
               "pays", "loire", "centre", "val", "bourgogne", "franche", "comte",
               "est", "corse", "departement", "fr"}

# Mots qu'un même employeur ajoute ou retire selon le site (« Acme » /
# « Acme Consulting ») ; tout autre mot distingue une entité (« VINCI » /
# « VINCI Energies » / « VINCI Construction » restent séparées).
_COMPANY_SUFFIXES = {"services", "service", "solutions", "consulting", "conseil",
                     "technologies", "technology", "tech", "digital", "international",
                     "europe", "recrutement", "recruitment", "careers", "jobs", "emploi"}

TITLE_THRESHOLD = 0.75
DESCRIPTION_THRESHOLD = 0.5


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9+#]+", fold(text)) if w]


def normalize_company(name) -> str:
    """« ACME France SAS » → « acme » ; entreprise inconnue → ''."""
    low = fold(name).replace("&", " et ")
    low = re.sub(r"\([^)]*\)", " ", low)
    words = [w for w in _words(low) if w not in _LEGAL]
    out = " ".join(words)
    return "" if out in _UNKNOWN_COMPANY else out


def companies_match(a: str, b: str) -> bool:
    """Deux noms normalisés désignent-ils le même employeur ?"""
    if not a or not b:
        return False
    if a == b:
        return True
    wa, wb = a.split(), b.split()
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if long_[:len(short)] != short:
        return False
    return all(w in _COMPANY_SUFFIXES for w in long_[len(short):])


def title_tokens(title) -> set[str]:
    """Mots porteurs de l'intitulé (sans genre, contrat, mots vides)."""
    low = _GENDER.sub(" ", fold(title))
    low = _CONTRACT_WORDS.sub(" ", low)
    return {w for w in _words(low) if w not in _TITLE_STOP and not w.isdigit()}


def title_similarity(a, b) -> float:
    """Similarité d'intitulés : Jaccard, ou recouvrement quand l'un inclut l'autre."""
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    jaccard = inter / len(ta | tb)
    contain = inter / min(len(ta), len(tb))
    # « Lead Tech IA » ⊂ « Lead Tech IA DSI » : même offre si l'intitulé court
    # a au moins trois mots porteurs, tous présents (« AI Engineer » ⊂ « Data
    # Scientist / AI Engineer » ne suffit pas).
    if contain == 1.0 and min(len(ta), len(tb)) >= 3:
        return max(jaccard, 0.9)
    return jaccard


def place_tokens(location) -> set[str]:
    """Ville(s) d'une localisation : « Paris 02 - 75 » → {paris}."""
    return {w for w in _words(location) if w not in _PLACE_STOP and not w.isdigit()
            and len(w) > 1 and not re.fullmatch(r"\d+[ab]?|\d+(?:e|er|eme)", w)}


def places_compatible(a, b) -> bool:
    pa, pb = place_tokens(a), place_tokens(b)
    return not pa or not pb or bool(pa & pb)


def _shingles(text: str, k: int = 5) -> set[str]:
    words = _words(text)
    return {" ".join(words[i:i + k]) for i in range(max(0, len(words) - k + 1))}


def description_similarity(a, b) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def canonical_url(url) -> str:
    url = str(url or "").strip().lower()
    url = re.sub(r"^https?://(www\.)?", "", url)
    return url.split("?")[0].split("#")[0].rstrip("/")


def _get(job, key, default=""):
    if isinstance(job, dict):
        return job.get(key, default)
    return getattr(job, key, default)


# Agrégateurs qui relaient la même offre depuis plusieurs partenaires sous des
# liens différents : la règle « même site, deux liens » ne s'y applique pas.
AGGREGATOR_SOURCES = {"labonnealternance"}


def source_conflict(sources_a, sources_b) -> bool:
    """Vrai si un même site affiche les deux offres sous des liens différents.

    Un site ne publie pas deux fois la même annonce : deux liens distincts sur
    LinkedIn (ou deux numéros France Travail) sont deux offres, même si
    l'intitulé est proche (« Applied AI Engineer, Prototyping » / « …, AI for
    Mistral »). ``sources_*`` : itérables de ``(site, url)``.
    """
    by_site: dict[str, set[str]] = {}
    for site, url in sources_a:
        if site in AGGREGATOR_SOURCES:
            continue
        if site and canonical_url(url):
            by_site.setdefault(site, set()).add(canonical_url(url))
    for site, url in sources_b:
        mine = by_site.get(site)
        if mine and canonical_url(url) and canonical_url(url) not in mine:
            return True
    return False


def same_offer(a, b) -> bool:
    """Vrai si ``a`` et ``b`` (ORM ou dict) sont la même annonce."""
    ua, ub = canonical_url(_get(a, "url")), canonical_url(_get(b, "url"))
    if ua and ua == ub:
        return True
    ta, tb = _get(a, "title"), _get(b, "title")
    ca, cb = normalize_company(_get(a, "company")), normalize_company(_get(b, "company"))
    if ca and cb:
        if not companies_match(ca, cb):
            return False
    else:
        da, db_ = str(_get(a, "description") or ""), str(_get(b, "description") or "")
        if len(da) < 200 or len(db_) < 200:
            return False
        if description_similarity(da, db_) < DESCRIPTION_THRESHOLD:
            return False
    if not places_compatible(_get(a, "location"), _get(b, "location")):
        return False
    ka = contracts.qualify_contract(ta, "", _get(a, "contract"))
    kb = contracts.qualify_contract(tb, "", _get(b, "contract"))
    if ka and kb and not set(ka) & set(kb):
        return False
    la = contracts.extract_experience(ta, "")["level"]
    lb = contracts.extract_experience(tb, "")["level"]
    if la and lb and la != lb:
        return False
    return title_similarity(ta, tb) >= TITLE_THRESHOLD


# ---------------------------------------------------------------------------
# Index en mémoire des offres connues (une requête au début de l'import)
# ---------------------------------------------------------------------------

class JobIndex:
    """Offres connues indexées par ``external_id`` (alias compris) et entreprise."""

    def __init__(self, db):
        from .models import Job, JobSource
        self.db = db
        self.by_eid: dict[str, object] = {}
        self.by_company: dict[str, list] = {}
        self.anonymous: list = []
        self.sources: dict[str, object] = {}
        self.postings: dict[int, list[tuple[str, str]]] = {}
        jobs = {j.id: j for j in db.query(Job).all()}
        for j in jobs.values():
            self._index(j)
        for s in db.query(JobSource).all():
            self.sources[s.external_id] = s
            self.postings.setdefault(s.job_id, []).append((s.source, s.url))
            job = jobs.get(s.job_id)
            if job is not None:
                self.by_eid.setdefault(s.external_id, job)

    def _index(self, job) -> None:
        if job.external_id:
            self.by_eid[job.external_id] = job
        if job.id is not None and job.id not in self.postings:
            # Offre antérieure à job_sources : sa diffusion d'origine.
            self.postings[job.id] = [(str((job.explanation or {}).get("source") or ""),
                                      job.url or "")]
        key = normalize_company(job.company)
        if key:
            self.by_company.setdefault(key, []).append(job)
        else:
            self.anonymous.append(job)

    def add(self, job) -> None:
        self._index(job)

    def has_other_sources(self, job, eid: str) -> bool:
        """Vrai si ``job`` regroupe aussi des diffusions d'autres sites."""
        return any(s.job_id == job.id and s.external_id != eid
                   for s in self.sources.values())

    def by_external_id(self, eid: str):
        return self.by_eid.get(eid)

    def find_duplicate(self, record, exclude_id=None, source: str | None = None):
        """Offre déjà connue correspondant à ``record`` (autre site), sinon None."""
        key = normalize_company(_get(record, "company"))
        if key:
            pool = list(self.by_company.get(key, []))
            pool += [j for k, items in self.by_company.items()
                     if k != key and companies_match(key, k) for j in items]
        else:
            pool = self.anonymous
        site = source if source is not None else _get(record, "_source")
        mine = [(str(site or ""), str(_get(record, "url") or ""))]
        for job in pool:
            if exclude_id is not None and job.id == exclude_id:
                continue
            if source_conflict(self.postings.get(job.id, []), mine):
                continue
            if same_offer(record, job):
                return job
        return None

    def attach(self, job, source: str, external_id: str, url: str = "",
               published_at: datetime | None = None, now: datetime | None = None):
        """Trace (ou rafraîchit) la diffusion ``external_id`` de ``job``."""
        from .models import JobSource
        from .freshness import utcnow
        now = now or utcnow()
        row = self.sources.get(external_id)
        if row is None:
            row = JobSource(job_id=job.id, source=str(source or ""), external_id=external_id,
                            url=str(url or ""), published_at=published_at,
                            first_seen_at=now, last_seen_at=now)
            self.db.add(row)
            self.sources[external_id] = row
        else:
            row.job_id = job.id
            row.last_seen_at = now
            if url:
                row.url = str(url)
            if source:
                row.source = str(source)
            if published_at is not None:
                row.published_at = published_at
        self.by_eid[external_id] = job
        posting = (str(source or ""), str(url or ""))
        known = self.postings.setdefault(job.id, [])
        if posting not in known:
            known.append(posting)
        return row


def job_for_external_id(db, external_id: str):
    """Offre portant cet ``external_id`` directement ou comme diffusion rattachée."""
    from .models import Job, JobSource
    job = db.query(Job).filter_by(external_id=external_id).first()
    if job is not None:
        return job
    row = db.query(JobSource).filter_by(external_id=external_id).first()
    return db.get(Job, row.job_id) if row is not None else None


def sources_map(db) -> dict[int, list[dict]]:
    """``{job_id: [{source, url, published_at, last_seen_at}]}`` pour l'API."""
    from .models import JobSource
    from .freshness import iso
    out: dict[int, list[dict]] = {}
    for s in db.query(JobSource).order_by(JobSource.id).all():
        out.setdefault(s.job_id, []).append({
            "source": s.source, "url": s.url, "published_at": iso(s.published_at),
            "last_seen_at": iso(s.last_seen_at)})
    return out


# ---------------------------------------------------------------------------
# Fusion des doublons déjà présents en base
# ---------------------------------------------------------------------------

_MERGE_TEXT = ("title", "company", "location", "remote", "contract", "url")


def merge_into(db, keep, dup) -> None:
    """Fusionne l'offre ``dup`` dans ``keep`` : champs, diffusions, notes, dossiers."""
    from .models import Application, JobRating, JobSource
    from .freshness import aware
    for key in _MERGE_TEXT:
        if not str(getattr(keep, key) or "").strip() and str(getattr(dup, key) or "").strip():
            setattr(keep, key, getattr(dup, key))
    if len(dup.description or "") > len(keep.description or ""):
        keep.description = dup.description
    for key in ("salary_min", "salary_max"):
        if getattr(keep, key) is None and getattr(dup, key) is not None:
            setattr(keep, key, getattr(dup, key))
    for key in ("skills", "sectors"):
        merged = list(getattr(keep, key) or [])
        for v in getattr(dup, key) or []:
            if v not in merged:
                merged.append(v)
        setattr(keep, key, merged)
    for key in ("published_at", "last_seen_at", "expires_at"):
        a, b = aware(getattr(keep, key)), aware(getattr(dup, key))
        if b is not None and (a is None or b > a):
            setattr(keep, key, getattr(dup, key))
    if keep.decision in (None, "", "new") and dup.decision not in (None, "", "new"):
        keep.decision = dup.decision
    for row in db.query(JobSource).filter_by(job_id=dup.id).all():
        row.job_id = keep.id
    if dup.external_id and not db.query(JobSource).filter_by(external_id=dup.external_id).first():
        src = str((dup.explanation or {}).get("source") or "")
        db.add(JobSource(job_id=keep.id, source=src, external_id=dup.external_id,
                         url=dup.url or "", published_at=dup.published_at,
                         first_seen_at=dup.created_at or dup.last_seen_at,
                         last_seen_at=dup.last_seen_at or dup.created_at))
    for app_row in db.query(Application).filter_by(job_id=dup.id).all():
        app_row.job_id = keep.id
    for rating in db.query(JobRating).filter_by(job_id=dup.id).all():
        mine = db.query(JobRating).filter_by(user_id=rating.user_id, job_id=keep.id).first()
        if mine is None:
            rating.job_id = keep.id
            continue
        if mine.decision in (None, "", "new") and rating.decision not in (None, "", "new"):
            mine.decision = rating.decision
        db.delete(rating)
    db.flush()
    db.delete(dup)


def _keep_rank(job, with_app: set) -> tuple:
    decided = job.decision not in (None, "", "new")
    return (job.id not in with_app, not decided, job.id)


def ensure_sources(db) -> int:
    """Crée la diffusion d'origine des offres importées avant ``job_sources``."""
    from .models import Job, JobSource
    known = {s.external_id for s in db.query(JobSource).all()}
    n = 0
    for job in db.query(Job).all():
        if job.external_id and job.external_id not in known:
            db.add(JobSource(job_id=job.id, source=str((job.explanation or {}).get("source") or ""),
                             external_id=job.external_id, url=job.url or "",
                             published_at=job.published_at,
                             first_seen_at=job.created_at or job.last_seen_at,
                             last_seen_at=job.last_seen_at or job.created_at))
            known.add(job.external_id)
            n += 1
    if n:
        db.flush()
    return n


def merge_duplicates(db) -> int:
    """Fusionne les doublons inter-sites déjà en base ; renvoie le nombre fusionné.

    L'offre conservée est celle qui porte un dossier de candidature, sinon une
    décision (retenue/ignorée), sinon la plus ancienne.
    """
    from .models import Application, Job
    ensure_sources(db)
    from .models import JobSource
    with_app = {row[0] for row in db.query(Application.job_id).all()}
    postings: dict[int, list[tuple[str, str]]] = {}
    for row in db.query(JobSource).all():
        postings.setdefault(row.job_id, []).append((row.source, row.url))
    jobs = sorted(db.query(Job).all(), key=lambda j: _keep_rank(j, with_app))
    kept: list = []
    by_company: dict[str, list] = {}
    anonymous: list = []
    merged = 0
    for job in jobs:
        key = normalize_company(job.company)
        pool = by_company.get(key, []) if key else anonymous
        if key:
            pool = pool + [j for k, items in by_company.items()
                           if k != key and companies_match(key, k) for j in items]
        mine = postings.get(job.id, [])
        target = next((k for k in pool if not source_conflict(postings.get(k.id, []), mine)
                       and same_offer(job, k)), None)
        if target is not None:
            merge_into(db, target, job)
            postings.setdefault(target.id, []).extend(mine)
            merged += 1
            continue
        kept.append(job)
        (by_company.setdefault(key, []) if key else anonymous).append(job)
    db.commit()
    return merged
