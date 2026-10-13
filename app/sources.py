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
def upsert_jobs(db,records,provider=None,profile_data=None,top_n: int | None = None, llm_budget: int | None = None):
 from .models import Job
 # Passe 1 (gratuite, sur toutes les annonces) : lexique + heuristique + pré-note.
 # Passe 2 (coûteuse/LLM) : uniquement le top-N pré-noté de chaque site.
 selected, prescored = prescore_and_select(records, profile_data, top_n)
 selected_ids = {id(r) for r in selected}
 if llm_budget is None:
  llm_budget = _llm_budget_for(len(selected))
 enriched = enrich_offers(list(selected), provider=provider, llm_budget=llm_budget)
 enriched_by_id = {id(r): e for r, e in zip(selected, enriched)}
 final = [(enriched_by_id.get(id(r), r) if id(r) in selected_ids else r) for r in prescored]
 n=0
 for r in final:
  r = dict(r)
  prescore = r.pop("_prescore", None)
  source = r.pop("_source", None); r.pop("_selected", None)
  eid=str(r.get("external_id") or r.get("id") or f"manual-{r.get('title','')}-{r.get('company','')}"); j=db.query(Job).filter_by(external_id=eid).first()
  if not j:j=Job(external_id=eid);db.add(j);n+=1
  for k in ["title","company","location","remote","contract","url","description"]:setattr(j,k,str(r.get(k) or ""))
  for k in ["salary_min","salary_max"]:setattr(j,k,int(r[k]) if r.get(k) not in (None,"") else None)
  for k in ["sectors","skills"]:
   v=r.get(k,[]);setattr(j,k,v if isinstance(v,list) else [x.strip() for x in str(v).split(",") if x.strip()])
  # Provenance (affichée dans l'UI) et pré-note : conservées dans `explanation`
  # car ce sont les seules informations que `Job` ne porte pas ailleurs.
  try:
   expl = dict(j.explanation or {})
   if source: expl["source"] = str(source)
   if prescore is not None: expl["prescore"] = float(prescore)
   if expl: j.explanation = expl
  except Exception:
   pass
 db.commit();return n
