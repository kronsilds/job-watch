"""LLM provider boundary with heavy/light model tiers.

Prompts are loaded from ``app/data/prompts.json`` (not inline). Each task maps
to a tier: heavy tasks (deep CV analysis, market synthesis) use
``LLM_MODEL_HEAVY``; light tasks (writing a pitch/email, classification) use
``LLM_MODEL_LIGHT``. When a remote provider is unavailable (or unconfigured)
we fall back to a deterministic, fact-only engine that never invents facts.
"""

from __future__ import annotations

import json
import os
import re

from dotenv import load_dotenv
from openai import OpenAI

import httpx

from .prompts import prompts as prompt_renderer

load_dotenv()


# Modèles par défaut selon le fournisseur actif (surchargeables via LLM_MODEL*).
_DEFAULT_MODELS = {
    "anthropic": ("claude-sonnet-5-5", "claude-haiku-5-5"),
}
_DEFAULT_HEAVY, _DEFAULT_LIGHT = "openai/gpt-4o", "openai/gpt-4o-mini"


def effective_provider() -> str:
    """Fournisseur réellement actif (décode aussi ``LLM_PROVIDER=auto``).

    - ``compatible`` : endpoint OpenAI-compatible (OpenRouter, OpenAI…) via
      ``LLM_API_KEY`` ;
    - ``anthropic`` : API Anthropic (Claude) via ``ANTHROPIC_API_KEY``, un
      bearer (``ANTHROPIC_AUTH_TOKEN`` / ``CLAUDE_CODE_OAUTH_TOKEN``) ou un
      compte Claude connecté en OAuth (``app/providers/claude_oauth.py``) ;
    - ``local`` : aucun modèle distant, mode déterministe/heuristique.
    """
    provider = os.getenv("LLM_PROVIDER", "auto").lower()
    if provider == "auto":
        if os.getenv("LLM_API_KEY"):
            return "compatible"
        if (os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN")
                or os.getenv("CLAUDE_CODE_OAUTH_TOKEN")):
            return "anthropic"
        from .providers import claude_oauth
        if claude_oauth.connected():
            return "anthropic"
        return "local"
    return provider


def _model(heavy: bool) -> str:
    """Pick the model for a tier, with sensible fallbacks."""
    defaults = _DEFAULT_MODELS.get(
        effective_provider(), (_DEFAULT_HEAVY, _DEFAULT_LIGHT))
    if heavy:
        return os.getenv("LLM_MODEL_HEAVY") or os.getenv("LLM_MODEL") or defaults[0]
    return os.getenv("LLM_MODEL_LIGHT") or os.getenv("LLM_MODEL") or defaults[1]


def _parse_json(content: str) -> dict:
    """Parse la réponse du modèle en objet JSON (tolère les fences ```json).

    Anthropic n'impose pas de ``response_format`` strict : le texte peut être
    encadré de fences Markdown ou de phrases, on extrait l'objet JSON.
    """
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        result = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("Réponse LLM invalide (JSON attendu)")
        try:
            result = json.loads(text[start:end + 1])
        except ValueError:
            raise RuntimeError("Réponse LLM invalide (JSON attendu)")
    if not isinstance(result, dict):
        raise RuntimeError("Réponse LLM invalide")
    return result


