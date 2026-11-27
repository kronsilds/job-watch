import csv,json,os
from .scrapers import enrich_skills_from_text
from .offer_enrichment import enrich_offers, heuristic_completion
def read_records(path):
 if path.suffix.lower()==".json": return json.loads(path.read_text())
 if path.suffix.lower()==".csv":
  with path.open(encoding="utf-8",newline="") as f:return list(csv.DictReader(f))
 raise ValueError("Source accepté: JSON ou CSV")
def _top_n_per_source() -> int:
 """Nombre d'annonces enrichies (LLM) par site après pré-note. Défaut 5."""
 try:
  return max(1, int(os.getenv("JOBWATCH_TOP_N_PER_SOURCE", "5") or 5))
 except (TypeError, ValueError):
  return 5
def _llm_budget_for(selected_len: int) -> int:
 """Budget LLM aligné sur la sélection top-N (défaut = tout le top-N).

 Si ``JOBWATCH_ENRICH_LLM_MAX`` est explicitement défini, il reste un
 plafond global (garde-fou coût) ; sinon on autorise l'enrichissement de
 tout le top-N (ex. 7 sites × 5 = 35 appels max).
 """
 import os as _os
 raw = _os.getenv("JOBWATCH_ENRICH_LLM_MAX")
 if raw in (None, ""):
  return max(0, int(selected_len))
 try:
  return max(0, int(raw))
 except (TypeError, ValueError):
  return max(0, int(selected_len))
def prescore_and_select(records, profile_data=None, top_n: int | None = None):
 """Pré-note heuristique (gratuite) + sélection des N meilleures par site.

 Passe 1 (gratuite) : lexique skills/secteurs + complétion heuristique, puis
 score heuristique ``score_job`` sur dict (aucun appel LLM/réseau).
 Passe 2 (hors de cette fonction) : seules les ``top_n`` annonces par site
 reçoivent la complétion coûteuse (LLM light) avant la note finale.

 Retourne ``(selected, prescored)`` où ``prescored`` porte ``_prescore``
 et ``_source`` (transitoires, jamais persistés tels quels), et ``selected``
 est le sous-ensemble à enrichir en priorité. Sans ``profile_data``
 (import hors profil validé), tout est sélectionné (pas de tri possible).
 """
 from .scoring import score_job
 if top_n is None:
  top_n = _top_n_per_source()
 cheap = [heuristic_completion(enrich_skills_from_text(dict(r))) for r in records]
 if not profile_data:
  for r in cheap:
   r.setdefault("_source", r.get("source") or "file")
  return list(cheap), cheap
 for r in cheap:
  try:
   s, _c, _e = score_job(r, profile_data or {})
  except Exception:
   s = 0.0
  r["_prescore"] = s
  r.setdefault("_source", r.get("source") or r.get("_source") or "file")
 by_source: dict[str, list[dict]] = {}
 for r in cheap:
  by_source.setdefault(str(r.get("_source") or "file"), []).append(r)
 selected: list[dict] = []
 for _src, items in by_source.items():
  items.sort(key=lambda r: (r.get("_prescore") or 0.0), reverse=True)
  for r in items[:max(1, top_n)]:
   r["_selected"] = True
   selected.append(r)
 return selected, cheap
_TEXT_FIELDS = ("title", "company", "location", "remote", "contract", "url", "description")


def _record_dates(r: dict, now):
 """Dates portées par un enregistrement (ISO, relatives…) → datetimes UTC."""
 from .freshness import parse_date
 pub = parse_date(r.get("published_at") or r.get("datePosted") or r.get("date_posted"), now)
 exp = parse_date(r.get("expires_at") or r.get("validThrough"), now)
 if pub and pub > now:
  pub = now  # date future = erreur de source : bornée à aujourd'hui
 return pub, exp


def _overwrite(j, r: dict) -> None:
 """Même diffusion ré-importée : ses champs remplacent ceux de l'offre."""
 for k in _TEXT_FIELDS:
  setattr(j, k, str(r.get(k) or ""))
 for k in ["salary_min", "salary_max"]:
  setattr(j, k, int(r[k]) if r.get(k) not in (None, "") else None)
 for k in ["sectors", "skills"]:
  v = r.get(k, [])
  setattr(j, k, v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()])


