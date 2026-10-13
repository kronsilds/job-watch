"""CV-to-profile analysis with an explicit, reviewable skills matrix.

The analysis may come from the LLM (rich, heavy model) or from the local
heuristic extractor (generalist, deterministic). Results are always passed
through :func:`normalize` and flagged ``review_required`` so that nothing is
validated automatically.

``skill_matrix`` est la **source unique** des compétences : elle porte le
libellé, la catégorie, le niveau et la preuve ; ``skills`` n'en est que la
projection plate (utilisée par le scoring, la veille et les faits de
candidature). :func:`normalize` garantit que les deux listes ne divergent
jamais, ce qui supprime le doublon historique « sql » / « SQL Server ».
"""

from __future__ import annotations

import json
import os
import re

from .extract import extract_structured
from .llm import get_provider, DeterministicProvider, effective_provider

FIELDS = ('jobs', 'adjacent_jobs', 'metiers_vises', 'locations', 'remote',
          'contracts', 'sectors', 'exclusions', 'skills', 'sources', 'seniorite')

# Clés qui prouvent qu'un modèle a renvoyé la fiche complète (et non un patch).
CORE_KEYS = ('jobs', 'main_job', 'skills', 'skill_matrix', 'profile_summary')

_SKILL_WORD_SPLIT = re.compile(r"[^0-9a-zà-ÿ+#.]+")


def _llm_configured() -> bool:
    """Un fournisseur LLM réel est-il configuré ? (heavy et/ou light)."""
    return effective_provider() in {'openrouter', 'openai', 'compatible',
                                    'anthropic', 'claude'}


def _real_llm(provider) -> bool:
    """Vrai dès qu'un modèle distante plutôt qu'en mode purement déterministe."""
    return not isinstance(provider, DeterministicProvider)


def _sparse(data: dict) -> bool:
    """L'extraction heuristique a-t-elle donné peu de résultats ?"""
    facts = (len(data.get('skills') or []) + len(data.get('jobs') or [])
             + len(data.get('locations') or []) + len(data.get('contracts') or []))
    return facts < 4



def _list(value):
    return [str(x).strip() for x in (value or []) if str(x).strip()]


def _skill_words(value: str) -> set[str]:
    """Mots significatifs d'un libellé (« git / git flow » → {git, flow})."""
    return {w for w in _SKILL_WORD_SPLIT.split(str(value).lower()) if w}


def _covered_by(labels, skill: str) -> bool:
    """Le libellé est-il déjà porté par une compétence plus complète ?

    ``sql`` face à ``SQL Server``, ``git`` face à ``Git / Git Flow`` : la forme
    courte est un doublon du même mot-clé, pas une compétence distincte. Les
    libellés courts (``R``, ``Go``) sont protégés et ``Java`` n'est jamais
    absorbé par ``JavaScript`` (comparaison mot à mot, pas caractère à caractère).
    """
    skill = str(skill).strip()
    words = _skill_words(skill)
    if len(skill) < 3 or not words:
        return False
    return any(words <= _skill_words(label) for label in labels if str(label).strip())


def merge_skill_list(explicit, matrix_labels) -> list[str]:
    """Liste canonique des compétences, sans doublon, à partir des deux sources.

    Le libellé de la matrice gagne (casse lisible : ``SQL Server`` plutôt que
    ``sql server``), l'ordre de la liste explicite (LLM ou saisie) est conservé,
    et toute compétence détaillée dans la matrice est garantie présente.
    """
    display = {str(label).strip().lower(): str(label).strip() for label in matrix_labels}
    out, seen = [], set()
    for skill in explicit:
        item = str(skill).strip()
        key = item.lower()
        if not item or key in seen:
            continue
        if key in display:
            seen.add(key)
            out.append(display[key])
            continue
        if _covered_by([*matrix_labels, *out], item):
            continue
        seen.add(key)
        out.append(item)
    for label in matrix_labels:
        item = str(label).strip()
        key = item.lower()
        if item and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _salary(value):
    """Montant annuel entier ou ``None`` (les valeurs non numériques sont ignorées)."""
    try:
        amount = int(float(value))
    except (TypeError, ValueError):
        return None
    return amount if 0 < amount < 1_000_000 else None


