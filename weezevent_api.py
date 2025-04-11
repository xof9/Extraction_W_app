import requests
from datetime import datetime, date
from db_connection import get_connection
import os
from dotenv import load_dotenv
import logging
import traceback
import decimal # Pour gérer les montants
import json
import mysql.connector

try:
    # Fonction pour obtenir le token d'accès Weezevent
    from weezevent_utils import get_access_token
except ImportError:
    logging.critical("ERREUR CRITIQUE: Impossible d'importer 'get_access_token' depuis 'weezevent_utils.py'.")
    def get_access_token():
        logging.error("Fonction get_access_token non trouvée.")
        return None

load_dotenv()

API_KEY = os.getenv("WEEZEVENT_API_KEY")
if not API_KEY:
     logging.critical("ERREUR CRITIQUE: WEEZEVENT_API_KEY n'est pas défini dans .env.")

def get_participant_answers(access_token, participant_id):
    """Récupère les réponses au formulaire pour un participant donné."""
    if not API_KEY:
        logging.error("API_KEY manquant pour get_participant_answers.")
        return {}
    if not access_token:
        logging.error(f"Access token manquant pour get_participant_answers (participant {participant_id}).")
        return {}

    url = f"https://api.weezevent.com/participant/{participant_id}/answers?api_key={API_KEY}&access_token={access_token}"
    logging.debug(f"Récupération réponses pour participant ID: {participant_id}")
    response = None
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 404: # Pas une erreur, juste pas de réponse
             logging.warning(f"Aucune réponse trouvée (404) pour participant {participant_id}.")
             return {}
        response.raise_for_status() # Lève une exception pour les autres erreurs HTTP

        answers_data = response.json().get("answers", [])
        answers_dict = {}
        for ans in answers_data:
            # Clé normalisée (minuscule, sans espaces) pour faciliter la recherche
            label = str(ans.get("label", "")).strip().lower()
            value = ans.get("value")
            if label:
                answers_dict[label] = value

        logging.debug(f"Réponses pour {participant_id} traitées.")
        return answers_dict
    except requests.exceptions.Timeout:
        logging.error(f"Erreur connexion/requête (Timeout) réponses participant {participant_id}")
        return {}
    except requests.exceptions.RequestException as e:
        status_code = response.status_code if response is not None else 'N/A'
        response_text = response.text if response is not None else 'N/A'
        logging.error(f"Erreur connexion/requête réponses participant {participant_id}: {e} (Status: {status_code})")
        logging.debug(f"Détails erreur requête réponses: Response={response_text[:500]}")
        return {}
    except json.JSONDecodeError as e_json:
        response_text = response.text if response is not None else 'N/A'
        logging.error(f"Erreur décodage JSON réponse answers pour participant {participant_id}: {e_json}")
        logging.debug(f"Réponse brute non-JSON (answers): {response_text[:500]}")
        return {}
    except Exception as e:
        logging.error(f"Erreur inattendue (réponses participant {participant_id}): {e}", exc_info=True)
        return {}

def normalize_data(data, *keys):
    """Recherche une valeur pour plusieurs clés possibles (insensible casse), retourne la première trouvée (str)."""
    if not isinstance(data, dict):
        return ""
    for key_or_value in keys:
        value_str = None
        if isinstance(key_or_value, str):
            search_key = key_or_value.lower().strip() # Recherche insensible à la casse
            if search_key in data:
                raw_value = data[search_key]
                value_str = str(raw_value).strip() if raw_value is not None else ""
                if value_str: return value_str
        elif key_or_value is not None: # Si une valeur directe est passée en fallback
            value_str = str(key_or_value).strip()
            if value_str: return value_str
    return "" # Retourne chaîne vide si rien n'est trouvé