def _fill(j, r: dict) -> None:
 """Autre site pour la même offre : on ne fait que compléter (jamais écraser)."""
 for k in _TEXT_FIELDS:
  val = str(r.get(k) or "").strip()
  if not val:
   continue
  cur = str(getattr(j, k) or "").strip()
  if not cur or cur in ("Inconnue", "Sans titre") or (k == "description" and len(val) > len(cur)):
   setattr(j, k, val)
 for k in ["salary_min", "salary_max"]:
  if getattr(j, k) is None and r.get(k) not in (None, ""):
   setattr(j, k, int(r[k]))
 for k in ["sectors", "skills"]:
  v = r.get(k, [])
  v = v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]
  merged = list(getattr(j, k) or [])
  for x in v:
   if x not in merged:
    merged.append(x)
  setattr(j, k, merged)


def upsert_jobs(db,records,provider=None,profile_data=None,top_n: int | None = None, llm_budget: int | None = None, stats: dict | None = None, fetch_details: bool = False):
 """Enregistre les annonces : pré-note, complétion ciblée, rapprochement inter-sites.

 Une annonce déjà vue sur un autre site (même entreprise, intitulé proche,
 ville compatible : :mod:`app.dedup`) est rattachée à l'offre existante au
 lieu d'en créer une nouvelle ; chaque diffusion est tracée dans
 ``job_sources``. Les dates (publication, limite, dernière apparition) sont
 mises à jour à chaque passage. Retourne le nombre d'offres **nouvelles** ;
 ``stats`` (facultatif) reçoit ``{"created", "merged", "updated"}``.
 ``fetch_details`` (veille web) récupère le texte complet des annonces
 LinkedIn et HelloWork retenues, que la page de recherche ne fournit pas.
 """
 from .dedup import JobIndex
 from .freshness import aware, utcnow
 from .models import Job
 # Passe 1 (gratuite, sur toutes les annonces) : lexique + heuristique + pré-note.
 # Passe 2 (coûteuse/LLM) : uniquement le top-N pré-noté de chaque site.
 selected, prescored = prescore_and_select(records, profile_data, top_n)
 selected_ids = {id(r) for r in selected}
 if llm_budget is None:
  llm_budget = _llm_budget_for(len(selected))
 if fetch_details:
  from .websearch import fill_details
  fill_details(selected)  # avant la complétion : elle lit ce texte
 enriched = enrich_offers(list(selected), provider=provider, llm_budget=llm_budget)
 enriched_by_id = {id(r): e for r, e in zip(selected, enriched)}
 final = [(enriched_by_id.get(id(r), r) if id(r) in selected_ids else r) for r in prescored]
 index = JobIndex(db)
 now = utcnow()
 counts = {"created": 0, "merged": 0, "updated": 0}
 for r in final:
  r = dict(r)
  prescore = r.pop("_prescore", None)
  source = r.pop("_source", None); r.pop("_selected", None)
  # Offre toujours affichée par l'entreprise (page carrières) ou « Active » chez
  # La bonne alternance : confirmée en ligne, présentée quel que soit son âge.
  live = bool(r.pop("_live", False))
  eid=str(r.get("external_id") or r.get("id") or f"manual-{r.get('title','')}-{r.get('company','')}")
  j = index.by_external_id(eid)
  is_new = False
  if j is not None:
   # Diffusion déjà connue : si l'offre en regroupe plusieurs, on complète
   # seulement (ne pas perdre la description plus riche d'un autre site).
   if j.external_id == eid and not index.has_other_sources(j, eid):
    _overwrite(j, r)
   else:
    _fill(j, r)
   counts["updated"] += 1
  else:
   j = index.find_duplicate(r, source=source or "")
   if j is not None:
    _fill(j, r)
    counts["merged"] += 1
   else:
    j = Job(external_id=eid); db.add(j)
    _overwrite(j, r)
    counts["created"] += 1; is_new = True
  pub, exp = _record_dates(r, now)
  if pub and (aware(j.published_at) is None or pub > aware(j.published_at)):
   j.published_at = pub
  if exp and (aware(j.expires_at) is None or exp > aware(j.expires_at)):
   j.expires_at = exp
  j.last_seen_at = now
  if j.availability is None or live or (j.availability == "expired" and (exp is None or exp > now)):
   # Revue dans une recherche : de nouveau en ligne.
   j.availability = "active"; j.availability_note = ""
  if live:
   j.live_confirmed_at = now
  # Provenance (affichée dans l'UI) et pré-note : conservées dans `explanation`
  # car ce sont les seules informations que `Job` ne porte pas ailleurs.
  try:
   expl = dict(j.explanation or {})
   if source and not expl.get("source"): expl["source"] = str(source)
   if prescore is not None: expl["prescore"] = float(prescore)
   if expl: j.explanation = expl
  except Exception:
   pass
  db.flush()
  if is_new:
   index.add(j)
  index.attach(j, source or "", eid, str(r.get("url") or ""), pub, now)
 db.commit()
 if stats is not None:
  stats.update(counts)
 return counts["created"]