def build_profile_summary(data: dict) -> str:
    """Résumé de profil factuel et déterministe (aucune information inventée).

    Repli utilisé lorsque ni le modèle ni l'utilisateur n'ont fourni de résumé :
    « Architecte logiciel (Directeur informatique) — 25 ans d'expérience ;
    Compétences clés : … ; Paris · remote. »
    """
    jobs = _list(data.get('jobs'))
    title = str(data.get('main_job') or '').strip() or (jobs[0] if jobs else '')
    secondary = str(data.get('secondary_job') or '').strip()
    if not secondary and len(jobs) > 1:
        secondary = jobs[1]
    years = data.get('years_experience')
    skills = [s for s in _list(data.get('skills')) if len(s) > 2][:6]
    place = ' · '.join([*_list(data.get('locations'))[:3], *_list(data.get('remote'))])

    parts: list[str] = []
    if title and secondary and secondary.lower() != title.lower():
        parts.append(f'{title} ({secondary})')
    elif title:
        parts.append(title)
    elif secondary:
        parts.append(secondary)
    if isinstance(years, (int, float)) and years > 0:
        parts.append(f"{years:g} ans d'expérience")
    if skills:
        parts.append('Compétences clés : ' + ', '.join(skills))
    if place:
        parts.append(place)
    if not parts:
        return ''
    head, rest = parts[0], ' ; '.join(parts[1:])
    return f'{head} — {rest}.' if rest else f'{head}.'


def normalize(data: dict) -> dict:
    """Keep only displayable, safe values from either LLM or heuristic output."""
    base = extract_structured('')
    for key in FIELDS:
        base[key] = _list(data.get(key))
    base['name'] = str(data.get('name') or '').strip()
    base['main_job'] = str(data.get('main_job') or '').strip()
    base['secondary_job'] = str(data.get('secondary_job') or '').strip()
    base['mobility'] = str(data.get('mobility') or '').strip()
    base['profile_summary'] = str(data.get('profile_summary') or '').strip()
    years = data.get('years_experience')
    base['years_experience'] = years if isinstance(years, (int, float)) and 0 <= years <= 60 else None
    base['salary_min'] = _salary(data.get('salary_min'))
    base['salary_max'] = _salary(data.get('salary_max'))
    base['ponderation'] = dict(data.get('ponderation') or {})
    matrix = []
    seen_rows = set()
    for row in data.get('skill_matrix') or []:
        if not isinstance(row, dict) or not str(row.get('skill') or '').strip():
            continue
        label = str(row['skill']).strip()
        if label.lower() in seen_rows:
            continue
        seen_rows.add(label.lower())
        matrix.append({
            'skill': label,
            'category': str(row.get('category') or 'Autre').strip(),
            'level': str(row.get('level') or 'à confirmer').strip(),
            'evidence': str(row.get('evidence') or 'À confirmer dans le CV').strip(),
        })
    base['skill_matrix'] = matrix[:80]
    # La matrice est la source unique du détail : `skills` en est la projection
    # plate (libellé identique, sans doublon de mot-clé).
    base['skills'] = merge_skill_list(
        base['skills'], [row['skill'] for row in base['skill_matrix']])
    if not base['main_job'] and base['jobs']:
        base['main_job'] = base['jobs'][0]
    if base['main_job'] and not base['secondary_job'] and len(base['jobs']) > 1:
        base['secondary_job'] = base['jobs'][1]
    if not base['profile_summary']:
        # Toujours un résumé : celui du modèle s'il existe, sinon celui déduit
        # des faits extraits (jamais de texte inventé).
        base['profile_summary'] = build_profile_summary(base)
    return base


def heuristic_analysis(text: str) -> dict:
    data = extract_structured(text)
    data['skill_matrix'] = [
        {'skill': row['skill'], 'category': row['category'], 'level': 'à confirmer',
         'evidence': row['evidence']}
        for row in data['skill_matrix']
    ]
    return normalize(data)


def merge_with_heuristics(llm_data: dict, text: str) -> dict:
    """Guarantee the generalist heuristic baseline is never lost.

    Skills/locations/contracts detected locally enrich (without overriding)
    what the LLM returned, making the result robust even if the model lags.
    """
    heur = extract_structured(text)
    return _fill_gaps(llm_data, heur)


def complete_with_light(llm_data: dict, heur: dict, text: str) -> dict:
    """Fusion pour compléter une extraction heuristique pauvre via une passe light.

    Le résultat du modèle léger sert de base ; la baseline heuristique remplit
    les champs que le modèle aurait laissés vides (n'invente jamais).
    """
    baseline = extract_structured(text) if not heur else heur
    return _fill_gaps(llm_data, baseline)