def parse_date(date_str):
    """Tente de parser une date (DD/MM/YYYY, YYYY-MM-DD, DD-MM-YYYY). Retourne objet date ou None."""
    if not date_str or not isinstance(date_str, str): return None
    date_str_cleaned = date_str.strip()
    if not date_str_cleaned: return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            # Retourne un objet date (sans heure)
            return datetime.strptime(date_str_cleaned, fmt).date()
        except ValueError:
            continue
    logging.warning(f"Format date non reconnu/invalide: '{date_str_cleaned}'")
    return None

def parse_datetime(datetime_str):
    """Tente de parser date+heure (YYYY-MM-DD HH:MM:SS, YYYY-MM-DDTHH:MM:SS). Retourne objet datetime ou None."""
    if not datetime_str or not isinstance(datetime_str, str): return None
    datetime_str_cleaned = datetime_str.strip()
    if not datetime_str_cleaned: return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"): # Gère le 'T' séparateur
        try:
            return datetime.strptime(datetime_str_cleaned, fmt)
        except ValueError:
            continue
    logging.warning(f"Format datetime non reconnu/invalide: '{datetime_str_cleaned}'")
    return None

def save_to_db(nom, prenom, email, telephone, date_naissance_str, adresse, ville, code_postal, event_id,
               source_info, financement_eligible, rqth, amenagements_necessaires, amenagements_details,
               montant_paye, nom_billet, code_promo, date_creation_inscription_str):
    """Enregistre ou met à jour un participant dans la DB via ON DUPLICATE KEY UPDATE."""
    conn = None
    cursor = None
    logging.debug(f"save_to_db: Tentative sauvegarde pour {email} (Event: {event_id})")

    # Nettoyage et validation des données
    nom_cleaned = str(nom).strip()[:255] if nom else ""
    prenom_cleaned = str(prenom).strip()[:255] if prenom else ""
    email_cleaned = str(email).strip().lower()[:255] if email else ""
    if not email_cleaned:
         logging.error(f"save_to_db: Email manquant pour Nom='{nom_cleaned}', Prénom='{prenom_cleaned}'. Sauvegarde annulée.")
         return # Email est requis (potentiellement partie de la clé unique)

    telephone_cleaned = str(telephone).strip()[:20] if telephone else None
    adresse_cleaned = str(adresse).strip() if adresse else None
    ville_cleaned = str(ville).strip()[:100] if ville else None
    code_postal_cleaned = str(code_postal).strip()[:10] if code_postal else None

    date_naissance_obj = parse_date(date_naissance_str) # Peut être None
    date_creation_obj = parse_datetime(date_creation_inscription_str) # Peut être None
    logging.debug(f"  -> Dates parsées pour DB: Naissance={date_naissance_obj}, Création={date_creation_obj}")

    source_info_cleaned = str(source_info).strip()[:255] if source_info else None
    financement_eligible_cleaned = str(financement_eligible).strip()[:50] if financement_eligible else None
    rqth_cleaned = str(rqth).strip()[:10] if rqth else None
    amenagements_necessaires_cleaned = str(amenagements_necessaires).strip()[:10] if amenagements_necessaires else None
    amenagements_details_cleaned = str(amenagements_details).strip() if amenagements_details else None
    nom_billet_cleaned = str(nom_billet).strip()[:255] if nom_billet else None
    code_promo_cleaned = str(code_promo).strip()[:100] if code_promo else None

    # Conversion et validation montant payé en Decimal
    montant_paye_decimal = None
    if montant_paye is not None:
        try:
            # Gère les virgules (français) et les espaces comme séparateur décimal
            montant_paye_str_norm = str(montant_paye).replace(',', '.').strip()
            if montant_paye_str_norm: # Vérifie si chaîne non vide après nettoyage
                 montant_paye_decimal = decimal.Decimal(montant_paye_str_norm)
        except (ValueError, TypeError, decimal.InvalidOperation) as e:
             logging.warning(f"  -> Impossible de convertir montant payé '{montant_paye}' en Decimal pour {email_cleaned}: {e}. Sera NULL.")
             # montant_paye_decimal reste None

    logging.debug(f"  -> Montant payé pour DB: {montant_paye_decimal}")

    # Requête SQL - Assurez-vous que la clé unique est bien (email, event_id) ou équivalent
    sql = """
       INSERT INTO inscriptions (
           nom, prenom, email, telephone, date_naissance, adresse, ville, code_postal, event_id,
           source_info, financement_eligible, rqth, amenagements_necessaires, amenagements_details,
           montant_paye, nom_billet, code_promo, date_creation_inscription
       ) VALUES (
           %(nom)s, %(prenom)s, %(email)s, %(telephone)s, %(date_naissance)s, %(adresse)s, %(ville)s, %(code_postal)s, %(event_id)s,
           %(source_info)s, %(financement_eligible)s, %(rqth)s, %(amenagements_necessaires)s, %(amenagements_details)s,
           %(montant_paye)s, %(nom_billet)s, %(code_promo)s, %(date_creation)s
       ) ON DUPLICATE KEY UPDATE
           nom=VALUES(nom), prenom=VALUES(prenom), telephone=VALUES(telephone), date_naissance=VALUES(date_naissance),
           adresse=VALUES(adresse), ville=VALUES(ville), code_postal=VALUES(code_postal), source_info=VALUES(source_info),
           financement_eligible=VALUES(financement_eligible), rqth=VALUES(rqth),
           amenagements_necessaires=VALUES(amenagements_necessaires), amenagements_details=VALUES(amenagements_details),
           montant_paye=VALUES(montant_paye), nom_billet=VALUES(nom_billet), code_promo=VALUES(code_promo),
           date_creation_inscription=VALUES(date_creation_inscription);
       """
    params = {
        'nom': nom_cleaned, 'prenom': prenom_cleaned, 'email': email_cleaned, 'telephone': telephone_cleaned,
        'date_naissance': date_naissance_obj, 'adresse': adresse_cleaned, 'ville': ville_cleaned, 'code_postal': code_postal_cleaned,
        'event_id': int(event_id), 'source_info': source_info_cleaned, 'financement_eligible': financement_eligible_cleaned, 'rqth': rqth_cleaned,
        'amenagements_necessaires': amenagements_necessaires_cleaned, 'amenagements_details': amenagements_details_cleaned,
        'montant_paye': montant_paye_decimal, 'nom_billet': nom_billet_cleaned, 'code_promo': code_promo_cleaned,
        'date_creation': date_creation_obj
    }

    try:
        conn = get_connection() # Depuis le pool
        if not conn:
            logging.error(f"save_to_db: Impossible d'obtenir une connexion DB pour {email_cleaned}")
            return
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()
        affected_rows = cursor.rowcount
        # rowcount: 1=INSERT, 2=UPDATE, 0=Aucun changement (MySQL)
        if affected_rows == 1: logging.info(f"DB OK: Participant {email_cleaned} (Event: {event_id}) inséré.")
        elif affected_rows == 2: logging.info(f"DB OK: Participant {email_cleaned} (Event: {event_id}) mis à jour.")
        elif affected_rows == 0: logging.info(f"DB OK: Participant {email_cleaned} (Event: {event_id}) déjà à jour.")
        else: logging.warning(f"DB: Rowcount inattendu ({affected_rows}) pour {email_cleaned} (Event: {event_id}).")
    except mysql.connector.Error as db_err:
         logging.error(f"Erreur DB sauvegarde {email_cleaned} (Event: {event_id}): {db_err}", exc_info=True)
         # Log SQL et Params pour debug en cas d'erreur
         try: debug_sql = cursor.statement if cursor else sql; logging.error(f"   -> SQL Échoué (approx): {debug_sql}")
         except Exception as log_e: logging.error(f"   -> Erreur formatage SQL debug: {log_e}")
         logging.error(f"   -> Params Échoués: {params}")
         if conn:
             try: conn.rollback()
             except Exception as rb_err: logging.error(f"  -> Erreur rollback: {rb_err}")
    except Exception as e:
        logging.error(f"Erreur non-DB sauvegarde {email_cleaned} (Event: {event_id}): {e}", exc_info=True)
        if conn:
            try: conn.rollback()
            except Exception as rb_err: logging.error(f"  -> Erreur rollback: {rb_err}")
    finally:
        if cursor: cursor.close()
        if conn: conn.close() # Remet la connexion dans le pool