def backfill_details(db, limit: int = 150, on_progress=None, max_age: int | None = None) -> dict:
 """Rattrape le texte des annonces déjà en base qui n'en ont pas (LinkedIn, HelloWork).

 Une requête par annonce (quota et cache de la veille), les offres actives
 les mieux notées d'abord. La page sert aussi de vérification : offre
 retirée (404/410) ou close → marquée terminée. Ne remplace jamais une
 donnée existante ; s'arrête au quota épuisé. Relançable : les annonces déjà
 complétées sont ignorées. Les annonces trop anciennes pour être affichées
 (plus de ``max_age`` jours, par défaut le plus grand ``max_age_days`` des
 profils validés) ne sont pas rattrapées : inutile de dépenser le quota.
 """
 import httpx
 from . import freshness, websearch
 from .models import Job, Profile
 from .scrapers import enrich_skills_from_text
 if max_age is None:
  ages = [freshness.max_age_days(p.data) for p in db.query(Profile).filter_by(status='validated').all()]
  max_age = max(ages) if ages else freshness.max_age_days()
 candidates = [j for j in db.query(Job).all()
               if websearch.detail_page(j.url) and not str(j.description or "").strip()]
 jobs = [j for j in candidates if freshness.freshness(j, max_age)["fresh"]]
 jobs.sort(key=lambda j: (-(j.score or 0), -(j.created_at.timestamp() if j.created_at else 0)))
 res = {"todo": len(jobs), "too_old": len(candidates) - len(jobs), "max_age": max_age,
        "filled": 0, "expired": 0, "failed": 0, "quota": False}
 for j in jobs[:limit]:
  url, parse = websearch.detail_page(j.url)
  try:
   html = websearch.fetch_page(url, ttl=86400)
  except websearch.BudgetExceeded:
   res["quota"] = True
   break
  except httpx.HTTPStatusError as exc:
   code = exc.response.status_code if exc.response is not None else 0
   if code in (404, 410):
    freshness.apply_check(j, {"status": "expired", "reason": f"la page de l'annonce n'existe plus (HTTP {code})"})
    res["expired"] += 1; db.commit()
   else:
    res["failed"] += 1
   continue
  except Exception:
   res["failed"] += 1
   continue
  status, reason = freshness.page_status(html, url, url)
  freshness.apply_check(j, {"status": status, "reason": reason})
  if status == "expired":
   res["expired"] += 1
  details = parse(html)
  if str(details.get("description") or "").strip():
   j.description = details["description"]
   if details.get("contract") and not j.contract:
    j.contract = details["contract"]
   if details.get("sectors") and not j.sectors:
    j.sectors = details["sectors"]
   extra = enrich_skills_from_text({"title": j.title, "description": j.description,
                                    "skills": list(j.skills or []), "sectors": list(j.sectors or [])})
   j.skills = extra.get("skills") or j.skills
   j.sectors = extra.get("sectors") or j.sectors
   if status != "expired":
    res["filled"] += 1
  elif status != "expired":
   res["failed"] += 1
  db.commit()
  if on_progress:
   on_progress(res)
 return res


backfill_linkedin = backfill_details  # nom historique