class LLMProvider:
    name = "base"
    model_used = ""

    def generate(self, prompt: str, facts: dict) -> dict:
        raise NotImplementedError

    def analyze_profile(self, cv_text: str) -> dict:
        raise NotImplementedError

    def summarize_market(self, market: dict) -> dict:
        raise NotImplementedError

    def analyze_market(self, profil: dict, market: dict) -> dict:
        raise NotImplementedError

    def generate_cv(self, facts: dict) -> dict:
        raise NotImplementedError

    def complete_profile(self, cv_text: str) -> dict:
        raise NotImplementedError

    def refine_profile(self, instruction: str, profile: dict,
                       cv_text: str = "") -> dict:
        """Passe lourde : affine la fiche de profil selon une consigne libre.

        Renvoie ``{"profile": {...}, "changes_summary": "..."}`` quand un modèle
        est disponible, sinon ``{"profile": {}, "warning": "..."}`` : la fiche
        est alors renvoyée inchangée (jamais de modification inventée).
        """
        raise NotImplementedError

    def enrich_offer(self, offer: dict) -> dict:
        """Passe light : extrait les champs manquants d'une annonce."""
        raise NotImplementedError

    def complete_task(self, task: str, **kwargs) -> dict:
        """Exécute une tâche générique déclarée dans ``prompts.json``
        (ex. ``writer_pitch``, ``writer_cv_ats``, ``reviewer``)."""
        raise NotImplementedError


