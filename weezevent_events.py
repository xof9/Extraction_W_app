import requests
from db_connection import get_connection # Utilise le pool de connexions
import os
from dotenv import load_dotenv
import logging
import mysql.connector

try:
    # Fonction pour obtenir le token d'accès Weezevent
    from weezevent_utils import get_access_token
except ImportError:
    logging.error("Impossible d'importer get_access_token depuis weezevent_utils.")
    # Fonction factice pour éviter les erreurs, mais le script ne fonctionnera pas
    def get_access_token():
        logging.error("Fonction get_access_token non disponible.")
        return None

load_dotenv()

def save_event_to_db(event_id, nom, start_date_str, is_active):
    """
    Enregistre ou met à jour un événement dans la BDD.
    'actif' (booléen) indique si l'événement n'est PAS annulé selon Weezevent.
    'start_date_str' est la date de début brute (chaîne API).
    """
    conn = None
    cursor = None
    try:
        conn = get_connection() # Obtient une connexion du pool
        if not conn:
            logging.error("Impossible d'obtenir une connexion DB pour save_event_to_db.")
            return
        cursor = conn.cursor()
        actif_db_value = 1 if is_active else 0 # Convertit booléen en entier pour la DB

        # Tente de parser la date (partie YYYY-MM-DD) pour la BDD
        event_date_db = None
        if start_date_str:
            try:
                date_part = start_date_str.split(' ')[0].split('T')[0]
                event_date_db = date_part # Stocker comme chaîne YYYY-MM-DD
            except Exception:
                 logging.warning(f"Impossible de parser la date '{start_date_str}' pour l'événement {event_id}. Sera NULL en BDD.")
                 # event_date_db reste None

        # ON DUPLICATE KEY UPDATE pour insérer ou mettre à jour
        sql = """
            INSERT INTO evenements (event_id, nom, date, actif)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                nom = VALUES(nom),
                date = VALUES(date),
                actif = VALUES(actif)
            """
        params = (event_id, nom, event_date_db, actif_db_value)

        cursor.execute(sql, params)
        conn.commit()
        logging.info(f"Événement {event_id} ('{nom}') sauvegardé/MAJ. Date: {event_date_db}, Actif (non-annulé): {is_active}")

    except Exception as e:
        logging.error(f"Erreur DB lors de la sauvegarde de l'événement {event_id}: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor: cursor.close()
        # Remettre la connexion dans le pool
        if conn: conn.close()

def get_events():
    """
    Récupère les événements depuis l'API Weezevent.
    Marque comme 'actif = 1' dans la BDD les événements non annulés.
    Enregistre la date de début de l'événement.
    Le filtrage par date pour l'affichage/synchronisation se fait ailleurs (lecture BDD).
    """
    logging.info("Début de la récupération des événements Weezevent...")
    access_token = get_access_token()
    if not access_token:
        logging.error("Impossible de récupérer les événements sans token d'accès.")
        return

    
    CANCELED_STATUS_ID = 4 
    
    API_KEY = os.getenv("WEEZEVENT_API_KEY")
    if not API_KEY:
        logging.error("WEEZEVENT_API_KEY non trouvé dans les variables d'environnement.")
        return

    url = f"https://api.weezevent.com/events?api_key={API_KEY}&access_token={access_token}"
    # Ajoutez d'autres paramètres si nécessaire (ex: include_closed=true)
    logging.info(f"Appel API événements : {url}")

    try:
        response = requests.get(url, timeout=20) # Timeout pour la requête
        response.raise_for_status() # Gère les erreurs HTTP 4xx/5xx

        events_data = response.json()
        events_list = events_data.get("events")

        if events_list:
            logging.info(f"{len(events_list)} événements reçus de l'API.")
            processed_count = 0
            for event in events_list:
                event_id = event.get("id")
                name = event.get("name", "Nom Indisponible")
                start_date_str = event.get("date", {}).get("start") # Date de début (peut être None)

                sales_status = event.get("sales_status", {})
                status_id = sales_status.get("id_status")
                status_label = sales_status.get("libelle_status", "Statut Inconnu")

                # Un événement est actif pour la BDD s'il n'est PAS annulé
                is_active = True
                if status_id == CANCELED_STATUS_ID:
                    is_active = False # Marqué comme inactif si annulé

                logging.debug(f"Event ID {event_id} ('{name}') - Statut API: '{status_label}' (ID: {status_id}) -> Actif BDD (non-annulé): {is_active}")

                if event_id:
                    # Sauvegarde avec date de début et le flag 'actif' (non-annulé)
                    save_event_to_db(event_id, name, start_date_str, is_active)
                    processed_count += 1
                else:
                     logging.warning(f"Événement API sans ID trouvé, ignoré : {name}")

            logging.info(f"{processed_count} événements traités et sauvegardés/mis à jour.")

        elif events_list is not None: # Clé "events" existe mais vide
             logging.info("Aucun événement retourné par l'API Weezevent.")
        else: # Clé "events" absente
            logging.warning("La clé 'events' est manquante dans la réponse API.")
            logging.debug(f"Réponse API reçue : {events_data}")

    except requests.exceptions.Timeout:
        logging.error(f"Erreur Timeout lors de la requête API pour récupérer les événements.")
    except requests.exceptions.RequestException as e:
         logging.error(f"Erreur requête API pour récupérer les événements : {e}")
    except Exception as e:
         logging.error(f"Erreur inattendue lors de la récupération/traitement des événements : {e}", exc_info=True)

    logging.info("Fin de la récupération des événements.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    print("Lancement manuel de la récupération des événements Weezevent...")
    get_events()
    print("Récupération des événements terminée.")