def get_active_event_ids():
    """
    Récupère IDs des événements depuis BDD: actifs (non-annulés) ET futurs/sans date.
    Utilisés pour filtrer les participants à synchroniser.
    """
    conn = None
    cursor = None
    event_ids = []
    logging.debug("get_active_event_ids: Récupération IDs actifs ET futurs/sans date BDD...")

    try:
        conn = get_connection()
        if not conn:
            logging.error("get_active_event_ids: Connexion DB échouée depuis le pool.")
            return []

        cursor = conn.cursor()
        # Sélectionne les IDs des événements actifs (non-annulés) ET futurs (ou sans date)
        # Assurez-vous que la colonne 'date' est de type DATE ou DATETIME.
        sql_get_ids = """
            SELECT event_id FROM evenements
            WHERE actif = 1 AND (date IS NULL OR date >= CURDATE())
        """
        cursor.execute(sql_get_ids)
        results = cursor.fetchall()

        # Extrait l'ID de chaque tuple résultat
        event_ids = [row[0] for row in results if row and row[0] is not None]

        logging.info(f"IDs événements actifs ET futurs/sans date trouvés en BDD pour synchro: {event_ids}")
        if not event_ids:
            logging.info("Aucun événement actif ET futur/sans date trouvé en BDD pour synchro participants.")

        return event_ids

    except mysql.connector.Error as db_err:
        logging.error(f"Erreur DB récupération event_ids actifs/futurs: {db_err}", exc_info=True)
        return []
    except Exception as e:
        logging.error(f"Erreur inattendue récupération event_ids actifs/futurs: {e}", exc_info=True)
        return []
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