class DeterministicProvider(LLMProvider):
    name = "deterministic"

    def generate(self, prompt: str, facts: dict) -> dict:
        title = facts.get("job_title", "le poste")
        company = facts.get("company", "votre entreprise")
        skills = facts.get("matched_skills", [])

        pitch = (
            f"Je souhaite vous proposer ma candidature "
            f"au poste de {title} chez {company}."
        )
        if skills:
            pitch += (
                " Mon profil documente notamment : "
                + ", ".join(skills)
                + "."
            )
        pitch += (
            " Je serais heureux·se d’échanger sur "
            "l’adéquation avec vos besoins."
        )

        return {
            "pitch": pitch,
            "email_subject": f"Candidature — {title}",
            "email_body": (
                f"Bonjour,\n\n{pitch}\n\n"
                "Vous trouverez mon CV en pièce jointe.\n\n"
                "Cordialement,\nCandidat·e"
            ),
        }

    def analyze_profile(self, cv_text: str) -> dict:
        from .profile_analysis import heuristic_analysis
        return heuristic_analysis(cv_text)

    def complete_profile(self, cv_text: str) -> dict:
        # Light complement used when the heuristic baseline is sparse; the
        # deterministic provider has no model, so it falls back to the
        # same fact-only heuristic extraction.
        from .profile_analysis import heuristic_analysis
        return heuristic_analysis(cv_text)

    def refine_profile(self, instruction: str, profile: dict,
                       cv_text: str = "") -> dict:
        # Sans modèle, aucune consigne libre ne peut être interprétée : la fiche
        # est renvoyée inchangée, avec un avertissement explicite (aucune
        # modification n'est inventée).
        return {
            "profile": {},
            "changes_summary": "",
            "warning": ("Aucun modèle IA configuré (clé API absente) : la "
                        "consigne n'a pas pu être appliquée, la fiche est inchangée."),
        }

    def enrich_offer(self, offer: dict) -> dict:
        # Sans modèle : complétion heuristique fact-only (lexique) des champs
        # vides de l'annonce — gratuite, hors-ligne, n'invente rien.
        from .offer_enrichment import heuristic_completion
        return heuristic_completion(dict(offer))

    def generate_cv(self, facts: dict) -> dict:
        """Fact-only, ATS-oriented CV built locally when no model is available."""
        title = facts.get("job_title") or "le poste"
        company = facts.get("company") or ""
        main = facts.get("main_job") or title
        secondary = facts.get("secondary_job") or ""
        years = facts.get("years_experience")
        profile_skills = [str(s) for s in facts.get("profile_skills") or []]
        matched = [str(s) for s in facts.get("matched_skills") or []]
        highlight_skills = matched or profile_skills

        head = f"Profil orienté {title}" + (f" chez {company}" if company else "") + "."
        if years:
            head += f" {years} ans d'expérience."
        if secondary:
            head += f" Métier connexe : {secondary}."
        target = (
            "Compétences directement liées à la cible : "
            + ", ".join(matched) + "."
            if matched
            else ("Compétences documentées : " + ", ".join(profile_skills) + "."
                  if profile_skills else "- Aucune compétence documentée dans le profil.")
        )
        lines = [
            "CURRICULUM VITAE", "=" * 30, "",
            "OBJECTIF", head, "",
            "COMPÉTENCES CLÉS",
            *[f"- {s}" for s in highlight_skills], "",
            "COMPÉTENCES / RÉALISATIONS", target, "",
            "MÉTIERS",
            *([f"- {main}"] + ([f"- {secondary}"] if secondary else [])),
        ]
        return {
            "cv_ats": "\n".join(lines),
            "summary": head,
            "skills": highlight_skills,
            "highlights": highlight_skills,
        }

    def summarize_market(self, market: dict) -> dict:
        top_skill = ""
        if market.get("skills"):
            top_skill = market["skills"][0][0]
        return {
            "summary": f"{market.get('jobs_count', 0)} offres indexées. "
                       f"Compétence la plus demandée : {top_skill or 'n.c.'}.",
            "trends": [],
            "recommendation": "Consultez la veille et affinez le profil pour améliorer le scoring.",
        }

    def analyze_market(self, profil: dict, market: dict) -> dict:
        mots = " ; ".join(profil.get("metiers_vises") or profil.get("jobs") or profil.get("metiers") or [])
        top = market.get("skills") or []
        return {
            "summary": f"Analyse heuristique. {market.get('jobs_count', 0)} offres indexées.",
            "trends": [f"Compétence la plus demandée : {top[0][0]}" if top else "Peu d'offres collectées."],
            "keywords": [mots] if mots else [],
            "seniorities": profil.get("seniorite") or [],
            "angles": ["Étendre la localisation", "Ajouter des synonymes de métier", "Relancer la veille après mise à jour du profil"],
            "hypotheses": ["Analyse non enrichie : configurez un fournisseur LLM pour des hypothèses raisonnées."],
            "recommendation": "Complétez le profil (sources, métiers visés) puis relancez une campagne.",
        }

    def complete_task(self, task: str, **kwargs) -> dict:
        """Repli local sans modèle pour les tâches de la boucle de raffinage.

        Le rédacteur réutilise les gabarits factuels existants (pitch / CV ATS) ;
        l'évaluateur note la couverture des mots-clés de l'offre et la présence
        de l'intitulé du poste, sans jamais inventer de fait.
        """
        job = kwargs.get("job_offer") or {}
        prof = kwargs.get("candidate_profile") or {}
        job_skills = [str(s).lower() for s in job.get("skills") or []]
        profile_skills = {str(s).lower() for s in prof.get("skills") or []}
        matched = sorted(profile_skills & set(job_skills))
        if task in ("writer_pitch", "writer_cv_ats"):
            facts = {
                "job_title": job.get("title", ""), "company": job.get("company", ""),
                "matched_skills": matched, "profile_skills": sorted(profile_skills),
                "main_job": prof.get("main_job") or "",
                "secondary_job": prof.get("secondary_job") or "",
                "years_experience": prof.get("years_experience"),
                "profile_summary": prof.get("profile_summary") or "",
            }
            if task == "writer_pitch":
                document = self.generate("", facts)["pitch"]
            else:
                document = self.generate_cv(facts)["cv_ats"]
            return {"document": document,
                    "changes_summary": "Génération déterministe (sans modèle)"}
        if task == "reviewer":
            document = str(kwargs.get("current_draft") or "").lower()
            expected = [s for s in job_skills if s in profile_skills] or job_skills[:5]
            covered = [s for s in expected if s and s in document]
            kw = (10.0 * len(covered) / len(expected)) if expected else 6.0
            title_hit = bool(job.get("title")) and str(job["title"]).lower() in document
            score = min(10.0, round(0.6 * kw + (4.0 if title_hit else 2.0), 1))
            missing = [s for s in expected if s not in covered]
            feedback = ("Points forts : mots-clés de l'offre couverts." if not missing
                        else "- Reprendre les mots-clés manquants : " + ", ".join(missing))
            return {
                "score": score, "feedback": feedback,
                "criteria": {
                    "pertinence": 8.0 if title_hit else 5.0,
                    "mots_cles": round(kw, 1), "clarte": 7.0,
                    "personnalisation": 7.0 if str(job.get("company", "")).lower() in document else 5.0,
                    "format_ats": 7.0,
                },
            }
        raise NotImplementedError(f"Tâche inconnue : {task}")


