import os
import smtplib
import traceback 
from email.mime.text import MIMEText
from dotenv import load_dotenv
from db_connection import get_connection 
import mysql.connector 
load_dotenv()

DB_NAME = os.getenv('DB_NAME') 
DB_SIZE_LIMIT_MB = 100.0 
THRESHOLD_PERCENT = 85.0

# Configuration pour l'envoi d'email d'alerte
NOTIFY_EMAIL_TO = os.getenv('NOTIFY_EMAIL_TO')
SMTP_SERVER = os.getenv('SMTP_SERVER')
SMTP_PORT = int(os.getenv('SMTP_PORT', 587))
SMTP_LOGIN = os.getenv('SMTP_LOGIN')
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD')


# ===== Fonction principale de vérification de la taille BDD =====
def check_database_size():
    """Interroge la BDD pour sa taille et la compare au seuil configuré."""
    conn = None
    cursor = None
    current_size_mb = 0.0
    threshold_mb = DB_SIZE_LIMIT_MB * (THRESHOLD_PERCENT / 100.0)

    if not DB_NAME:
        print(f"ERREUR: La variable d'environnement 'DB_NAME' n'est pas définie.")
        return

    print("INFO: Début vérification taille BDD...")
    try:
        conn = get_connection()
        if conn is None:
            print("ERREUR: Impossible d'établir la connexion via get_connection.")
            return

        cursor = conn.cursor(dictionary=True)

        # Requête SQL pour obtenir la taille de la base de données
        query = """
            SELECT table_schema AS database_name,
                   SUM(data_length + index_length) / 1024 / 1024 AS size_in_mb
            FROM information_schema.TABLES
            WHERE table_schema = %s
            GROUP BY table_schema;
        """
        cursor.execute(query, (DB_NAME,))
        result = cursor.fetchone()

        if result:
            current_size_mb = result['size_in_mb']
            print(f"INFO: Taille actuelle BDD '{DB_NAME}': {current_size_mb:.2f} Mo.")
        else:
            print(f"AVERTISSEMENT: Impossible de récupérer la taille pour '{DB_NAME}'. Vérifiez le nom.")
            return

        # Comparaison avec le seuil et déclenchement de la notification si nécessaire
        if current_size_mb >= threshold_mb:
            print(f"ALERTE: Seuil dépassé! Taille={current_size_mb:.2f} Mo, Seuil={threshold_mb:.2f} Mo ({THRESHOLD_PERCENT}%).")
            send_notification(current_size_mb, threshold_mb)
        else:
            print(f"INFO: Taille BDD en dessous du seuil ({threshold_mb:.2f} Mo).")

    except mysql.connector.Error as db_err:
        print(f"ERREUR MySQL lors de la vérification: {db_err}")
        print(traceback.format_exc()) 
    except Exception as e:
        print(f"ERREUR Générale lors de la vérification: {e}")
        print(traceback.format_exc()) 
    finally:
       
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()
        print("INFO: Vérification taille BDD terminée.")


# ===== Fonction pour envoyer l'email de notification =====
def send_notification(current_size, threshold_size):
    """Formate et envoie un email d'alerte via SMTP."""
    # Vérification de la présence des configurations SMTP essentielles
    if not all([NOTIFY_EMAIL_TO, SMTP_SERVER, SMTP_LOGIN, SMTP_PASSWORD]):
        print("ERREUR: Configuration SMTP incomplète dans .env. Notification non envoyée.")
        return

    # Construction de l'email
    subject = f"ALERTE Espace Base de Données - {DB_NAME}"
    body = f"""
    Attention,

    L'espace utilisé par la base de données '{DB_NAME}' ({current_size:.2f} Mo)
    a dépassé le seuil d'alerte de {threshold_size:.2f} Mo ({THRESHOLD_PERCENT}%).

    Limite totale (plan gratuit): {DB_SIZE_LIMIT_MB:.2f} Mo.

    Veuillez vérifier et envisager un nettoyage pour éviter des problèmes.
    """
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = SMTP_LOGIN
    msg['To'] = NOTIFY_EMAIL_TO

    # Envoi de l'email
    try:
        print(f"INFO: Tentative d'envoi de l'email d'alerte à {NOTIFY_EMAIL_TO}...")
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls() 
            server.login(SMTP_LOGIN, SMTP_PASSWORD) 
            server.sendmail(SMTP_LOGIN, [NOTIFY_EMAIL_TO], msg.as_string())
        print("INFO: Email d'alerte envoyé avec succès.")
    except smtplib.SMTPAuthenticationError as auth_err:
         print(f"ERREUR SMTP Authentification: {auth_err}. Vérifiez login/mot de passe (ou mot de passe d'application si 2FA).")
    except Exception as e:
        print(f"ERREUR Inconnue lors de l'envoi de l'email: {e}")
        print(traceback.format_exc()) 


# ===== Bloc de tesst direct =====
if __name__ == "__main__":
    print("--- Exécution directe de monitoring.py ---")
    check_database_size()
    print("--- Fin de l'exécution directe ---")