#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Serveur de clôture automatique — Charles Murgat
Lance: python server.py
"""

import json
import math
import time
import datetime
import threading
import os
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # Autorise les requêtes depuis la PWA mobile

# ─── UTILS ───────────────────────────────────────────────────────────────────
def to_minutes(hhmm):
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m

def min_to_hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"

# ─── SETUP SELENIUM (shared) ─────────────────────────────────────────────────
def _setup_driver(email, password, url, date_str=""):
    """
    Lance Chrome headless, pose cookie RGPD, navigue vers SaisieRapide, login si nécessaire.
    Retourne (driver, saisie_url) ou lève une Exception.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    import shutil, glob, re as _re

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    chromium_path     = os.environ.get("CHROME_BIN") or \
                        shutil.which("chromium") or \
                        shutil.which("chromium-browser") or \
                        shutil.which("google-chrome")
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH") or \
                        shutil.which("chromedriver")

    if chromium_path:
        opts.binary_location = chromium_path
    if chromedriver_path:
        service = Service(chromedriver_path)
        driver  = webdriver.Chrome(service=service, options=opts)
    else:
        from webdriver_manager.chrome import ChromeDriverManager
        service = Service(ChromeDriverManager().install())
        driver  = webdriver.Chrome(service=service, options=opts)

    # Date cible
    if date_str:
        try:
            dt = datetime.date.fromisoformat(date_str)
        except Exception:
            dt = datetime.date.today()
    else:
        dt = datetime.date.today()
    mois, annee = dt.month, dt.year

    # Injecter mois/année dans l'URL
    url = _re.sub(r'mois=\d+', f'mois={mois:02d}', url)
    url = _re.sub(r'annee=\d+', f'annee={annee}', url)

    # Cookie RGPD + navigation
    base_url = '/'.join(url.split('/')[:3])
    driver.get(base_url)
    time.sleep(1)
    driver.add_cookie({'name': 'alert-rgpd', 'value': 'true',
                       'domain': base_url.replace('https://', '').replace('http://', '')})

    driver.get(url)
    time.sleep(3)

    # Login si nécessaire
    current = driver.current_url.lower()
    if 'login' in current or 'account' in current or 'connect' in current or 'auth' in current:
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By
        email_el = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                "input[type='email'], input[placeholder='Email'], input[name='Email']"))
        )
        email_el.clear()
        email_el.send_keys(email)
        pwd_el = driver.find_element('css selector', "input[type='password']")
        pwd_el.clear()
        pwd_el.send_keys(password)
        driver.find_element('css selector', "button[type='submit']").click()
        time.sleep(4)
        driver.add_cookie({'name': 'alert-rgpd', 'value': 'true',
                           'domain': base_url.replace('https://', '').replace('http://', '')})
        driver.get(url)
        time.sleep(3)

    # Naviguer vers Saisie rapide
    saisie_url = f"{base_url}/Paie/VariablePaie/SaisieRapide?&mois={mois:02d}&annee={annee}"
    driver.get(saisie_url)
    time.sleep(5)  # attendre chargement Vue

    return driver, saisie_url