class RemoteProvider(LLMProvider):
    """Couche « tâches » commune aux fournisseurs distants (sortie JSON).

    Chaque tâche rend un gabarit de ``app/data/prompts.json`` et délègue
    l'appel au transport :meth:`_complete`, qui renvoie un objet JSON. Les
    transports concrets (OpenAI-compatible, Anthropic) n'implémentent que
    ``_complete`` : toute la logique de prompts/validation est partagée.
    """

    provider_key = "openai"

    def _complete(self, system: str, user: str, model: str, temperature: float) -> dict:
        raise NotImplementedError

    def generate(self, prompt: str, facts: dict) -> dict:
        # light tier: writing task, fast model is enough
        model = _model(heavy=False)
        system, user = prompt_renderer.render(
            "generate",
            instruction=prompt,
            facts=json.dumps(facts, ensure_ascii=False, indent=2),
        )
        self.model_used = model
        result = self._complete(system, user, model, temperature=0.2)
        required = {"pitch", "email_subject", "email_body"}
        if not required.issubset(result):
            raise RuntimeError("Réponse LLM incomplète")
        return {key: str(result[key]) for key in required}

    def analyze_profile(self, cv_text: str) -> dict:
        # heavy tier: deep CV understanding
        model = _model(heavy=True)
        system, user = prompt_renderer.render("analyze_profile", cv_text=cv_text)
        self.model_used = model
        return self._complete(system, user, model, temperature=0.1)

    def complete_profile(self, cv_text: str) -> dict:
        # light tier: quick enrichment to complete a sparse heuristic extraction
        model = _model(heavy=False)
        system, user = prompt_renderer.render("complete_profile", cv_text=cv_text)
        self.model_used = model
        return self._complete(system, user, model, temperature=0.1)

    def refine_profile(self, instruction: str, profile: dict,
                       cv_text: str = "") -> dict:
        # heavy tier: la consigne porte sur toute la fiche (métier, ciblage,
        # compétences), il faut la comprendre et la reformuler sans inventer.
        model = _model(heavy=True)
        system, user = prompt_renderer.render(
            "refine_profile",
            instruction=instruction,
            profile=json.dumps(profile, ensure_ascii=False, indent=2),
            cv_text=cv_text.strip() or (
                "(aucun CV importé : la fiche actuelle est la seule base factuelle)"),
        )
        self.model_used = model
        result = self._complete(system, user, model, temperature=0.2)
        refined = result.get("profile")
        if not isinstance(refined, dict) or not refined:
            raise RuntimeError("Réponse LLM invalide (profil affiné manquant)")
        return {"profile": refined,
                "changes_summary": str(result.get("changes_summary") or "")}

    def enrich_offer(self, offer: dict) -> dict:
        # light tier: extraction ciblée des champs manquants de l'annonce
        model = _model(heavy=False)
        known = {k: offer.get(k) for k in
                 ("title", "company", "location", "remote", "contract",
                  "salary_min", "salary_max", "sectors", "skills")
                 if offer.get(k) not in (None, "", [])}
        text = (str(offer.get("title") or "") + "\n\n"
                + str(offer.get("description") or ""))[:4000]  # borne la taille du prompt
        system, user = prompt_renderer.render(
            "enrich_offer",
            offer_text=text,
            known_fields=json.dumps(known, ensure_ascii=False, indent=2),
        )
        self.model_used = model
        result = self._complete(system, user, model, temperature=0.1)
        if not isinstance(result, dict):
            raise RuntimeError("Réponse LLM invalide")
        return result

    def generate_cv(self, facts: dict) -> dict:
        # heavy tier: ATS-tailored, fact-based CV rewriting
        model = _model(heavy=True)
        system, user = prompt_renderer.render(
            "generate_cv",
            facts=json.dumps(facts, ensure_ascii=False, indent=2),
        )
        self.model_used = model
        result = self._complete(system, user, model, temperature=0.2)
        if not result.get("cv_ats"):
            raise RuntimeError("Réponse LLM incomplète (cv_ats manquant)")
        return {
            "cv_ats": str(result.get("cv_ats", "")),
            "summary": str(result.get("summary", "")),
            "skills": [str(x) for x in result.get("skills") or []],
            "highlights": [str(x) for x in result.get("highlights") or []],
        }

    def summarize_market(self, market: dict) -> dict:
        model = _model(heavy=True)
        system, user = prompt_renderer.render(
            "market_summary", market=json.dumps(market, ensure_ascii=False, indent=2)
        )
        self.model_used = model
        return self._complete(system, user, model, temperature=0.2)

    def analyze_market(self, profil: dict, market: dict) -> dict:
        model = _model(heavy=True)
        system, user = prompt_renderer.render(
            "market_analysis",
            profil=json.dumps(profil, ensure_ascii=False, indent=2),
            market=json.dumps(market, ensure_ascii=False, indent=2),
        )
        self.model_used = model
        return self._complete(system, user, model, temperature=0.2)

    def complete_task(self, task: str, **kwargs) -> dict:
        """Tâche générique rendue depuis ``prompts.json`` (tier heavy/light)."""
        temperature = float(kwargs.pop("temperature", 0.3))
        model = _model(heavy=prompt_renderer.tier_for(task) == "heavy")
        system, user = prompt_renderer.render(task, **kwargs)
        self.model_used = model
        result = self._complete(system, user, model, temperature=temperature)
        if not isinstance(result, dict):
            raise RuntimeError("Réponse LLM invalide")
        return result


