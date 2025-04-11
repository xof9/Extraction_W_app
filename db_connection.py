import mysql.connector
from mysql.connector import pooling
import os
from dotenv import load_dotenv
import logging # Pour logger les erreurs du pool

load_dotenv()

# Configuration du pool de connexions
cnx_pool = None
try:
    db_config = {
        "host": os.getenv("DB_HOST"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "database": os.getenv("DB_NAME"),
        "port": int(os.getenv("DB_PORT", 3306)), # Port DB, défaut 3306
        "auth_plugin": 'mysql_native_password' # Ou l'auth plugin nécessaire
    }

    # Validation des paramètres essentiels
    required_keys = ["host", "user", "password", "database"]
    if any(not db_config.get(key) for key in required_keys):
        raise ValueError("Variables d'environnement BDD manquantes (HOST, USER, PASSWORD, DATABASE).")

    # Création du pool de connexions (une seule fois au démarrage)
    print(f"Initialisation du pool de connexions MySQL vers {db_config['host']}:{db_config['port']}...")
    cnx_pool = pooling.MySQLConnectionPool(
        pool_name = "flask_weezevent_pool",
        pool_size = 5, # Nombre de connexions maintenues ouvertes
        pool_reset_session=True, # Recommandé pour réinitialiser l'état de la session entre les utilisations
        **db_config # Passe les autres configs (host, user, etc.)
    )
    print("Pool de connexions initialisé.")

except ValueError as e:
    logging.error(f"Erreur de configuration DB: {e}")
    # Le pool reste à None
except mysql.connector.Error as err:
    logging.error(f"Erreur lors de l'initialisation du pool de connexions MySQL: {err}")
    # Le pool reste à None
except Exception as e:
    logging.error(f"Erreur inattendue lors de la configuration du pool DB: {e}")
    # Le pool reste à None

def get_connection():
    """ Obtient une connexion depuis le pool. """
    if cnx_pool is None:
        logging.error("Tentative d'obtenir une connexion alors que le pool n'est pas initialisé.")
        raise ConnectionError("Le pool de connexions à la base de données n'a pas pu être initialisé.")

    try:
        conn = cnx_pool.get_connection()
        return conn
    except mysql.connector.Error as err:
        logging.error(f"Erreur pour obtenir une connexion du pool: {err}")
        raise ConnectionError(f"Impossible d'obtenir une connexion du pool: {err}")
    except Exception as e:
         logging.error(f"Erreur inattendue lors de l'obtention d'une connexion du pool: {e}")
         raise ConnectionError(f"Erreur inattendue pour obtenir une connexion du pool: {e}")