def get_ticket_prices(access_token, event_ids):
    """Récupère les prix de base des billets pour une liste d'event_ids (pour fallback)."""
    if not API_KEY:
        logging.error("API_KEY manquant pour get_ticket_prices.")
        return {}
    if not access_token:
        logging.error("Access token manquant pour get_ticket_prices.")
        return {}
    if not event_ids:
        logging.warning("get_ticket_prices: Aucun event_id fourni.")
        return {}

    ticket_prices = {}
    # Construit paramètre id_event[]=... pour l'URL
    id_param = "&".join([f"id_event[]={eid}" for eid in event_ids])
    url = f"https://api.weezevent.com/tickets?api_key={API_KEY}&access_token={access_token}&{id_param}"
    logging.info(f"Récupération prix de base billets pour {len(event_ids)} événements...")
    logging.debug(f"Appel API tickets: {url}")
    response = None

    try:
        response = requests.get(url, timeout=20) # Timeout un peu plus long
        response.raise_for_status()
        data = response.json()

        # Fonction récursive pour extraire les tickets des événements et catégories
        def extract_tickets(items_list):
            prices = {}
            if not isinstance(items_list, list): return prices
            for item in items_list:
                if not isinstance(item, dict): continue
                # Tickets directs dans l'item
                if "tickets" in item and isinstance(item["tickets"], list):
                    for ticket in item["tickets"]:
                        if isinstance(ticket, dict):
                            ticket_id = ticket.get("id")
                            ticket_price = ticket.get("price") # Prix de base
                            if ticket_id is not None:
                                prices[str(ticket_id)] = ticket_price # ID comme chaîne
                # Appel récursif pour sous-catégories
                if "categories" in item and isinstance(item["categories"], list):
                    prices.update(extract_tickets(item["categories"]))
            return prices

        found_prices = {}
        if "events" in data and isinstance(data["events"], list):
             found_prices = extract_tickets(data["events"])
        elif isinstance(data, list): # Si la réponse est directement une liste
             found_prices = extract_tickets(data)
        else:
             logging.warning(f"Structure inattendue réponse API /tickets: Clés = {list(data.keys())}")

        ticket_prices.update(found_prices)
        logging.info(f"Récupéré {len(ticket_prices)} définitions de prix de base de billets.")
        logging.debug(f"Prix de base billets (ID -> Prix Base): {ticket_prices}")
        return ticket_prices

    except requests.exceptions.Timeout:
        logging.error(f"Erreur requête API /tickets: Timeout.")
        return {}
    except requests.exceptions.RequestException as e:
        status_code = response.status_code if response is not None else 'N/A'
        response_text = response.text if response is not None else 'N/A'
        logging.error(f"Erreur requête API /tickets: {e} (Status: {status_code})")
        logging.debug(f"Détails erreur API tickets: Response={response_text[:500]}")
        return {}
    except json.JSONDecodeError as e_json:
        response_text = response.text if response is not None else 'N/A'
        logging.error(f"Erreur décodage JSON réponse /tickets: {e_json}")
        logging.debug(f"Réponse brute non-JSON (/tickets): {response_text[:500]}")
        return {}
    except Exception as e:
        logging.error(f"Erreur inattendue récupération prix billets: {e}", exc_info=True)
        return {}