class OpenAICompatibleProvider(RemoteProvider):
    """Transport OpenAI-compatible : SDK ``openai`` puis repli HTTP direct."""

    name = "llm"
    provider_key = "openai"

    def __init__(self):
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            raise RuntimeError("LLM_API_KEY manquante")
        self.model_used = _model(heavy=True)
        self.client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1"),
            default_headers={
                "HTTP-Referer": "http://localhost:8000",
                "X-OpenRouter-Title": "Job Watch",
            },
        )

    def _complete(self, system: str, user: str, model: str, temperature: float) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # 1) Voie principale : SDK openai (OpenAI-compatible).
        try:
            response = self.client.chat.completions.create(
                model=model,
                temperature=temperature,
                messages=messages,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content
            if not content:
                raise RuntimeError("Réponse LLM vide")
            return _parse_json(content)
        except Exception as exc:
            print(f"[LLM] SDK openai échoué, repli HTTP direct : {exc}")

        # 2) Repli sans dépendance : appel HTTP direct à l'endpoint compatible OpenAI.
        base = os.getenv("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
        api_key = os.getenv("LLM_API_KEY")
        try:
            with httpx.Client(timeout=60) as client:
                r = client.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                        "response_format": {"type": "json_object"},
                    },
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
            return _parse_json(content)
        except Exception as exc:
            raise RuntimeError(f"Échec LLM (SDK puis HTTP) : {exc}")


class AnthropicProvider(RemoteProvider):
    """Transport Anthropic (Claude) : SDK ``anthropic`` puis repli HTTP direct.

    Modes d'authentification (ordre de priorité) :

    1. ``ANTHROPIC_API_KEY`` — clé classique, header ``x-api-key`` ;
    2. ``ANTHROPIC_AUTH_TOKEN`` / ``CLAUDE_CODE_OAUTH_TOKEN`` — bearer statique ;
    3. **compte Claude connecté en OAuth** (``app/providers/claude_oauth.py``)
       — ``Authorization: Bearer`` + ``anthropic-beta: oauth-2025-04-20``, sans
       ``x-api-key``, avec refresh automatique du token ;
    4. ``LLM_API_KEY`` — clé de secours (compat).

    Le SDK est optionnel — sans lui, les appels passent en HTTP direct (httpx).
    """

    name = "claude"
    provider_key = "anthropic"

    def __init__(self):
        from .providers import claude_oauth
        self._oauth = claude_oauth
        self.api_key = (os.getenv("ANTHROPIC_API_KEY")
                        or os.getenv("LLM_API_KEY") or "")
        self._static_token = (os.getenv("ANTHROPIC_AUTH_TOKEN")
                              or os.getenv("CLAUDE_CODE_OAUTH_TOKEN") or "")
        if (not self.api_key and not self._static_token
                and not claude_oauth.connected()):
            raise RuntimeError("ANTHROPIC_API_KEY manquante et aucun compte "
                               "Claude OAuth connecté")
        # Une clé statique éclipse le bearer (comportement du SDK Anthropic).
        self._bearer = not self.api_key
        self.model_used = _model(heavy=True)
        self.client = None if self._bearer else self._sdk_client()

    @staticmethod
    def _base_url() -> str:
        return (os.getenv("ANTHROPIC_BASE_URL")
                or "https://api.anthropic.com").rstrip("/")

    def _token(self) -> str:
        """Jeton Bearer courant (statique ou OAuth rafraîchi si expiré)."""
        return self._static_token or self._oauth.get_access_token()

    def _sdk_client(self):
        try:
            from anthropic import Anthropic
        except ImportError:
            print("[LLM] SDK anthropic absent, appels en HTTP direct")
            return None
        if self._bearer:
            # Reconstruit à chaque appel pour suivre la rotation du token OAuth.
            return Anthropic(
                auth_token=self._token(), base_url=self._base_url() or None,
                default_headers={"anthropic-beta": self._oauth.beta_header()})
        return Anthropic(api_key=self.api_key, base_url=self._base_url() or None)

    def _headers(self) -> dict:
        headers = {"anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        if self._bearer:
            headers["Authorization"] = f"Bearer {self._token()}"
            headers["anthropic-beta"] = self._oauth.beta_header()
        else:
            headers["x-api-key"] = self.api_key
        return headers

    def _complete(self, system: str, user: str, model: str, temperature: float) -> dict:
        max_tokens = int(os.getenv("LLM_MAX_TOKENS") or 4096)
        try:
            return self._complete_once(system, user, model, temperature, max_tokens)
        except Exception as exc:
            auth_error = any(s in str(exc).lower()
                             for s in ("401", "403", "unauthorized",
                                       "authentication", "expired",
                                       "invalid token"))
            if not (self._bearer and not self._static_token and auth_error):
                raise
            # Token OAuth expiré/révoqué côté serveur : refresh forcé puis retry.
            try:
                self._oauth.refresh_tokens()
            except Exception:
                raise exc
            return self._complete_once(system, user, model, temperature, max_tokens)

    def _complete_once(self, system: str, user: str, model: str,
                       temperature: float, max_tokens: int) -> dict:
        # 1) Voie principale : SDK anthropic (Messages API).
        client = self._sdk_client() if self._bearer else self.client
        if client is not None:
            try:
                response = client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    temperature=temperature,
                    messages=[{"role": "user", "content": user}],
                )
                content = "".join(
                    getattr(block, "text", "")
                    for block in response.content
                    if getattr(block, "type", "") == "text"
                )
                if not content:
                    raise RuntimeError("Réponse LLM vide")
                return _parse_json(content)
            except Exception as exc:
                print(f"[LLM] SDK anthropic échoué, repli HTTP direct : {exc}")

        # 2) Repli sans dépendance : appel HTTP direct à la Messages API.
        try:
            with httpx.Client(timeout=60) as http:
                r = http.post(
                    f"{self._base_url()}/v1/messages",
                    headers=self._headers(),
                    json={
                        "model": model,
                        "max_tokens": max_tokens,
                        "system": system,
                        "temperature": temperature,
                        "messages": [{"role": "user", "content": user}],
                    },
                )
                r.raise_for_status()
                blocks = r.json().get("content") or []
            content = "".join(
                str(b.get("text", "")) for b in blocks if b.get("type") == "text")
            if not content:
                raise RuntimeError("Réponse LLM vide")
            return _parse_json(content)
        except Exception as exc:
            raise RuntimeError(f"Échec LLM Anthropic (SDK puis HTTP) : {exc}")


class FallbackProvider(LLMProvider):
    name = "fallback"

    def __init__(self, primary: LLMProvider, fallback: LLMProvider):
        self.primary = primary
        self.fallback = fallback

    @property
    def model_used(self) -> str:
        return getattr(self.primary, "model_used", "") or getattr(self.fallback, "model_used", "")

    def generate(self, prompt: str, facts: dict) -> dict:
        try:
            return self.primary.generate(prompt, facts)
        except Exception as exc:
            print(f"[LLM] Échec du fournisseur distant : {exc}")
            print("[LLM] Utilisation du mode déterministe")
            return self.fallback.generate(prompt, facts)

    def analyze_profile(self, cv_text: str) -> dict:
        try:
            return self.primary.analyze_profile(cv_text)
        except Exception as exc:
            print(f"[LLM] Profile analysis fallback: {exc}")
            return self.fallback.analyze_profile(cv_text)

    def complete_profile(self, cv_text: str) -> dict:
        try:
            return self.primary.complete_profile(cv_text)
        except Exception as exc:
            print(f"[LLM] Profile completion fallback: {exc}")
            return self.fallback.complete_profile(cv_text)

    def refine_profile(self, instruction: str, profile: dict,
                       cv_text: str = "") -> dict:
        try:
            return self.primary.refine_profile(instruction, profile, cv_text)
        except Exception as exc:
            print(f"[LLM] Profile refinement fallback: {exc}")
            return self.fallback.refine_profile(instruction, profile, cv_text)

    def enrich_offer(self, offer: dict) -> dict:
        try:
            return self.primary.enrich_offer(offer)
        except Exception as exc:
            print(f"[LLM] Offer enrichment fallback: {exc}")
            return self.fallback.enrich_offer(offer)

    def generate_cv(self, facts: dict) -> dict:
        try:
            return self.primary.generate_cv(facts)
        except Exception as exc:
            print(f"[LLM] CV generation fallback: {exc}")
            return self.fallback.generate_cv(facts)

    def summarize_market(self, market: dict) -> dict:
        try:
            return self.primary.summarize_market(market)
        except Exception as exc:
            print(f"[LLM] Market summary fallback: {exc}")
            return self.fallback.summarize_market(market)

    def analyze_market(self, profil: dict, market: dict) -> dict:
        try:
            return self.primary.analyze_market(profil, market)
        except Exception as exc:
            print(f"[LLM] Market analysis fallback: {exc}")
            return self.fallback.analyze_market(profil, market)

    def complete_task(self, task: str, **kwargs) -> dict:
        try:
            return self.primary.complete_task(task, **kwargs)
        except Exception as exc:
            print(f"[LLM] Task '{task}' fallback: {exc}")
            return self.fallback.complete_task(task, **kwargs)


def get_provider() -> LLMProvider:
    provider = effective_provider()

    if provider in {"anthropic", "claude"}:
        return FallbackProvider(
            primary=AnthropicProvider(),
            fallback=DeterministicProvider(),
        )

    if provider in {"openrouter", "openai", "compatible"}:
        return FallbackProvider(
            primary=OpenAICompatibleProvider(),
            fallback=DeterministicProvider(),
        )

    return DeterministicProvider()