# ─── LECTURE ECOLLAB ─────────────────────────────────────────────────────────
def fetch_ecollab_days(email, password, url, date_str=""):
    """
    Lit les horaires depuis eCollab pour le mois contenant date_str.
    Retourne (True, {date_iso: {plages, travaille}}) ou (False, erreur).
    """
    driver = None
    try:
        driver, _ = _setup_driver(email, password, url, date_str)

        # Lire tous les jours via JS
        result = driver.execute_script("""
            let vueEl = document.querySelector('#vueSaisieRapide');
            if (!vueEl || !vueEl.__vue__) {
                vueEl = [...document.querySelectorAll('div')].find(e => {
                    try { return e.__vue__?.$data?.currentSalarie; } catch(x) { return false; }
                });
            }
            if (!vueEl) return {error: 'ERR_NO_VUE'};
            const vm = vueEl.__vue__;
            const jours = vm.$data.currentSalarie && vm.$data.currentSalarie.Jours;
            if (!jours || !jours.length) return {error: 'ERR_NO_JOURS'};

            // Debug: lister toutes les clés du premier jour
            const debugKeys = Object.keys(jours[0]).filter(k => {
                const v = jours[0][k];
                return typeof v !== 'function';
            }).map(k => k + '(' + typeof jours[0][k] + ')');

            const days = {};
            for (const j of jours) {
                const mois = String(j.Mois).padStart(2, '0');
                const jour = String(j.Jour).padStart(2, '0');
                // Déterminer l'année depuis l'URL ou la page
                const params = new URLSearchParams(window.location.search);
                const annee = params.get('annee') || new Date().getFullYear();
                const dateKey = annee + '-' + mois + '-' + jour;

                const plages = [];
                if (j.Horaires && j.Horaires.length) {
                    for (const h of j.Horaires) {
                        const deb = h.HeureDebut;
                        const fin = h.HeureFin;
                        if (typeof deb === 'number' && typeof fin === 'number' && (deb > 0 || fin > 0)) {
                            const debH = String(Math.floor(deb/60)).padStart(2,'0');
                            const debM = String(deb%60).padStart(2,'0');
                            const finH = String(Math.floor(fin/60)).padStart(2,'0');
                            const finM = String(fin%60).padStart(2,'0');
                            plages.push({debut: debH+':'+debM, fin: finH+':'+finM});
                        }
                    }
                }
                // Extraire les variables du jour si disponibles
                let variables = null;
                try {
                    // Chercher dans VariablesJour, Variables, ou ValeurVariable
                    const varSources = [j.VariablesJour, j.Variables, j.ValeursVariables, j.ListeVariables];
                    for (const src of varSources) {
                        if (src && Array.isArray(src) && src.length > 0) {
                            variables = {};
                            for (const v of src) {
                                const lib = (v.Libelle || v.libelle || v.Label || v.Nom || '').toUpperCase();
                                const val = v.Valeur || v.valeur || v.Value || v.Quantite || 0;
                                if (lib.indexOf('ASTREINTE') !== -1) variables.astreinte = val;
                                if (lib.indexOf('ELOIGNEMENT') !== -1) variables.indemniteEloignement = val;
                            }
                            break;
                        }
                    }
                } catch(e) {}

                days[dateKey] = {
                    plages: plages,
                    travaille: !!j.EstTravaille,
                    valideSalarie: !!j.ValideeParSalarie,
                    valideEntreprise: !!j.ValideeParEntreprise
                };
                if (variables) days[dateKey].variables = variables;
            }
            return {success: true, days: days, _debug_keys: debugKeys};
        """)

        if not result or result.get('error'):
            driver.quit()
            return False, f"Erreur lecture Vue : {result.get('error', 'inconnu')}", [], None

        # ── Extraction des données Récapitulatif (optionnel) ──
        recap_data = None
        try:
            # Cliquer sur l'onglet Récapitulatif
            tab_clicked = driver.execute_script("""
                var tabs = document.querySelectorAll('a, button, .nav-link, [role="tab"], li');
                for (var i = 0; i < tabs.length; i++) {
                    var t = tabs[i].textContent.trim().toLowerCase();
                    if (t.indexOf('capitulatif') !== -1 || t.indexOf('recap') !== -1) {
                        tabs[i].click();
                        return true;
                    }
                }
                return false;
            """)
            print(f"  [recap] Onglet Récapitulatif cliqué: {tab_clicked}")
            time.sleep(3)

            recap_result = driver.execute_script("""
                var r = {};
                var debugInfo = [];

                // Chercher le composant Vue recap — essayer plusieurs sélecteurs
                var selectors = ['#variable-paie', '#vueSaisieRapide', '[id*="recap"]', '[id*="variable"]', '[id*="paie"]'];
                var vm = null;
                var foundSelector = '';
                for (var i = 0; i < selectors.length; i++) {
                    var el = document.querySelector(selectors[i]);
                    if (el && el.__vue__) {
                        vm = el.__vue__;
                        foundSelector = selectors[i];
                        break;
                    }
                }
                if (!vm) {
                    // Fallback: chercher tout élément avec __vue__
                    var allEls = document.querySelectorAll('*');
                    for (var i = 0; i < allEls.length; i++) {
                        if (allEls[i].__vue__ && allEls[i].id) {
                            debugInfo.push('vue_el: #' + allEls[i].id);
                        }
                    }
                    return {error: 'NO_VUE_FOUND', debugInfo: debugInfo};
                }
                debugInfo.push('found_on: ' + foundSelector);

                // Remonter la chaîne $parent pour trouver le bon composant
                var root = vm;
                var chain = [foundSelector];
                while (root.$parent && root.$parent !== root.$root) {
                    root = root.$parent;
                    chain.push(root.$options && root.$options.name ? root.$options.name : '?');
                }
                debugInfo.push('chain: ' + chain.join(' > '));

                // Explorer toutes les propriétés du vm trouvé (et $root)
                var targets = [{label: 'vm', obj: vm}, {label: '$root', obj: vm.$root}];
                targets.forEach(function(target) {
                    var o = target.obj;
                    if (!o) return;
                    var keys = Object.keys(o.$data || {});
                    var proto = Object.getPrototypeOf(o);
                    if (proto) keys = keys.concat(Object.getOwnPropertyNames(proto));
                    keys = keys.concat(Object.keys(o));
                    var seen = new Set();
                    keys.forEach(function(k) {
                        if (k.startsWith('_') || k.startsWith('$') || seen.has(k)) return;
                        seen.add(k);
                        try {
                            var v = o[k];
                            var t = typeof v;
                            if (t === 'function') return;
                            var prefix = target.label + '.' + k;
                            if (v && t === 'object' && !Array.isArray(v)) {
                                debugInfo.push(prefix + '={' + Object.keys(v).slice(0,10).join(',') + '}');
                            } else if (Array.isArray(v)) {
                                debugInfo.push(prefix + '=[' + v.length + ']');
                            } else if (v !== undefined && v !== null) {
                                var sv = JSON.stringify(v);
                                if (sv && sv.length < 100) debugInfo.push(prefix + '=' + sv);
                            }
                        } catch(e) {}
                    });
                });
                r._debug_recap_keys = debugInfo;

                // ── Méthode 1: Vue properties (noms possibles) ──
                var totalH = vm.TotalHeures || vm.totalHeures || vm.NbHeuresSup || vm.nbHeuresSup || vm.HeuresSupplementaires || '';
                var totalHE = vm.TotalHeuresEquivalent || vm.totalHeuresEquivalent || vm.NbHeuresSupEquivalentes || '';
                r.totalHeures = totalH || '0';
                r.totalHeuresEquivalent = totalHE || totalH || '0';

                // Fériés
                var hf = vm.TotalHeuresJoursFeries || vm.HeuresFeries || vm.JoursFeries || {};
                r.heuresFeries = {
                    total: hf.totalHeuresFeries || hf.Total || hf.total || hf.NbHeures || 0,
                    majorees: hf.totalHeuresFeriesMajorees || hf.Majorees || hf.majorees || 0
                };

                // Nuit
                var hn = vm.TotalHeuresHeuresNuit || vm.HeuresNuit || vm.HeureNuit || {};
                r.heuresNuit = {
                    total: hn.totalHeuresNuit || hn.Total || hn.total || hn.NbHeures || 0,
                    majorees: hn.totalHeuresNuitMajorees || hn.Majorees || hn.majorees || 0
                };

                // Dimanche
                var hd = vm.TotalHeuresHeuresDimanche || vm.HeuresDimanche || vm.HeureDimanche || {};
                r.heuresDimanche = {
                    total: hd.totalHeuresDimanche || hd.Total || hd.total || hd.NbHeures || 0,
                    majorees: hd.totalHeuresDimancheMajorees || hd.Majorees || hd.majorees || 0
                };

                // Détail par semaine
                var detail = vm.RecapitulatifDetailHeuresSuppEquivalentes || vm.DetailHeuresSupp || vm.detailSemaines || [];
                r.detailParSemaine = detail.map ? detail.map(function(d) {
                    return {plage: d.plage || d.Plage || '', heuresSupp: d.heuresSupp || d.HeuresSupp || 0, heuresSuppEquivalentes: d.heuresSuppEquivalentes || d.HeuresSuppEquivalentes || 0};
                }) : [];

                // ── Méthode 2: Scraper le DOM visible du récap ──
                var domData = {};
                var panels = document.querySelectorAll('.panel, .card, .recap, [class*="recap"], [class*="total"], table, .table');
                panels.forEach(function(panel) {
                    var text = panel.innerText || '';
                    // Chercher patterns "Label : valeur" ou "Label valeur h"
                    var lines = text.split('\\n');
                    lines.forEach(function(line) {
                        line = line.trim().toLowerCase();
                        if (line.indexOf('supp') !== -1 && line.match(/[\\d,.]+/)) {
                            domData.heuresSupp = line;
                        }
                        if ((line.indexOf('rié') !== -1 || line.indexOf('ferie') !== -1) && line.match(/[\\d,.]+/)) {
                            domData.heuresFeries = line;
                        }
                        if (line.indexOf('nuit') !== -1 && line.match(/[\\d,.]+/)) {
                            domData.heuresNuit = line;
                        }
                        if (line.indexOf('dimanche') !== -1 && line.match(/[\\d,.]+/)) {
                            domData.heuresDimanche = line;
                        }
                        if ((line.indexOf('récup') !== -1 || line.indexOf('recup') !== -1) && line.match(/[\\d,.]+/)) {
                            domData.recup = line;
                        }
                    });
                });
                r._dom_data = domData;

                // ── Méthode 3: Scraper TOUTES les tables du récap ──
                var tables = document.querySelectorAll('table');
                var tableData = [];
                tables.forEach(function(tbl, idx) {
                    var rows = tbl.querySelectorAll('tr');
                    var rowTexts = [];
                    rows.forEach(function(row) {
                        var cells = row.querySelectorAll('td, th');
                        var cellTexts = [];
                        cells.forEach(function(c) { cellTexts.push(c.innerText.trim()); });
                        if (cellTexts.length > 0) rowTexts.push(cellTexts.join(' | '));
                    });
                    if (rowTexts.length > 0) tableData.push('TABLE' + idx + ': ' + rowTexts.join(' // '));
                });
                r._tables = tableData.slice(0, 5);

                r.success = true;
                return r;
            """)
            print(f"  [recap] Résultat brut: {recap_result}")
            if recap_result and recap_result.get('error'):
                print(f"  [recap] Erreur: {recap_result.get('error')}, info: {recap_result.get('debugInfo', [])}")
            if recap_result and recap_result.get('success'):
                # Log debug info
                if recap_result.get('_debug_recap_keys'):
                    for dk in recap_result['_debug_recap_keys'][:30]:
                        print(f"    [recap-debug] {dk}")
                if recap_result.get('_dom_data'):
                    print(f"    [recap-dom] {recap_result['_dom_data']}")
                if recap_result.get('_tables'):
                    for t in recap_result['_tables']:
                        print(f"    [recap-table] {t[:200]}")
                recap_data = recap_result
                # Nettoyer les clés de debug avant d'envoyer au client
                for k in ['success', '_debug_recap_keys', '_dom_data', '_tables']:
                    recap_data.pop(k, None)
        except Exception as e_recap:
            print(f"  [recap] Extraction récap échouée (non bloquant) : {e_recap}")

        driver.quit()

        return True, result.get('days', {}), result.get('_debug_keys', []), recap_data

    except Exception as e:
        if driver:
            try: driver.quit()
            except: pass
        return False, f"Erreur inattendue : {e}", [], None