def get_registrations():
    """Fonction principale: récupère et traite inscriptions des événements actifs ET futurs/sans date."""
    logging.info("="*20 + " DÉBUT SYNCHRO PARTICIPANTS " + "="*20)

    # Récupère IDs des événements pertinents depuis la BDD
    event_ids = get_active_event_ids()
    if not event_ids:
        logging.info("Aucun événement actif et futur/sans date trouvé pour la synchronisation. Arrêt.")
        logging.info("="*20 + " FIN SYNCHRO (Aucun Event Pertinent) " + "="*20)
        return

    access_token = get_access_token()
    if not access_token:
        logging.error("Impossible de continuer sans token d'accès.")
        logging.info("="*20 + " FIN SYNCHRO (Erreur Token) " + "="*20)
        return

    if not API_KEY: # Vérification redondante mais sûre
         logging.error("API_KEY manquant. Impossible de continuer.")
         logging.info("="*20 + " FIN SYNCHRO (Erreur API_KEY) " + "="*20)
         return

    # Récupère les prix de base (pour fallback si prix final non trouvé)
    logging.info("Récupération prix de base des billets (fallback)...")
    all_ticket_prices = get_ticket_prices(access_token, event_ids)
    if not all_ticket_prices:
         logging.warning("Aucun prix de base de billet récupéré. Fallback de prix impossible.")
    else:
         logging.info(f"{len(all_ticket_prices)} prix de base récupérés.")

    total_participants_api = 0
    total_participants_processed = 0

    # Boucle sur les événements pertinents
    for event_id in event_ids:
        logging.info(f"--- Traitement Événement ID: {event_id} ---")
        url_participants = (f"https://api.weezevent.com/participant/list?"
                            f"api_key={API_KEY}&access_token={access_token}&id_event[]={event_id}&full=1")
        logging.debug(f"Appel API participants: {url_participants}")
        response = None

        try:
            response = requests.get(url_participants, timeout=45) # Timeout plus long
            response.raise_for_status()
            data = response.json()

            if "participants" not in data:
                 logging.error(f"Clé 'participants' manquante dans réponse API pour event {event_id}.")
                 logging.debug(f"Réponse API brute: {str(data)[:1000]}")
                 continue # Événement suivant

            participants_api_data = data.get("participants", [])
            count_api_event = len(participants_api_data)
            total_participants_api += count_api_event
            logging.info(f"API a retourné {count_api_event} participants pour l'événement {event_id}.")

            if not participants_api_data:
                logging.info(f"Aucun participant pour l'événement {event_id}. Passage au suivant.")
                continue

            processed_in_event = 0
            # Boucle sur les participants de cet événement
            for index, p_data in enumerate(participants_api_data):
                participant_num = index + 1
                logging.debug(f"\nTraitement P {participant_num}/{count_api_event} (Event {event_id})...")

                if not isinstance(p_data, dict):
                    logging.warning(f"P {participant_num} ignoré (Event {event_id}): Donnée non valide.")
                    continue

                participant_id = p_data.get("id_participant")
                if not participant_id:
                    owner_data_log = p_data.get("owner", {})
                    email_log = str(owner_data_log.get("email") or p_data.get("email") or "N/A").strip().lower()
                    logging.warning(f"P {participant_num} ignoré (Event {event_id}): ID Participant Manquant. Email: {email_log}.")
                    continue

                logging.debug(f"  Participant ID: {participant_id}")

                answers = get_participant_answers(access_token, participant_id)

                # Extraction des données participant
                owner_data = p_data.get("owner", {})
                if not isinstance(owner_data, dict): owner_data = {}

                nom = owner_data.get("last_name", p_data.get("last_name", ""))
                prenom = owner_data.get("first_name", p_data.get("first_name", ""))
                email = str(owner_data.get("email") or p_data.get("email") or "").strip().lower()
                if not email:
                    logging.warning(f"P {participant_num} (ID: {participant_id}, Event: {event_id}) ignoré: Email manquant.")
                    continue # Email requis

                telephone = normalize_data(answers, "telephone", "portable", owner_data.get("phone"), p_data.get("phone"))
                date_naissance_str = normalize_data(answers, "date de naissance", "date_de_naissance", owner_data.get("birthdate"), p_data.get("birthdate"))
                adresse = normalize_data(answers, "adresse", owner_data.get("address"), p_data.get("address"))
                ville = normalize_data(answers, "ville", owner_data.get("city"), p_data.get("city"))
                code_postal = normalize_data(answers, "code postal", "code_postal", owner_data.get("zipcode"), p_data.get("zipcode"))

                # Champs spécifiques formulaire (adapter les libellés exacts si besoin)
                libelle_exact_source = "comment avez-vous entendu parler de la compagnie maritime ? (bouche à oreille, site, presse, réseaux sociaux, autres à préciser)."
                libelle_exact_financement = "êtes-vous éligible à un financement pour cette formation ?"
                libelle_exact_rqth = "bénéficiez-vous d'une rqth ?"
                libelle_exact_amenagement_combine = "avez-vous besoin d'aménagements nécessaires pour facilité l'accès à la formation ? si oui, précisez"

                source_info = normalize_data(answers, libelle_exact_source)
                financement_eligible = normalize_data(answers, libelle_exact_financement)
                rqth = normalize_data(answers, libelle_exact_rqth)

                # Logique pour les aménagements (Oui/Non + Détails)
                valeur_amenagement_combine = normalize_data(answers, libelle_exact_amenagement_combine)
                amenagements_necessaires = None
                amenagements_details = None
                if valeur_amenagement_combine:
                    if valeur_amenagement_combine.lower().strip() in ["non", "no", "0", "false", "aucun"]:
                        amenagements_necessaires = "Non"
                    else:
                        amenagements_necessaires = "Oui"
                        amenagements_details = valeur_amenagement_combine # La valeur est le détail

                # Données directes depuis p_data
                code_promo = p_data.get("promo_code", "")
                date_creation_inscription_str = p_data.get("create_date", "") # Ex: 'YYYY-MM-DD HH:MM:SS'
                id_ticket_str = str(p_data.get("id_ticket", ""))
                nom_billet = p_data.get("ticket_name", id_ticket_str if id_ticket_str else "N/A")

                # --- Logique Montant Payé ---
                montant_a_sauvegarder = None
                # !!! ACTION REQUISE !!!
                # Vérifiez le nom exact du champ contenant le PRIX FINAL PAYÉ dans vos données p_data.
                CHAMP_PRIX_FINAL_API = "PRICE_FIELD_NOT_FOUND_IN_LOGS" # Placeholder - À METTRE À JOUR !

                if CHAMP_PRIX_FINAL_API != "PRICE_FIELD_NOT_FOUND_IN_LOGS" and CHAMP_PRIX_FINAL_API in p_data:
                    montant_final_api = p_data.get(CHAMP_PRIX_FINAL_API)
                    if montant_final_api is not None:
                        montant_a_sauvegarder = montant_final_api
                        logging.debug(f"  -> Utilisé montant payé '{montant_a_sauvegarder}' via clé API '{CHAMP_PRIX_FINAL_API}'.")
                # Fallback sur le prix de base si le prix final n'est pas trouvé/utilisé
                if montant_a_sauvegarder is None:
                    logging.debug("  -> Montant final non trouvé/utilisé. Tentative fallback PRIX DE BASE.")
                    if id_ticket_str and id_ticket_str in all_ticket_prices:
                        prix_base = all_ticket_prices[id_ticket_str]
                        if prix_base is not None:
                            montant_a_sauvegarder = prix_base
                            logging.debug(f"  -> Utilisation PRIX DE BASE '{montant_a_sauvegarder}' pour ticket ID {id_ticket_str}.")
                        else: logging.warning(f"  -> Prix base trouvé pour ticket {id_ticket_str} mais valeur None. Montant sera NULL.")
                    elif id_ticket_str: logging.warning(f"  -> Prix base non trouvé pour ticket {id_ticket_str}. Montant sera NULL.")
                    else: logging.warning(f"  -> id_ticket manquant. Montant sera NULL.")
                # --- Fin Logique Montant Payé ---

                # Sauvegarde en base de données
                save_to_db(
                    nom, prenom, email, telephone, date_naissance_str, adresse, ville, code_postal, event_id,
                    source_info, financement_eligible, rqth,
                    amenagements_necessaires, amenagements_details,
                    montant_a_sauvegarder, # Peut être None
                    nom_billet, code_promo, date_creation_inscription_str
                )
                processed_in_event += 1
                total_participants_processed += 1
            # Fin boucle participants
            logging.info(f"{processed_in_event} participants traités pour l'événement {event_id}.")

        # Gestion des erreurs pour la boucle d'un événement
        except requests.exceptions.Timeout:
            logging.error(f"Erreur Timeout requête participant/list Event {event_id}.")
            logging.warning(f"Skipping event {event_id} due to API timeout.")
            continue # Passe à l'événement suivant
        except requests.exceptions.RequestException as req_err:
            status_code = response.status_code if response is not None else 'N/A'
            response_text = response.text if response is not None else 'N/A'
            logging.error(f"Erreur requête participant/list Event {event_id}: {req_err} (Status: {status_code})")
            logging.debug(f"Détails erreur API participants: Response={response_text[:500]}")
            logging.warning(f"Skipping event {event_id} due to API request error.")
            continue
        except json.JSONDecodeError as e_json:
             response_text = response.text if response is not None else 'N/A'
             logging.error(f"Erreur décodage JSON participant/list Event {event_id}: {e_json}")
             logging.debug(f"Réponse brute non-JSON participants: {response_text[:500]}")
             logging.warning(f"Skipping event {event_id} due to JSON error.")
             continue
        except Exception as general_err:
            logging.error(f"Erreur inattendue majeure durant traitement Event {event_id}: {general_err}", exc_info=True)
            logging.warning(f"Skipping event {event_id} due to unexpected error.")
            continue
    # Fin boucle événements

    logging.info(f"--- Fin Traitement Tous Événements ---")
    logging.info(f"Total participants API (événements actifs/futurs) : {total_participants_api}")
    logging.info(f"Total participants traités (tentatives sauvegarde DB) : {total_participants_processed}")
    logging.info("="*20 + " FIN SYNCHRO PARTICIPANTS " + "="*20)


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - [%(funcName)s] - %(message)s')
    print("*"*10 + " Lancement manuel synchronisation inscriptions Weezevent... " + "*"*10)
    # Décommentez pour lancer la vraie synchro
    # get_registrations()
    print("*"*10 + " Synchronisation terminée (si décommentée). " + "*"*10)

    # --- Section de Test (remplacer par des ID valides) ---
    print("\n" + "="*10 + " DÉBUT TESTS UNITAIRES " + "="*10)
    test_token = get_access_token()
    if test_token and API_KEY:
        print("\n--- TEST get_active_event_ids ---")
        test_ids = get_active_event_ids()
        print(f"IDs événements actifs/futurs trouvés: {test_ids}")

        test_event_ids_for_prices = test_ids[:2] # Prend les 2 premiers ou mettez des IDs spécifiques
        # test_event_ids_for_prices = [123456, 789012] # Ex: Forcer des IDs

        if test_event_ids_for_prices:
            print(f"\n--- TEST get_ticket_prices (pour events: {test_event_ids_for_prices}) ---")
            prices = get_ticket_prices(test_token, test_event_ids_for_prices)
            print("Prix de base:", json.dumps(prices, indent=2))

        print("\n--- TEST get_participant_answers ---")
        test_participant_id = "REMPLACER_PAR_UN_ID_PARTICIPANT_VALIDE" # <== À CHANGER
        if test_participant_id != "REMPLACER_PAR_UN_ID_PARTICIPANT_VALIDE":
            answers = get_participant_answers(test_token, test_participant_id)
            print(f"Réponses formulaire pour participant {test_participant_id}:")
            try: print(json.dumps(answers, indent=2, ensure_ascii=False))
            except TypeError: print(answers)
        else: print("ID Participant test non défini. Test sauté.")

        print("\n--- TEST Récupération p_data complet ---")
        test_event_id_for_pdata = 0 # <== À CHANGER (ID EVENT VALIDE)
        test_participant_id_for_pdata = "REMPLACER_PAR_UN_ID_PARTICIPANT_VALIDE" # <== À CHANGER
        if test_event_id_for_pdata and test_participant_id_for_pdata != "REMPLACER_PAR_UN_ID_PARTICIPANT_VALIDE":
             url_test_pdata = (f"https://api.weezevent.com/participant/list?"
                              f"api_key={API_KEY}&access_token={test_token}&id_event[]={test_event_id_for_pdata}"
                              f"&ids_participant[]={test_participant_id_for_pdata}&full=1")
             print(f"Appel API p_data: {url_test_pdata}")
             try:
                 resp = requests.get(url_test_pdata, timeout=15)
                 resp.raise_for_status()
                 pdata_list = resp.json().get('participants', [])
                 if pdata_list:
                     print(f"--- RAW p_data participant {test_participant_id_for_pdata} (Event {test_event_id_for_pdata}) ---")
                     print(json.dumps(pdata_list[0], indent=2, ensure_ascii=False))
                 else: print(f"Participant/Event non trouvé ou réponse vide.")
             except Exception as e_test:
                 print(f"Erreur test récupération p_data: {e_test}")
                 if 'resp' in locals() and hasattr(resp, 'text'): print(f"Réponse brute: {resp.text[:500]}")
        else: print("ID Event/Participant test non défini. Test p_data sauté.")

    elif not test_token: print("Token non obtenu, tests API annulés.")
    elif not API_KEY: print("API_KEY non défini, tests API annulés.")

    print("="*10 + " FIN DES TESTS UNITAIRES " + "="*10)