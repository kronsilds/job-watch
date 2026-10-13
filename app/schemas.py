from pydantic import BaseModel,Field
class ProfileData(BaseModel):
 model_config={'json_schema_extra':{'example':{'name':'','main_job':'Data Analyst','skills':['SQL'],'jobs':['Data Analyst'],'locations':['Paris'],'contracts':['CDI']}}}
 name:str=Field(default='',description="Nom affiché du candidat"); main_job:str=Field(default='',description='Métier principal visé'); secondary_job:str=Field(default='',description='Métier secondaire visé'); jobs:list[str]=Field(default_factory=list,description='Intitulés visés'); adjacent_jobs:list[str]=Field(default_factory=list,description='Métiers adjacents acceptés'); metiers_vises:list[str]=Field(default_factory=list,description='Alias historique de jobs'); sources:list[str]=Field(default_factory=list,description='Connecteurs sources à interroger'); seniorite:list[str]=Field(default_factory=list,description="Niveaux de séniorité visés"); locations:list[str]=Field(default_factory=list,description='Zones géographiques visées'); mobility:str=Field(default='',description='Mobilité (ex: remote, Paris + remote)'); remote:list[str]=Field(default_factory=list,description='Préférences remote'); contracts:list[str]=Field(default_factory=list,description='Contrats visés (CDI, CDD, freelance…)'); salary_min:int|None=Field(default=None,description='Salaire annuel brut min (€)'); salary_max:int|None=Field(default=None,description='Salaire annuel brut max (€)'); sectors:list[str]=Field(default_factory=list,description='Secteurs visés'); exclusions:list[str]=Field(default_factory=list,description='Mots-clés exclus'); skills:list[str]=Field(default_factory=list,description='Compétences du candidat'); years_experience:float|None=Field(default=None,description="Années d'expérience"); skill_matrix:list[dict]=Field(default_factory=list,description='Matrice compétence → niveau'); profile_summary:str=Field(default='',description='Résumé libre du profil'); ponderation:dict=Field(default_factory=dict,description='Pondération du scoring (0-1 par dimension)')
class ProfileUpdate(BaseModel):
 model_config={'json_schema_extra':{'example':{'name':'Candidat','validate_profile':True,'source_text':'','data':{'main_job':'Data Analyst','skills':['SQL'],'jobs':['Data Analyst'],'locations':['Paris']}}}}
 name:str=Field(default='Candidat',description='Nom affiché'); data:ProfileData=Field(description='Contenu de la fiche profil'); validate_profile:bool=Field(default=False,description='True = valide le profil et rescore les offres'); source_text:str=Field(default='',description='Texte source (CV) conservé tel quel')
class ProfileRefinePrompt(BaseModel):
 model_config={'json_schema_extra':{'example':{'instruction':'Ajoute le télétravail 3j/semaine','name':'Candidat','source_text':'','data':{'main_job':'Data Analyst','skills':['SQL']}}}}
 instruction:str=Field(description='Consigne libre ex: « ajoute le remote »',examples=['Ajoute le télétravail 3 jours/semaine']); name:str=Field(default='Candidat',description='Nom affiché'); data:ProfileData=Field(description='Fiche profil actuelle'); source_text:str=Field(default='',description='Texte source (CV)')
class StateUpdate(BaseModel):
 model_config={'json_schema_extra':{'example':{'state':'validated'}}}
 state:str=Field(description="Nouvel état du dossier (ex: draft, to_validate, validated, sent…)",examples=['validated'])
class AuthCredentials(BaseModel):
 model_config={'json_schema_extra':{'example':{'username':'alice','password':'secret123'}}}
 username:str=Field(default='',description="Nom de compte (2-64 car., sans espaces, insensible à la casse)",examples=['alice']); password:str=Field(default='',description='Mot de passe (4 caractères minimum)',examples=['secret123'])
class RefineFeedback(BaseModel):
 model_config={'json_schema_extra':{'example':{'comments':'Ton plus direct, 120 mots max','document_type':'pitch'}}}
 comments:str=Field(description='Commentaires libres pour réviser le document',examples=['Ton plus direct, 120 mots max']); document_type:str|None=Field(default=None,description='pitch | cv | email (défaut: pitch)',examples=['pitch'])
class ApplicationContentUpdate(BaseModel):
 model_config={'json_schema_extra':{'example':{'pitch':'Pitch retouché…','email_subject':'Candidature — Data Analyst','email_body':'Bonjour…','cv_after':'Expérience…','skills_to_highlight':['SQL']}}}
 pitch:str|None=Field(default=None,description='Pitch retouché'); email_subject:str|None=Field(default=None,description="Objet de l'e-mail"); email_body:str|None=Field(default=None,description="Corps de l'e-mail"); cv_after:str|None=Field(default=None,description='CV optimisé ATS retouché'); skills_to_highlight:list[str]|None=Field(default=None,description='Compétences à mettre en avant')
class OfferUrlImport(BaseModel):
 """Injection manuelle d'une annonce repérée par l'utilisateur (lien collé).

 ``title``/``company``/``description`` sont des replis facultatifs, utilisés
 uniquement si la page n'est pas lisible (connexion requise, anti-bot) : ils ne
 remplacent jamais une donnée réellement extraite de l'annonce.
 """
 model_config={'json_schema_extra':{'example':{'url':'https://www.example.com/offre/123','title':'','company':'','description':''}}}
 url:str=Field(description="URL publique de l'annonce",examples=['https://www.example.com/offre/123'])
 title:str=Field(default='',description="Repli si la page est illisible : intitulé saisi à la main"); company:str=Field(default='',description='Repli si la page est illisible : entreprise'); description:str=Field(default='',description='Repli si la page est illisible : description')
class CacheReplayRequest(BaseModel):
 model_config={'json_schema_extra':{'example':{'limit':200,'source':'','import_missing':False}}}
 limit:int=Field(default=200,description="Nombre max d'entrées rejouées"); source:str=Field(default='',description='Filtre connecteur (vide = tous)'); import_missing:bool=Field(default=False,description='True = réimporte en base les offres manquantes rejouées')