# ─── CLÔTURE SELENIUM ────────────────────────────────────────────────────────
def cloture_selenium(email, password, url, plages, date_str="", variables=None):
    """
    Ouvre Chrome, se connecte à Ecollaboratrice, injecte les horaires et variables, sauvegarde.
    Retourne (True, "message") ou (False, "erreur")
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,800")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    # Trouver Chromium et Chromedriver via env ou système
    import shutil, glob

    chromium_path     = os.environ.get("CHROME_BIN") or \
                        shutil.which("chromium") or \
                        shutil.which("chromium-browser") or \
                        shutil.which("google-chrome")

    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH") or \
                        shutil.which("chromedriver")

    try:
        if chromium_path:
            opts.binary_location = chromium_path
        if chromedriver_path:
            service = Service(chromedriver_path)
            driver  = webdriver.Chrome(service=service, options=opts)
        else:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            driver  = webdriver.Chrome(service=service, options=opts)
    except Exception as e:
        return False, f"Impossible de lancer Chrome : {e}"

    try:
        # ── 1. Ouvrir la page ────────────────────────────────────────────────
        # Injecter mois et année courants dans l'URL
        import re as _re
        today_d = datetime.date.today()
        url = _re.sub(r'mois=\d+', f'mois={today_d.month:02d}', url)
        url = _re.sub(r'annee=\d+', f'annee={today_d.year}', url)
        # ── 1b. Ouvrir domaine pour poser le cookie RGPD ─────────────────────
        base_url = '/'.join(url.split('/')[:3])  # ex: https://drive.ecollaboratrice.com
        driver.get(base_url)
        time.sleep(1)
        driver.add_cookie({'name': 'alert-rgpd', 'value': 'true', 'domain': base_url.replace('https://', '').replace('http://', '')})

        driver.get(url)
        time.sleep(3)

        # ── 2. Connexion si nécessaire ───────────────────────────────────────
        current = driver.current_url.lower()
        if 'login' in current or 'account' in current or 'connect' in current or 'auth' in current:
            try:
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                from selenium.webdriver.common.by import By
                email_el = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email'], input[placeholder='Email'], input[name='Email']"))
                )
                email_el.clear()
                email_el.send_keys(email)
                pwd_el = driver.find_element('css selector', "input[type='password']")
                pwd_el.clear()
                pwd_el.send_keys(password)
                driver.find_element('css selector', "button[type='submit']" ).click()
                time.sleep(4)
                # Poser le cookie RGPD après login
                driver.add_cookie({'name': 'alert-rgpd', 'value': 'true', 'domain': base_url.replace('https://', '').replace('http://', '')})
                driver.get(url)
                time.sleep(3)
            except Exception as e:
                driver.quit()
                return False, f'Erreur de connexion : {e}'



        # ── 3. Naviguer vers Saisie rapide ───────────────────────────────────
        if date_str:
            try:
                dt = datetime.date.fromisoformat(date_str)
                mois = dt.month
                annee = dt.year
            except Exception:
                dt = datetime.date.today()
                mois, annee = dt.month, dt.year
        else:
            dt = datetime.date.today()
            mois, annee = dt.month, dt.year

        base_url = '/'.join(url.split('/')[:3])
        saisie_url = f"{base_url}/Paie/VariablePaie/SaisieRapide?&mois={mois:02d}&annee={annee}"
        driver.get(saisie_url)
        time.sleep(3)

        # ── 4. Attendre le chargement Vue ─────────────────────────────────────
        time.sleep(2)

        # ── 5. Injection horaires via model.Jours ────────────────────────────
        plages_min = [{"debut": to_minutes(p["debut"]), "fin": to_minutes(p["fin"])} for p in plages]
        plages_json = json.dumps(plages_min)

        # Extraire jour et mois de la date
        date_parts = date_str.split('-')
        target_mois = int(date_parts[1])
        target_jour = int(date_parts[2])

        result = driver.execute_script(f"""
            const tM = {target_mois};
            const tJ = {target_jour};
            const pl = {plages_json};

            // Trouver le composant Vue Saisie Rapide
            let vueEl = document.querySelector('#vueSaisieRapide');
            if (!vueEl || !vueEl.__vue__) {{
                vueEl = [...document.querySelectorAll('div')].find(e => {{
                    try {{ return e.__vue__?.$data?.currentSalarie; }} catch(x) {{ return false; }}
                }});
            }}
            if (!vueEl) return 'ERR_NO_VUE';
            const vm = vueEl.__vue__;

            // Utiliser currentSalarie.Jours (source reactive des composants)
            const jours = vm.$data.currentSalarie && vm.$data.currentSalarie.Jours;
            if (!jours || !jours.length) return 'ERR_NO_JOURS';

            // Trouver le jour par Jour/Mois
            const jour = jours.find(j => j.Jour === tJ && j.Mois === tM);
            if (!jour) return 'ERR_NO_JOUR:' + tJ + '/' + tM;

            if (pl.length === 0) {{
                // Journee vide : marquer non-travaille et vider les horaires
                vm.$set(jour, 'EstTravaille', false);
                if (jour.Matin) vm.$set(jour.Matin, 'Travaille', false);
                if (jour.ApresMidi) vm.$set(jour.ApresMidi, 'Travaille', false);
                while (jour.Horaires.length > 0) jour.Horaires.pop();
            }} else {{
                // Marquer le jour comme travaille (necessaire pour weekends/feries)
                vm.$set(jour, 'EstTravaille', true);
                if (jour.Matin) vm.$set(jour.Matin, 'Travaille', true);
                if (jour.ApresMidi) vm.$set(jour.ApresMidi, 'Travaille', true);

                // Trouver un horaire de reference pour cloner avec le bon prototype
                let refH = null;
                for (const j of jours) {{
                    if (j.Horaires && j.Horaires.length) {{
                        const h0 = j.Horaires[0];
                        if (typeof h0.TempsTotalPlageHoraire === 'function') {{ refH = h0; break; }}
                    }}
                }}

                // Ajuster le nombre de plages
                while (jour.Horaires.length > pl.length) jour.Horaires.pop();
                while (jour.Horaires.length < pl.length) {{
                    const src = refH || jour.Horaires[0];
                    const clone = Object.create(Object.getPrototypeOf(src));
                    Object.keys(src).forEach(k => {{ clone[k] = src[k]; }});
                    clone.HeureDebut = 0; clone.HeureFin = 0; clone.Id = 0;
                    jour.Horaires.push(clone);
                }}

                // Injecter les valeurs avec Vue.$set pour la reactivite
                for (let i = 0; i < pl.length; i++) {{
                    vm.$set(jour.Horaires[i], 'HeureDebut', pl[i].debut);
                    vm.$set(jour.Horaires[i], 'HeureFin', pl[i].fin);
                }}
            }}

            // Valider le jour (coche verte salarie)
            vm.$set(jour, 'ValideeParSalarie', true);

            // Forcer le re-rendu de tous les composants Vue
            document.querySelectorAll('*').forEach(e => {{
                try {{ if (e.__vue__?.$forceUpdate) e.__vue__.$forceUpdate(); }} catch(x) {{}}
            }});

            return 'OK:' + (pl.length ? pl.map(p => p.debut + '-' + p.fin).join(',') : 'JOUR_VIDE');
        """)

        if not result or not str(result).startswith('OK'):
            driver.quit()
            return False, f"Injection Vue echouee : {result}"

        time.sleep(2)

        # ── 5b. Injection variables eCollab (ASTREINTE, INDEMNITE ELOIGNEMENT) ──
        var_result_msg = ""
        if variables and (variables.get('astreinte', 0) > 0 or variables.get('indemniteEloignement', 0) > 0):
            astreinte_val = int(variables.get('astreinte', 0))
            indemnite_val = int(variables.get('indemniteEloignement', 0))

            # Cliquer sur le jour pour ouvrir le détail (saisie-journée)
            click_result = driver.execute_script(f"""
                const tM = {target_mois};
                const tJ = {target_jour};

                // Chercher la ligne du jour dans les composants ligne-horaire
                var allEls = document.querySelectorAll('*');
                for (var i = 0; i < allEls.length; i++) {{
                    try {{
                        var v = allEls[i].__vue__;
                        if (v && v.$props && v.$props.jour &&
                            v.$props.jour.Jour === tJ && v.$props.jour.Mois === tM) {{
                            // Chercher le lien/bouton cliquable dans la ligne
                            var clickTarget = allEls[i].querySelector('a, .jour-label, td:first-child, .clickable') || allEls[i];
                            clickTarget.click();
                            return 'CLICKED';
                        }}
                    }} catch(e) {{}}
                }}
                // Fallback: chercher par texte du jour
                var dayStr = String(tJ);
                var tds = document.querySelectorAll('td, .day-cell, .jour-cell');
                for (var j = 0; j < tds.length; j++) {{
                    if (tds[j].textContent.trim() === dayStr) {{
                        tds[j].click();
                        return 'CLICKED_TD';
                    }}
                }}
                return 'ERR_NO_DAY_CLICK';
            """)

            if click_result and click_result.startswith('CLICKED'):
                time.sleep(3)  # Attendre ouverture du détail jour

                # Chercher et remplir les champs de variables
                var_set = driver.execute_script(f"""
                    var results = [];
                    var aVal = {astreinte_val};
                    var iVal = {indemnite_val};

                    // Methode 1: Chercher via le composant Vue saisie-journée
                    var sjEl = document.getElementById('vue-vdp-saisie-journee');
                    if (sjEl && sjEl.__vue__) {{
                        var sjVm = sjEl.__vue__;
                        // Essayer de modifier les valeurs via Vue data
                        if (sjVm.valeursVariablesExistantes && sjVm.valeursVariablesExistantes.length) {{
                            for (var k = 0; k < sjVm.valeursVariablesExistantes.length; k++) {{
                                var v = sjVm.valeursVariablesExistantes[k];
                                var label = (sjVm.variablesExistantes && sjVm.variablesExistantes[k])
                                    ? (sjVm.variablesExistantes[k].Libelle || sjVm.variablesExistantes[k].libelle || '').toUpperCase()
                                    : '';
                                if (label.indexOf('ASTREINTE') !== -1 && aVal > 0) {{
                                    sjVm.$set(sjVm.valeursVariablesExistantes, k, aVal);
                                    results.push('VUE_ASTREINTE=' + aVal);
                                }}
                                if (label.indexOf('ELOIGNEMENT') !== -1 && iVal > 0) {{
                                    sjVm.$set(sjVm.valeursVariablesExistantes, k, iVal);
                                    results.push('VUE_INDEMNITE=' + iVal);
                                }}
                            }}
                        }}
                    }}

                    // Methode 2: Chercher les inputs par label (fallback)
                    if (results.length === 0) {{
                        var allLabels = document.querySelectorAll('label, .variable-label, td');
                        for (var i = 0; i < allLabels.length; i++) {{
                            var txt = allLabels[i].textContent.trim().toUpperCase();
                            var parent = allLabels[i].closest('tr, .form-group, .row, .variable-row, div');
                            if (!parent) continue;
                            var input = parent.querySelector('input[type="number"], input[type="text"], input');
                            if (!input) continue;

                            if (txt.indexOf('ASTREINTE') !== -1 && aVal > 0) {{
                                var nativeSet = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                nativeSet.call(input, aVal);
                                input.dispatchEvent(new Event('input', {{bubbles: true}}));
                                input.dispatchEvent(new Event('change', {{bubbles: true}}));
                                results.push('INPUT_ASTREINTE=' + aVal);
                            }}
                            if (txt.indexOf('ELOIGNEMENT') !== -1 && iVal > 0) {{
                                var nativeSet2 = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                                nativeSet2.call(input, iVal);
                                input.dispatchEvent(new Event('input', {{bubbles: true}}));
                                input.dispatchEvent(new Event('change', {{bubbles: true}}));
                                results.push('INPUT_INDEMNITE=' + iVal);
                            }}
                        }}
                    }}

                    // Sauvegarder dans le modal si possible
                    if (results.length > 0) {{
                        var saveBtn = null;
                        var btns = document.querySelectorAll('button');
                        for (var b = 0; b < btns.length; b++) {{
                            var bt = btns[b].textContent.trim().toLowerCase();
                            if (bt === 'enregistrer' || bt === 'valider' || bt === 'sauvegarder') {{
                                saveBtn = btns[b]; break;
                            }}
                        }}
                        if (saveBtn) {{
                            saveBtn.click();
                            results.push('MODAL_SAVED');
                        }}
                    }}

                    return results.length ? results.join(',') : 'NO_VARS_FOUND';
                """)
                var_result_msg = f" | Variables: {var_set}"

                time.sleep(2)

                # Revenir à la vue saisie rapide si on est sur une autre page
                driver.execute_script("""
                    // Fermer le modal si ouvert (chercher bouton fermer/retour)
                    var closeBtn = document.querySelector('.close, .btn-close, [aria-label="Close"], .modal .close');
                    if (closeBtn) closeBtn.click();
                    // Cliquer sur l'onglet Saisie Rapide si nécessaire
                    var tabs = document.querySelectorAll('a, button, .nav-link, [role="tab"]');
                    for (var i = 0; i < tabs.length; i++) {
                        if (tabs[i].textContent.trim().toLowerCase().indexOf('saisie rapide') !== -1) {
                            tabs[i].click(); break;
                        }
                    }
                """)
                time.sleep(2)

        # ── 6. Sauvegarder via Vue method ────────────────────────────────────
        save_result = driver.execute_script("""
            // Appeler directement la methode Vue SaveVariablePaie
            var el = document.querySelector('#vueSaisieRapide');
            if (el && el.__vue__) {
                var vm = el.__vue__;
                if (typeof vm.SaveVariablePaie === 'function') {
                    vm.SaveVariablePaie();
                    return 'VUE_SAVE:SaveVariablePaie()';
                }
            }
            // Fallback : chercher bouton "Sauvegarder" par texte
            var allBtns = document.querySelectorAll('button');
            for (var i = 0; i < allBtns.length; i++) {
                var t = allBtns[i].textContent.trim().toLowerCase();
                if (t === 'sauvegarder' || t === 'sauvegarder et terminer') {
                    allBtns[i].click();
                    return 'CLICKED:' + allBtns[i].textContent.trim();
                }
            }
            return 'ERR_NO_SAVE';
        """)
        time.sleep(3)

        # Verifier s'il y a un message de succes ou d'erreur apres la sauvegarde
        post_save = driver.execute_script("""
            const alerts = document.querySelectorAll('.alert, .toast, .notification, [class*=success], [class*=error], [class*=alert]');
            const msgs = [...alerts].map(a => a.textContent.trim()).filter(t => t.length > 0 && t.length < 200);
            return { save_btn: arguments[0] || 'none', messages: msgs.slice(0, 5), url: window.location.href };
        """)

        resume = " | ".join(f"{p['debut']} \u2192 {p['fin']}" for p in plages) if plages else "Journée vide"
        if date_str:
            parts = date_str.split('-')
            date_label = f"{parts[2]}/{parts[1]}/{parts[0]}"
        else:
            date_label = "aujourd'hui"
        return True, f"Cl\u00f4ture r\u00e9ussie ({date_label}) : {resume}{var_result_msg}"

    except Exception as e:
        try:
            driver.quit()
        except Exception:
            pass
        return False, f"Erreur inattendue : {e}"


# ─── ROUTES API ──────────────────────────────────────────────────────────────

@app.route("/debug", methods=["GET"])
def debug():
    """Diagnostique Chrome/Chromedriver sur le serveur."""
    import shutil, glob, os
    def find_bin(*names):
        for name in names:
            p = shutil.which(name)
            if p: return p
        for name in names:
            matches = glob.glob(f"/nix/store/*/{name}") + glob.glob(f"/nix/store/*/bin/{name}")
            if matches: return matches[0]
        return None

    return jsonify({
        "chromium":     find_bin("chromium", "chromium-browser", "google-chrome"),
        "chromedriver": find_bin("chromedriver"),
        "PATH":         os.environ.get("PATH", ""),
        "nix_chromium": glob.glob("/nix/store/*/bin/chromium")[:3],
        "nix_driver":   glob.glob("/nix/store/*/bin/chromedriver")[:3],
    })


@app.route("/screenshot", methods=["POST"])
def screenshot():
    import base64, re as _re
    data     = request.get_json(force=True)
    email    = data.get("email","").strip()
    password = data.get("password","").strip()
    url      = data.get("url","").strip()
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    opts = Options()
    opts.add_argument("--headless=new"); opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage"); opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1280,900")
    cp = os.environ.get("CHROME_BIN") or shutil.which("chromium") or shutil.which("chromium-browser")
    dp = os.environ.get("CHROMEDRIVER_PATH") or shutil.which("chromedriver")
    if cp: opts.binary_location = cp
    driver = webdriver.Chrome(service=Service(dp), options=opts)
    try:
        today_d = datetime.date.today()
        url = _re.sub(r'mois=\d+', f'mois={today_d.month:02d}', url)
        url = _re.sub(r'annee=\d+', f'annee={today_d.year}', url)
        driver.get(url); time.sleep(3)
        cur = driver.current_url.lower()
        if "login" in cur or "account" in cur or "connect" in cur or "auth" in cur:
            try:
                inputs = driver.find_elements("css selector","input")
                email_el = next((i for i in inputs if i.get_attribute("placeholder") in ("Email","email","Login","login") or i.get_attribute("type")=="email" or i.get_attribute("name") in ("Email","email")), None)
                pwd_el   = next((i for i in inputs if i.get_attribute("type")=="password"), None)
                if email_el: email_el.clear(); email_el.send_keys(email)
                if pwd_el:   pwd_el.clear();   pwd_el.send_keys(password)
                btns = driver.find_elements("css selector","button, input[type='submit']")
                btn  = next((b for b in btns if b.text.strip().upper() in ("SE CONNECTER","CONNEXION","CONNECT","LOGIN","VALIDER") or b.get_attribute("type")=="submit"), None)
                if btn: btn.click()
                elif pwd_el:
                    from selenium.webdriver.common.keys import Keys
                    pwd_el.send_keys(Keys.RETURN)
                time.sleep(4)
            except: pass
        # Capturer HTML popup RGPD AVANT toute tentative
        popup_html = driver.execute_script(
            "const modals=[...document.querySelectorAll('[class*=modal],[class*=popup],[class*=rgpd],[class*=overlay]')];"
            "if(modals.length) return modals[0].outerHTML.substring(0,2000);"
            "return 'NO_MODAL_FOUND';"
        )
        # Tous les boutons visibles sur la page
        all_buttons = driver.execute_script(
            "return [...document.querySelectorAll('button,a')].map(b=>({"
            "  tag:b.tagName, text:b.textContent.trim().substring(0,50),"
            "  cls:b.className.substring(0,50), visible:b.offsetParent!==null"
            "})).filter(b=>b.text.length>0).slice(0,30);"
        )
        time.sleep(1)
        # Inspecter le bouton RGPD en détail
        rgpd_info = driver.execute_script(
            "const all=[...document.querySelectorAll('button,a,span,div,p')];"
            "const matches=all.filter(x=>x.textContent.includes('COMPRIS'));"
            "return matches.map(x=>({"
            "  tag:x.tagName,"
            "  text:JSON.stringify(x.textContent.trim()),"
            "  html:x.outerHTML.substring(0,200),"
            "  codes:[...x.textContent].map(c=>c.charCodeAt(0))"
            "}));"
        )
        rgpd_still_open = bool(rgpd_info)
        png = driver.get_screenshot_as_base64()
        day_cells = driver.execute_script("""
            const cells = document.querySelectorAll('td, [class*="jour"], [class*="day"]');
            return Array.from(cells).slice(0,30).map(c => ({
                tag: c.tagName, text: c.textContent.trim().substring(0,30),
                hasOnclick: !!c.onclick, cls: c.className.substring(0,50)
            }));
        """)
        final_url = driver.current_url; title = driver.title
        driver.quit()
        return jsonify({"title":title,"url":final_url,"screenshot":png,"day_cells":day_cells,"rgpd_open":rgpd_still_open,"rgpd_info":rgpd_info,"popup_html":popup_html,"all_buttons":all_buttons})
    except Exception as e:
        try: driver.quit()
        except: pass
        return jsonify({"error":str(e)}), 500


@app.route("/ping", methods=["GET"])
def ping():
    """Test de connexion depuis la PWA."""
    return jsonify({"status": "ok", "message": "Serveur opérationnel"})


@app.route("/test-login", methods=["POST"])
def test_login():
    """
    Teste les identifiants eCollab via Selenium.
    Corps : { "email": "...", "password": "...", "url": "..." }
    """
    data = request.get_json(force=True)
    email    = (data.get("email")    or "").strip()
    password = (data.get("password") or "").strip()
    url      = (data.get("url")      or "").strip()

    if not email or not password:
        return jsonify({"success": False, "error": "Email et mot de passe requis"}), 400
    if not url:
        return jsonify({"success": False, "error": "URL Ecollaboratrice requise"}), 400

    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By
        import re as _re

        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-blink-features=AutomationControlled")

        chromium_path   = os.environ.get("CHROMIUM_PATH")
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
        if chromium_path:
            opts.binary_location = chromium_path
        if chromedriver_path:
            service = Service(chromedriver_path)
            driver  = webdriver.Chrome(service=service, options=opts)
        else:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            driver  = webdriver.Chrome(service=service, options=opts)

        # Ouvrir domaine + cookie RGPD
        base_url = '/'.join(url.split('/')[:3])
        driver.set_page_load_timeout(15)
        driver.get(base_url)
        time.sleep(1)
        driver.add_cookie({'name': 'alert-rgpd', 'value': 'true', 'domain': base_url.replace('https://', '').replace('http://', '')})

        # Naviguer vers l'URL eCollab (redirige vers login)
        today_d = datetime.date.today()
        url = _re.sub(r'mois=\d+', f'mois={today_d.month:02d}', url)
        url = _re.sub(r'annee=\d+', f'annee={today_d.year}', url)
        driver.get(url)

        # Attendre le champ email (max 10s)
        email_el = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email'], input[placeholder='Email'], input[name='Email']"))
        )
        email_el.clear()
        email_el.send_keys(email)
        pwd_el = driver.find_element('css selector', "input[type='password']")
        pwd_el.clear()
        pwd_el.send_keys(password)
        btn = driver.find_element('css selector', "button[type='submit']")
        driver.execute_script("arguments[0].click();", btn)

        # Attendre la redirection apres soumission
        time.sleep(3)

        # Re-naviguer vers l'URL cible pour verifier si la session est active
        driver.get(url)
        time.sleep(2)
        after_url = driver.current_url.lower()
        driver.quit()

        # Si redirige vers login = identifiants incorrects
        if 'login' in after_url or 'account' in after_url or 'connect' in after_url or 'auth' in after_url:
            return jsonify({"success": False, "error": "Identifiants incorrects"})
        # Sinon on a acces a la page = login OK
        return jsonify({"success": True, "message": "Connexion reussie"})

    except Exception as e:
        try:
            driver.quit()
        except:
            pass
        msg = str(e).split('\n')[0]  # Premiere ligne seulement, pas le stacktrace
        return jsonify({"success": False, "error": f"Erreur serveur : {msg}"}), 500


@app.route("/fetch-week", methods=["POST"])
def fetch_week():
    """
    Lit les horaires depuis eCollab pour le mois de la date donnée.
    Corps : { "email": "...", "password": "...", "url": "...", "date": "2026-03-08" }
    """
    data = request.get_json(force=True)
    email    = (data.get("email")    or "").strip()
    password = (data.get("password") or "").strip()
    url      = (data.get("url")      or "").strip()
    date_str = (data.get("date")     or "").strip()

    if not email or not password:
        return jsonify({"success": False, "error": "Email et mot de passe requis"}), 400
    if not url:
        return jsonify({"success": False, "error": "URL Ecollaboratrice requise"}), 400

    success, result, debug_keys, recap = fetch_ecollab_days(email, password, url, date_str)
    if success:
        resp = {"success": True, "days": result}
        if debug_keys:
            resp["_debug_keys"] = debug_keys
        if recap:
            resp["recap"] = recap
        return jsonify(resp)
    else:
        return jsonify({"success": False, "error": result}), 500


@app.route("/cloture", methods=["POST"])
def cloture():
    """
    Corps attendu :
    {
        "email":    "user@example.com",
        "password": "••••••••",
        "url":      "https://drive.ecollaboratrice.com/...",
        "plages":   [{"debut": "08:00", "fin": "12:00"}, ...]
    }
    """
    data = request.get_json(force=True)

    email    = (data.get("email")    or "").strip()
    password = (data.get("password") or "").strip()
    url      = (data.get("url")      or "").strip()
    plages   = data.get("plages", [])
    date_str = (data.get("date") or "").strip()  # date réelle du pointage YYYY-MM-DD
    variables = data.get("variables", {})  # {astreinte: N, indemniteEloignement: N}

    # Validation
    if not email or not password:
        return jsonify({"success": False, "error": "Email et mot de passe requis"}), 400
    if not url:
        return jsonify({"success": False, "error": "URL Ecollaboratrice requise"}), 400
    # Vérifier format plages (vide = journée non travaillée)
    for p in plages:
        if not p.get("debut") or not p.get("fin"):
            return jsonify({"success": False, "error": f"Plage incomplète : {p}"}), 400

    # Lancer la clôture
    success, message = cloture_selenium(email, password, url, plages, date_str, variables)

    if success:
        return jsonify({"success": True, "message": message})
    else:
        return jsonify({"success": False, "error": message}), 500


# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"\n{'='*50}")
    print(f"  Serveur Pointage CM — port {port}")
    print(f"  Test : http://localhost:{port}/ping")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
