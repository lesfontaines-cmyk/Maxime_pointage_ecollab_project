#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Serveur de clôture automatique — Charles Murgat
API directe eCollaboratrice (sans Selenium).
Lance: python server.py
"""

import json
import math
import time
import datetime
import threading
import os
import re as _re
import base64
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests as _requests

try:
    from pywebpush import webpush, WebPushException
    PUSH_AVAILABLE = True
except ImportError:
    PUSH_AVAILABLE = False
    print("[PUSH] pywebpush non installé — notifications push désactivées")

app = Flask(__name__)
CORS(app)

# ─── WEB PUSH (VAPID) ───────────────────────────────────────────────────────
_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
VAPID_KEYS_FILE = os.path.join(_SERVER_DIR, 'vapid_keys.json')
PUSH_SUBS_FILE  = os.path.join(_SERVER_DIR, 'push_subscriptions.json')

def _ensure_vapid_keys():
    if os.path.exists(VAPID_KEYS_FILE):
        with open(VAPID_KEYS_FILE, 'r') as f:
            return json.load(f)
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    private_key = ec.generate_private_key(ec.SECP256R1())
    priv_bytes = private_key.private_numbers().private_value.to_bytes(32, 'big')
    pub_bytes  = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    keys = {
        'privateKey': base64.urlsafe_b64encode(priv_bytes).rstrip(b'=').decode(),
        'publicKey':  base64.urlsafe_b64encode(pub_bytes).rstrip(b'=').decode()
    }
    with open(VAPID_KEYS_FILE, 'w') as f:
        json.dump(keys, f)
    print(f"[PUSH] Clés VAPID générées → {VAPID_KEYS_FILE}")
    return keys

if PUSH_AVAILABLE:
    try:
        VAPID_KEYS = _ensure_vapid_keys()
        print(f"[PUSH] Clés VAPID chargées (publicKey: {VAPID_KEYS['publicKey'][:20]}...)")
    except Exception as e:
        print(f"[PUSH] Erreur chargement clés VAPID: {e}")
        VAPID_KEYS = {'publicKey': '', 'privateKey': ''}
        PUSH_AVAILABLE = False
else:
    VAPID_KEYS = {'publicKey': '', 'privateKey': ''}

def _load_push_subs():
    try:
        with open(PUSH_SUBS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {}

def _save_push_subs(subs):
    with open(PUSH_SUBS_FILE, 'w') as f:
        json.dump(subs, f)

def _send_push(email, title, body, is_error=False):
    if not PUSH_AVAILABLE:
        return
    subs = _load_push_subs()
    sub_info = subs.get(email)
    if not sub_info:
        print(f"  [push] Pas d'abonnement push pour {email}")
        return
    try:
        webpush(
            subscription_info=sub_info,
            data=json.dumps({"title": title, "body": body, "isError": is_error}),
            vapid_private_key=VAPID_KEYS['privateKey'],
            vapid_claims={"sub": "mailto:" + email}
        )
        print(f"  [push] Notification envoyée à {email}")
    except WebPushException as e:
        print(f"  [push] Erreur WebPush: {e}")
        if hasattr(e, 'response') and e.response and e.response.status_code in (404, 410):
            subs.pop(email, None)
            _save_push_subs(subs)
            print(f"  [push] Abonnement expiré, supprimé pour {email}")
    except Exception as e:
        print(f"  [push] Erreur: {e}")

# ─── UTILS ───────────────────────────────────────────────────────────────────
def to_minutes(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m

def min_to_hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"

def _base_url_of(url):
    return '/'.join(url.split('/')[:3])

def _extract_id_contrat(url):
    m = _re.search(r'idContrat=(\d+)', url)
    return int(m.group(1)) if m else None

# ─── SESSION HTTP DIRECTE ECOLLABORATRICE ────────────────────────────────────
_http_session = None
_http_session_expiry = 0
_http_session_key = None
_login_lock = threading.Lock()

def _http_login(session, email, password, base_url):
    headers = {
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json, text/javascript, */*; q=0.01',
    }
    data = {'mail': email, 'motdepasse': password, 'rememberMe': 'true'}
    r = session.post(f"{base_url}/Auth/Login", data=data, headers=headers, timeout=30)
    try:
        j = r.json()
    except Exception:
        j = None

    if isinstance(j, dict) and j.get('utilisateurs'):
        users = j.get('utilisateurs') or []
        uid = None
        for u in users:
            if isinstance(u, dict) and not u.get('Desactive'):
                uid = u.get('Id'); break
        if uid is None and users and isinstance(users[0], dict):
            uid = users[0].get('Id')
        if uid is not None:
            d2 = dict(data); d2['idUtilisateur'] = uid
            r = session.post(f"{base_url}/Auth/Login", data=d2, headers=headers, timeout=30)
            try: j = r.json()
            except Exception: j = None

    if any(c.name == '.ASPXAUTH' for c in session.cookies):
        return True, None
    msg = j.get('message') if isinstance(j, dict) else None
    return False, msg or f"Echec de connexion (HTTP {r.status_code})"


def _ensure_http_session(email, password, url):
    global _http_session, _http_session_expiry, _http_session_key
    base = _base_url_of(url)
    key = (email, base)
    now = time.time()
    if _http_session is not None and _http_session_key == key and now < _http_session_expiry:
        return _http_session, base

    with _login_lock:
        now = time.time()
        if _http_session is not None and _http_session_key == key and now < _http_session_expiry:
            return _http_session, base

        s = _requests.Session()
        s.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'X-Requested-With': 'XMLHttpRequest',
            'Accept': 'application/json, text/javascript, */*; q=0.01',
        })
        host = base.replace('https://', '').replace('http://', '')
        try: s.cookies.set('alert-rgpd', 'true', domain=host, path='/')
        except Exception: pass

        ok, msg = False, None
        try:
            ok, msg = _http_login(s, email, password, base)
        except Exception as e:
            ok, msg = False, str(e)

        if not ok:
            raise RuntimeError(msg or "Echec de connexion")

        # Visiter la page SaisieRapide pour établir le contexte de session complet
        id_contrat = _extract_id_contrat(url)
        today = datetime.date.today()
        page_url = f"{base}/Paie/VariablePaie/SaisieRapide?"
        if id_contrat:
            page_url += f"idContrat={id_contrat}&"
        page_url += f"mois={today.month:02d}&annee={today.year}"
        try:
            pr = s.get(page_url, timeout=15, headers={'Accept': 'text/html,application/xhtml+xml'})
            print(f"  [session] Page SaisieRapide: {pr.status_code} ({len(pr.text)} octets)")
            # Extraire le token anti-CSRF si présent
            token_match = _re.search(r'__RequestVerificationToken["\s]*value="([^"]+)"', pr.text)
            if token_match:
                s.headers['__RequestVerificationToken'] = token_match.group(1)
                print(f"  [session] Token CSRF trouvé")
            # Garder le Referer pour les appels API suivants
            s.headers['Referer'] = page_url
        except Exception as e:
            print(f"  [session] Visite page échouée (non bloquant): {e}")

        now2 = time.time()
        ttl = 25 * 60
        for c in s.cookies:
            if c.name == '.ASPXAUTH' and c.expires:
                ttl = max(60, min(c.expires - now2, 6 * 3600))
                break
        _http_session = s
        _http_session_expiry = now2 + ttl
        _http_session_key = key
        print(f"  [session] Login HTTP OK, TTL={int(ttl)}s")
        return s, base


def _reset_http_session():
    global _http_session, _http_session_expiry, _http_session_key
    _http_session = None
    _http_session_expiry = 0
    _http_session_key = None


def _get_vdp(session, base, id_contrat, mois, annee):
    # Essayer d'abord l'endpoint salarié (compte manager)
    r = session.get(f"{base}/Paie/VariablePaieAPI/GetVDPSalarie",
                    params={'idContrat': id_contrat, 'mois': f'{int(mois):02d}', 'annee': int(annee)},
                    timeout=30)
    if r.ok:
        data = r.json()
        if isinstance(data, dict) and 'Jours' in data:
            return data

    # Fallback : endpoint groupé entreprise (compte salarié)
    r2 = session.get(f"{base}/Paie/VariablePaieAPI/GetVDPGroupeeEntreprise",
                     params={'idContrat': id_contrat, 'mois': f'{int(mois):02d}', 'annee': int(annee)},
                     timeout=30)
    r2.raise_for_status()
    data2 = r2.json()
    # Réponse = liste de mois, chacun contenant Salaries[].Jours
    if isinstance(data2, list) and len(data2) > 0:
        item = data2[0]
        salaries = item.get('Salaries') or []
        for sal in salaries:
            if sal.get('IdContrat') == id_contrat or len(salaries) == 1:
                model = dict(sal)
                model['Jours'] = sal.get('Jours', [])
                model['NomSalarie'] = sal.get('NomPrenom', '')
                model['_groupee'] = True
                model['_annee'] = item.get('Annee')
                model['_mois'] = item.get('Mois')
                model['_idEntreprise'] = item.get('IdEntreprise')
                model['_full_response'] = data2
                return model
    raise RuntimeError(f"Aucune donnée trouvée pour idContrat={id_contrat}")


# ─── LECTURE ECOLLAB (API directe) ──────────────────────────────────────────
def _model_to_days(model, mois, annee):
    days = {}
    mois = int(mois); annee = int(annee)
    for j in (model.get('Jours') or []):
        try:
            if int(j.get('Mois', 0)) != mois or int(j.get('Annee', 0)) != annee:
                continue
            jour = int(j.get('Jour'))
        except Exception:
            continue
        date_key = f"{annee}-{mois:02d}-{jour:02d}"
        plages = []
        total_min = 0
        variables = None
        for h in (j.get('Horaires') or []):
            hd = h.get('HeureDebut'); hf = h.get('HeureFin')
            if hd is None or hf is None:
                continue
            try:
                hd = int(hd); hf = int(hf)
            except Exception:
                continue
            if hf > hd:
                total_min += (hf - hd)
            p = {'debut': min_to_hhmm(hd), 'fin': min_to_hhmm(hf)}
            if h.get('IdTache'):
                p['tache'] = h['IdTache']
            obs = h.get('ObservationCustom') or {}
            if obs.get('Value'):
                p['absence'] = True
                p['tache'] = obs.get('Text')
            plages.append(p)

        var_sources = [j.get('VariablesJour'), j.get('Variables'),
                       j.get('ValeursVariables'), j.get('ListeVariables')]
        for src in var_sources:
            if src and isinstance(src, list) and len(src) > 0:
                variables = {}
                for v in src:
                    lib = (v.get('Libelle') or v.get('libelle') or v.get('Label') or v.get('Nom') or '').upper()
                    val = v.get('Valeur') or v.get('valeur') or v.get('Value') or v.get('Quantite') or 0
                    if 'ASTREINTE' in lib:
                        variables['astreinte'] = val
                    if 'ELOIGNEMENT' in lib:
                        variables['indemniteEloignement'] = val
                break

        days[date_key] = {
            'plages': plages,
            'travaille': bool(j.get('EstTravaille')),
            'valideSalarie': bool(j.get('ValideeParSalarie')),
            'valideEntreprise': bool(j.get('ValideeParEntreprise')),
        }
        if variables:
            days[date_key]['variables'] = variables
    return days


def fetch_ecollab_days(email, password, url, date_str="", _retry=True):
    if date_str:
        try:
            dt = datetime.date.fromisoformat(date_str)
        except Exception:
            dt = datetime.date.today()
    else:
        dt = datetime.date.today()
    mois, annee = dt.month, dt.year
    id_contrat = _extract_id_contrat(url)
    if not id_contrat:
        return False, "idContrat introuvable dans l'URL", [], None, []

    try:
        session, base = _ensure_http_session(email, password, url)
        try:
            model = _get_vdp(session, base, id_contrat, mois, annee)
        except Exception as e:
            if _retry:
                _reset_http_session()
                return fetch_ecollab_days(email, password, url, date_str, _retry=False)
            raise

        days = _model_to_days(model, mois, annee)
        recap = _extract_recap(model, mois, annee)
        taches = _extract_taches(model)

        return True, days, [], recap, taches

    except RuntimeError as e:
        return False, str(e), [], None, []
    except Exception as e:
        return False, f"Erreur lecture directe : {e}", [], None, []


def _extract_recap(model, mois, annee):
    mois = int(mois); annee = int(annee)
    recap = {}
    total_min = 0
    weeks = {}
    for j in (model.get('Jours') or []):
        try:
            if int(j.get('Mois', 0)) != mois or int(j.get('Annee', 0)) != annee:
                continue
            jour = int(j.get('Jour'))
        except Exception:
            continue
        day_min = 0
        for h in (j.get('Horaires') or []):
            hd = h.get('HeureDebut'); hf = h.get('HeureFin')
            if hd is not None and hf is not None:
                try:
                    hd = int(hd); hf = int(hf)
                    if hf > hd:
                        day_min += (hf - hd)
                except Exception:
                    pass
        total_min += day_min
        try:
            dt = datetime.date(annee, mois, jour)
            iso_week = dt.isocalendar()[1]
            weeks.setdefault(iso_week, 0)
            weeks[iso_week] += day_min
        except Exception:
            pass

    recap['totalHeures'] = min_to_hhmm(total_min) if total_min else '0'

    detail = []
    total_supp_min = 0
    for wk in sorted(weeks.keys()):
        wk_min = weeks[wk]
        supp = max(0, wk_min - 35 * 60)
        total_supp_min += supp
        detail.append({
            'plage': f'S{wk}',
            'heuresSupp': min_to_hhmm(supp) if supp else '0',
            'heuresSuppEquivalentes': min_to_hhmm(supp) if supp else '0',
        })
    recap['detailParSemaine'] = detail
    recap['totalHeuresSupp'] = round(total_supp_min / 60, 2)

    return recap


def _extract_taches(model):
    taches = []
    seen = set()
    for j in (model.get('Jours') or []):
        for h in (j.get('Horaires') or []):
            tid = h.get('IdTache')
            tname = h.get('LibelleTache') or h.get('Libelle') or ''
            if tid and tid not in seen:
                seen.add(tid)
                if tname:
                    taches.append({'id': tid, 'label': tname})
    return taches


# ─── CLÔTURE (API directe) ──────────────────────────────────────────────────
def cloture_direct(email, password, url, plages, date_str="", variables=None, _retry=True):
    if not date_str:
        return False, "Date requise pour la clôture"

    try:
        dt = datetime.date.fromisoformat(date_str)
    except Exception:
        return False, f"Date invalide : {date_str}"

    mois, annee = dt.month, dt.year
    target_jour = dt.day
    id_contrat = _extract_id_contrat(url)
    if not id_contrat:
        return False, "idContrat introuvable dans l'URL"

    try:
        session, base = _ensure_http_session(email, password, url)
        try:
            model = _get_vdp(session, base, id_contrat, mois, annee)
        except Exception:
            if _retry:
                _reset_http_session()
                return cloture_direct(email, password, url, plages, date_str, variables, _retry=False)
            raise

        if not isinstance(model, dict) or 'Jours' not in model:
            return False, "Réponse GetVDPSalarie inattendue (pas de 'Jours')"

        jour_found = None
        for j in (model.get('Jours') or []):
            try:
                if int(j.get('Jour', -1)) == target_jour and \
                   int(j.get('Mois', 0)) == mois and \
                   int(j.get('Annee', 0)) == annee:
                    jour_found = j
                    break
            except Exception:
                continue

        if not jour_found:
            return False, f"Jour {target_jour}/{mois:02d}/{annee} introuvable dans le modèle"

        if not plages:
            jour_found['EstTravaille'] = False
            if jour_found.get('Matin'):
                jour_found['Matin']['Travaille'] = False
            if jour_found.get('ApresMidi'):
                jour_found['ApresMidi']['Travaille'] = False
            horaires = jour_found.get('Horaires') or []
            while len(horaires) > 0:
                horaires.pop()
        else:
            jour_found['EstTravaille'] = True
            if jour_found.get('Matin'):
                jour_found['Matin']['Travaille'] = True
            if jour_found.get('ApresMidi'):
                jour_found['ApresMidi']['Travaille'] = True

            horaires = jour_found.get('Horaires') or []
            if not horaires:
                jour_found['Horaires'] = horaires

            ref_horaire = None
            for jj in (model.get('Jours') or []):
                for hh in (jj.get('Horaires') or []):
                    if hh.get('HeureDebut') is not None:
                        ref_horaire = hh
                        break
                if ref_horaire:
                    break

            while len(horaires) > len(plages):
                horaires.pop()
            while len(horaires) < len(plages):
                if ref_horaire:
                    clone = dict(ref_horaire)
                else:
                    clone = {}
                clone['HeureDebut'] = 0
                clone['HeureFin'] = 0
                clone['Id'] = 0
                horaires.append(clone)

            for i, p in enumerate(plages):
                horaires[i]['HeureDebut'] = to_minutes(p['debut'])
                horaires[i]['HeureFin'] = to_minutes(p['fin'])
                if p.get('tache'):
                    horaires[i]['IdTache'] = int(p['tache'])

        jour_found['ValideeParSalarie'] = True

        if variables:
            astreinte_val = int(variables.get('astreinte', 0))
            indemnite_val = int(variables.get('indemniteEloignement', 0))
            if astreinte_val > 0 or indemnite_val > 0:
                var_sources = [jour_found.get('VariablesJour'), jour_found.get('Variables'),
                               jour_found.get('ValeursVariables'), jour_found.get('ListeVariables')]
                for src in var_sources:
                    if src and isinstance(src, list):
                        for v in src:
                            lib = (v.get('Libelle') or v.get('libelle') or v.get('Label') or v.get('Nom') or '').upper()
                            if 'ASTREINTE' in lib and astreinte_val > 0:
                                v['Valeur'] = astreinte_val
                                if 'valeur' in v: v['valeur'] = astreinte_val
                                if 'Value' in v: v['Value'] = astreinte_val
                                if 'Quantite' in v: v['Quantite'] = astreinte_val
                            if 'ELOIGNEMENT' in lib and indemnite_val > 0:
                                v['Valeur'] = indemnite_val
                                if 'valeur' in v: v['valeur'] = indemnite_val
                                if 'Value' in v: v['Value'] = indemnite_val
                                if 'Quantite' in v: v['Quantite'] = indemnite_val
                        break

        if model.get('_groupee'):
            save_url = f"{base}/Paie/VariablePaieAPI/SaveVariableDePaieGroupee"
            salarie_clean = {k: v for k, v in model.items() if not k.startswith('_')}
            save_body = {
                'salaries': [salarie_clean],
                'mois': int(model.get('_mois', mois)),
                'annee': int(model.get('_annee', annee)),
                'terminer': False
            }
        else:
            save_url = f"{base}/Paie/VariablePaieAPI/SaveVariablePaie"
            save_body = {'model': model}

        r = session.post(save_url,
                         json=save_body,
                         headers={'Content-Type': 'application/json;charset=utf-8'},
                         timeout=60)

        if r.status_code in (401, 403) and _retry:
            _reset_http_session()
            return cloture_direct(email, password, url, plages, date_str, variables, _retry=False)

        if not r.ok:
            detail = ""
            try:
                detail = r.text[:200]
            except Exception:
                pass
            return False, f"Echec sauvegarde HTTP {r.status_code}: {detail}"

        resume = " | ".join(f"{p['debut']} → {p['fin']}" for p in plages) if plages else "Journée vide"
        date_label = f"{dt.day:02d}/{dt.month:02d}/{dt.year}"
        var_msg = ""
        if variables and (variables.get('astreinte', 0) > 0 or variables.get('indemniteEloignement', 0) > 0):
            parts = []
            if variables.get('astreinte', 0) > 0: parts.append(f"Astreinte={variables['astreinte']}")
            if variables.get('indemniteEloignement', 0) > 0: parts.append(f"Éloignement={variables['indemniteEloignement']}")
            var_msg = " | Variables: " + ", ".join(parts)

        return True, f"Clôture réussie ({date_label}) : {resume}{var_msg}"

    except RuntimeError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Erreur clôture directe : {e}"


# ─── ROUTES API ──────────────────────────────────────────────────────────────

@app.route("/ping", methods=["GET"])
def ping():
    """Test de connexion depuis la PWA."""
    return jsonify({"status": "ok", "message": "Serveur opérationnel (API directe)"})


@app.route("/vapid-public-key", methods=["GET"])
def vapid_public_key():
    return jsonify({"publicKey": VAPID_KEYS.get('publicKey', '')})


@app.route("/push-status", methods=["POST"])
def push_status():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    if not email:
        return jsonify({"hasSubscription": False})
    subs = _load_push_subs()
    return jsonify({"hasSubscription": email in subs})


@app.route("/subscribe", methods=["POST"])
def subscribe():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    sub = data.get("subscription")
    if not email or not sub:
        return jsonify({"success": False, "error": "Email et subscription requis"}), 400
    subs = _load_push_subs()
    subs[email] = sub
    _save_push_subs(subs)
    print(f"[PUSH] Subscription enregistrée pour {email}")
    return jsonify({"success": True})


@app.route("/test-push", methods=["POST"])
def test_push():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    delay = int(data.get("delay", 300))
    if not email:
        return jsonify({"success": False, "error": "Email requis"}), 400
    _send_push(email, "Test push", "Notification de test immédiate")
    def delayed():
        time.sleep(delay)
        _send_push(email, "Test push différé", f"Notification reçue après {delay}s")
    threading.Thread(target=delayed, daemon=True).start()
    return jsonify({"success": True, "message": f"Push immédiat envoyé + notification dans {delay}s"})


@app.route("/test-login", methods=["POST"])
def test_login():
    data = request.get_json(force=True)
    email    = (data.get("email")    or "").strip()
    password = (data.get("password") or "").strip()
    url      = (data.get("url")      or "").strip()

    if not email or not password:
        return jsonify({"success": False, "error": "Email et mot de passe requis"}), 400
    if not url:
        return jsonify({"success": False, "error": "URL Ecollaboratrice requise"}), 400

    try:
        _reset_http_session()
        _ensure_http_session(email, password, url)
        return jsonify({"success": True, "message": "Connexion reussie"})
    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e)})
    except Exception as e:
        msg = str(e).split('\n')[0]
        return jsonify({"success": False, "error": f"Erreur serveur : {msg}"}), 500


@app.route("/debug-model", methods=["POST"])
def debug_model():
    data = request.get_json(force=True)
    email    = (data.get("email")    or "").strip()
    password = (data.get("password") or "").strip()
    url      = (data.get("url")      or "").strip()
    mois = data.get("mois", 7)
    annee = data.get("annee", 2026)
    id_contrat = _extract_id_contrat(url)
    try:
        session, base = _ensure_http_session(email, password, url)
        model = _get_vdp(session, base, id_contrat, mois, annee)
        keys_info = {}
        for k, v in model.items():
            if k == 'Jours':
                keys_info[k] = f"[{len(v)} items]"
                if v:
                    keys_info['Jours_0_keys'] = list(v[0].keys()) if isinstance(v[0], dict) else str(type(v[0]))
            elif k == '_full_response':
                keys_info[k] = f"[len={len(str(v))}]"
            elif isinstance(v, (list, dict)):
                keys_info[k] = json.dumps(v, default=str)[:500]
            else:
                keys_info[k] = str(v)[:300]
        # Also try fetching the recap page HTML to find the endpoint
        recap_endpoints = {}
        for ep in [
            f"/Paie/VariablePaieAPI/GetRecapitulatif?idContrat={id_contrat}&mois={mois:02d}&annee={annee}",
            f"/Paie/VariablePaieAPI/GetRecapVDP?idContrat={id_contrat}&mois={mois:02d}&annee={annee}",
            f"/Paie/RecapitulatifAPI/GetRecapitulatif?idContrat={id_contrat}&mois={mois:02d}&annee={annee}",
        ]:
            try:
                r = session.get(f"{base}{ep}", timeout=10)
                ct = r.headers.get('content-type', '')
                recap_endpoints[ep] = {'status': r.status_code, 'ct': ct, 'body': r.text[:500]}
            except Exception as e:
                recap_endpoints[ep] = {'error': str(e)}
        return jsonify({"keys": keys_info, "recap_endpoints": recap_endpoints})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/fetch-week", methods=["POST"])
def fetch_week():
    data = request.get_json(force=True)
    email    = (data.get("email")    or "").strip()
    password = (data.get("password") or "").strip()
    url      = (data.get("url")      or "").strip()
    date_str = (data.get("date")     or "").strip()

    if not email or not password:
        return jsonify({"success": False, "error": "Email et mot de passe requis"}), 400
    if not url:
        return jsonify({"success": False, "error": "URL Ecollaboratrice requise"}), 400

    success, result, debug_keys, recap, taches = fetch_ecollab_days(email, password, url, date_str)
    if success:
        resp = {"success": True, "days": result}
        if recap:
            resp["recap"] = recap
        if taches:
            resp["taches"] = taches
        return jsonify(resp)
    else:
        return jsonify({"success": False, "error": result}), 500


@app.route("/cloture", methods=["POST"])
def cloture():
    data = request.get_json(force=True)

    email    = (data.get("email")    or "").strip()
    password = (data.get("password") or "").strip()
    url      = (data.get("url")      or "").strip()
    plages   = data.get("plages", [])
    date_str = (data.get("date") or "").strip()
    variables = data.get("variables", {})

    if not email or not password:
        return jsonify({"success": False, "error": "Email et mot de passe requis"}), 400
    if not url:
        return jsonify({"success": False, "error": "URL Ecollaboratrice requise"}), 400
    for p in plages:
        if not p.get("debut") or not p.get("fin"):
            return jsonify({"success": False, "error": f"Plage incomplète : {p}"}), 400

    success, message = cloture_direct(email, password, url, plages, date_str, variables)

    if success:
        _send_push(email, "Clôture réussie", message)
        return jsonify({"success": True, "message": message})
    else:
        _send_push(email, "Échec de la clôture", message, is_error=True)
        return jsonify({"success": False, "error": message}), 500


# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'='*50}")
    print(f"  Serveur Pointage CM — port {port}")
    print(f"  Mode : API directe (sans Selenium)")
    print(f"  Test : http://localhost:{port}/ping")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
