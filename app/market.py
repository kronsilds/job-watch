from collections import Counter
from .llm import get_provider
def analyze(db):
 from .models import Job
 jobs=db.query(Job).all(); top=lambda xs:Counter(x for a in xs for x in a).most_common(10)
 return {"jobs_count":len(jobs),"skills":top([j.skills or [] for j in jobs]),"sectors":top([j.sectors or [] for j in jobs]),"locations":Counter(j.location for j in jobs).most_common(10),"titles":Counter(j.title for j in jobs).most_common(10)}
def summarize(market):
 try:
  return get_provider().summarize_market(market)
 except Exception as exc:
  return {"summary":f"{market.get('jobs_count',0)} offres indexées.","trends":[],"recommendation":"Analyse LLM indisponible: "+str(exc)}
def analyze_market(profile, db):
 """Analyseur de marché complet : faits collectés (heuristique) + analyse LLM (hypothèses)."""
 market=analyze(db)
 try:
  enriched=get_provider().analyze_market(profile, market)
 except Exception as exc:
  enriched={"summary":f"{market.get('jobs_count',0)} offres indexées.","trends":[],"keywords":[],"seniorities":[],"angles":[],"hypotheses":["Analyse LLM indisponible: "+str(exc)],"recommendation":"Vérifiez la configuration LLM."}
 enriched["facts"]=market
 return enriched
