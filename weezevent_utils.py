import requests
import os
from dotenv import load_dotenv
import logging
import traceback
import json # Pour décodage JSON et debug

load_dotenv()

# Récupérer les credentials Weezevent une seule fois au démarrage
API_KEY = os.getenv("WEEZEVENT_API_KEY")
USERNAME = os.getenv("WEEZEVENT_USERNAME")
PASSWORD = os.getenv("WEEZEVENT_PASSWORD")

def get_access_token():
    """Récupère un token d'accès depuis l'API Weezevent."""
    url = "https://api.weezevent.com/auth/access_token"
    data = {"username": USERNAME, "password": PASSWORD, "api_key": API_KEY}

    # Vérifier si les credentials sont présents
    if not all([API_KEY, USERNAME, PASSWORD]):
        logging.error("Credentials Weezevent (API_KEY, USERNAME, PASSWORD) manquants dans .env")
        return None

    logging.debug(f"Tentative de récupération du token depuis {url}")
    response = None
    try:
        response = requests.post(url, data=data, timeout=10) # Timeout de 10 secondes
        response.raise_for_status() # Lève une exception pour les codes d'erreur HTTP (4xx/5xx)

        response_data = response.json()
        access_token = response_data.get("accessToken")

        if access_token:
            logging.info("Token d'accès Weezevent récupéré avec succès.")
            return access_token
        else:
            logging.error("Token d'accès Weezevent non trouvé dans la réponse JSON.")
            logging.debug(f"Réponse JSON brute (token): {response_data}")
            return None
    except requests.exceptions.Timeout:
        logging.error("Erreur requête token Weezevent: Timeout.")
        return None
    except requests.exceptions.RequestException as e:
        status_code = response.status_code if response is not None else 'N/A'
        response_text = response.text if response is not None else 'N/A'
        logging.error(f"Erreur requête token Weezevent: {e} (Status: {status_code})")
        logging.debug(f"Détails erreur token: Response Text (max 500 chars) = {response_text[:500]}")
        return None
    except json.JSONDecodeError as e_json:
        # Gérer le cas où la réponse n'est pas du JSON valide
        response_text = response.text if response is not None else 'N/A'
        logging.error(f"Erreur décodage JSON réponse token: {e_json}")
        logging.debug(f"Réponse brute non-JSON (token): {response_text[:500]}")
        return None
    except Exception as e:
         logging.error(f"Erreur non liée à la requête token Weezevent: {e}")
         logging.error(traceback.format_exc()) # Log stack trace pour erreurs inattendues
         return None