def _fill_gaps(llm_data: dict, baseline: dict) -> dict:
    merged = dict(llm_data)
    seen = {str(x).strip().lower() for x in merged.get('skills') or []}
    for skill, display, _category in [(s, s, '') for s in (baseline.get('skills') or [])]:
        if str(skill).strip().lower() not in seen:
            merged.setdefault('skills', []).append(skill)
            seen.add(str(skill).strip().lower())
    for key in FIELDS:
        if not merged.get(key) and baseline.get(key):
            merged[key] = baseline.get(key) or []
    for key in ('name', 'main_job', 'secondary_job', 'mobility', 'profile_summary'):
        if not merged.get(key):
            merged[key] = baseline.get(key) or ''
    if merged.get('years_experience') is None:
        merged['years_experience'] = baseline.get('years_experience')
    if not merged.get('skill_matrix'):
        merged['skill_matrix'] = baseline.get('skill_matrix') or []
    return merged



def analyze_cv(text: str) -> tuple[dict, dict]:
    if not text.strip():
        return heuristic_analysis(text), {'provider': 'heuristic', 'review_required': True}

    heur = heuristic_analysis(text)
    provider = get_provider()

    # Palier 1 : modèle lourd (heavy) quand il est configuré, fusionné avec la
    # baseline heuristique — comportement historique conservé.
    if _llm_configured():
        try:
            rich = merge_with_heuristics(provider.analyze_profile(text), text)
            # Palier 2 : si la sortie lourde reste pauvre, compléter via une
            # passe légère (request : heuristique pauvre -> LLM light).
            if _sparse(rich):
                rich = complete_with_light(provider.complete_profile(text), heur, text)
            return normalize(rich), {
                'provider': 'llm',
                'model': getattr(provider, 'model_used', ''),
                'review_required': True,
            }
        except Exception as exc:
            # Repli : compléter l'heuristique par une passe légère si possible.
            try:
                if _sparse(heur) and _real_llm(provider):
                    rich = complete_with_light(
                        provider.complete_profile(text), heur, text)
                    return normalize(rich), {
                        'provider': 'heuristic+light',
                        'model': getattr(provider, 'model_used', ''),
                        'review_required': True,
                    }
            except Exception:
                pass
            return heur, {
                'provider': 'heuristic', 'review_required': True, 'warning': str(exc),
            }

    # Pas de modèle lourd : si l'heuristique est pauvre, compléter par une
    # analyse LLM light (le fournisseur retombe en mode déterministe sinon).
    if _sparse(heur) and _real_llm(provider):
        try:
            rich = complete_with_light(provider.complete_profile(text), heur, text)
            return normalize(rich), {
                'provider': 'heuristic+light',
                'model': getattr(provider, 'model_used', ''),
                'review_required': True,
            }
        except Exception as exc:
            return heur, {
                'provider': 'heuristic', 'review_required': True, 'warning': str(exc),
            }
    return heur, {'provider': 'heuristic', 'review_required': True}


def refine_with_prompt(data: dict, instruction: str, cv_text: str = '',
                       provider=None) -> tuple[dict, dict]:
    """Affine la fiche de profil selon une consigne utilisateur, sans rien inventer.

    Le modèle reçoit la fiche courante et, si elle existe, la seule base
    factuelle autorisée : le texte du CV déjà importé. La fiche renvoyée est
    normalisée puis **affichée** à l'utilisateur — rien n'est enregistré ici,
    la validation du profil reste un acte humain. Sans modèle configuré, la
    fiche est renvoyée inchangée avec un avertissement explicite.
    """
    instruction = (instruction or '').strip()
    if not instruction:
        raise ValueError('Consigne vide : décrivez ce que vous voulez ajuster.')
    current = normalize(data or {})
    provider = provider or get_provider()
    result = provider.refine_profile(
        instruction=instruction, profile=current, cv_text=cv_text or '')
    result = result if isinstance(result, dict) else {}
    meta = {
        'provider': getattr(provider, 'name', ''),
        'model': getattr(provider, 'model_used', ''),
        'changes_summary': str(result.get('changes_summary') or ''),
        'warning': str(result.get('warning') or ''),
        'applied': False,
    }
    refined = result.get('profile')
    if not isinstance(refined, dict) or not refined:
        # Aucun modèle (ou réponse vide) : la fiche reste telle quelle.
        meta['warning'] = meta['warning'] or (
            'Aucune modification appliquée : la fiche est inchangée.')
        return current, meta
    if any(key in refined for key in CORE_KEYS):
        # Fiche complète renvoyée par le modèle (permet aussi de retirer un champ).
        refined_data = refined
    else:
        # Réponse partielle : seules les valeurs renseignées écrasent la fiche.
        refined_data = {**current,
                        **{k: v for k, v in refined.items()
                           if v not in (None, '', [], {})}}
    meta['applied'] = True
    return normalize(refined_data), meta
