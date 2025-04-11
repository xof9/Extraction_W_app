# -*- coding: utf-8 -*-
import csv
import io
import os
import re
import time
import threading
import traceback
from functools import wraps
from datetime import datetime, date
import decimal # Pour vérifier le type et formater
import mysql.connector
from db_connection import get_connection
from dotenv import load_dotenv
from flask import (Flask, flash, redirect, render_template, request, Response,
                   send_file, session, url_for)
from werkzeug.security import check_password_hash

# --- État de la mise à jour en arrière-plan ---
update_in_progress = False
update_lock = threading.Lock()

# ===== IMPORTER LES FONCTIONS DE SCRIPTS EXTERNES =====
try:
    from weezevent_events import get_events
    from weezevent_api import get_registrations
except ImportError as e:
    print(f"ERREUR: Impossible d'importer les scripts Weezevent - {e}")
    # Fonctions de remplacement si import échoue
    def get_events(): print("Fonction get_events non trouvée!")
    def get_registrations(): print("Fonction get_registrations non trouvée!")

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# --- Config Login ---
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = 300 # secondes

# --- Chargement Utilisateurs ---
USERS = {}
for key, value in os.environ.items():
    if key.endswith("_PASSWORD_HASH"):
        username = key[:-len("_PASSWORD_HASH")].lower()
        if value:
            USERS[username] = value
        else:
            # Avertissement si un hash de mot de passe est vide dans le .env
            print(f"!! ATTENTION : Valeur vide pour {key} !!")
if not USERS:
    # Avertissement si aucun utilisateur n'est configuré
    print("\n" + "="*60); print("!! ATTENTION : Aucun utilisateur trouvé dans .env !!"); print("="*60 + "\n")

# ===== FONCTION POUR MISE À JOUR EN ARRIÈRE-PLAN =====
def run_updates_in_background(flask_app):
    """Exécute get_events et get_registrations dans un thread séparé."""
    global update_in_progress, update_lock
    timestamp_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp_start}] [Thread Background] Démarrage des mises à jour...")

    # Le verrou est acquis par launch_background_update *avant* de lancer ce thread.
    # update_in_progress est déjà à True.

    try:
        # Nécessaire pour accéder aux ressources de l'app (config, db pool, etc.)
        with flask_app.app_context():
            print(f"[{timestamp_start}] [Thread Background] Exécution get_events()...")
            get_events()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Thread Background] Exécution get_registrations()...")
            get_registrations() # Mise à jour des données principales
        timestamp_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp_end}] [Thread Background] Mises à jour terminées.")

    except Exception as e:
        timestamp_err = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp_err}] [Thread Background] ERREUR lors des mises à jour : {e}")
        print(traceback.format_exc())

    finally:
        # Assurer que le drapeau est remis à False, même en cas d'erreur
        with update_lock:
            update_in_progress = False
        timestamp_final = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp_final}] [Thread Background] Fin du thread (drapeau remis à False).")

# --- Décorateurs & Routes ---

def login_required(f):
    """Décorateur pour exiger une connexion utilisateur."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            flash("Connexion requise.", "warning")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

@app.route('/lancer-mise-a-jour', methods=['POST'])
@login_required
def launch_background_update():
    """Lance le processus de mise à jour des données en arrière-plan."""
    global update_in_progress, update_lock
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] Requête reçue pour /lancer-mise-a-jour")

    # Verrou pour vérifier et modifier update_in_progress de manière atomique
    with update_lock:
        if update_in_progress:
            print(f"[{timestamp}] Mise à jour déjà en cours. Redirection vers maintenance.")
            flash("Une mise à jour des données est déjà en cours.", "warning")
            return redirect(url_for('show_maintenance'))
        else:
            print(f"[{timestamp}] Lancement du thread de mise à jour...")
            # Mettre à True *avant* de démarrer le thread pour éviter une race condition
            update_in_progress = True
            # Passer l'objet application Flask courant au thread
            update_thread = threading.Thread(target=run_updates_in_background, args=(app,))
            # Le thread n'empêchera pas l'application de s'arrêter si elle est tuée
            update_thread.daemon = True
            update_thread.start()
            print(f"[{timestamp}] Thread démarré. Redirection vers maintenance.")
            return redirect(url_for('show_maintenance'))

@app.route('/en-cours-de-mise-a-jour')
@login_required
def show_maintenance():
    """Affiche une page pendant la mise à jour des données."""
    global update_in_progress, update_lock
    # Lire l'état sous verrou pour garantir la cohérence
    with update_lock:
        in_progress = update_in_progress

    if in_progress:
        # Rafraîchit la page toutes les 10 secondes pour vérifier si la màj est finie
        headers = {'Refresh': '10'}
        return render_template('maintenance.html'), 200, headers
    else:
        flash("La mise à jour des données est terminée.", "info")
        # Redirige vers la page principale une fois terminé
        return redirect(url_for('select_event'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Gère la connexion des utilisateurs."""
    if 'logged_in' in session:
        return redirect(url_for('index'))

    error = None
    locked_out = False
    remaining_time = 0
    remaining_attempts = MAX_LOGIN_ATTEMPTS - session.get('login_attempts', 0)

    # Vérifie si l'utilisateur est actuellement bloqué
    lockout_until = session.get('lockout_until', 0)
    current_time = time.time()
    if current_time < lockout_until:
        locked_out = True
        remaining_time = int(lockout_until - current_time)
        remaining_attempts = 0
        # Si POST pendant le blocage, juste réafficher la page de blocage
        if request.method == 'POST':
             return render_template('login.html', error=None, locked_out=True, remaining_time=remaining_time, remaining_attempts=0)
    elif 'lockout_until' in session:
        # Si le temps de blocage est passé, réinitialiser
         session.pop('lockout_until', None)
         session.pop('login_attempts', None)
         remaining_attempts = MAX_LOGIN_ATTEMPTS

    if request.method == 'POST' and not locked_out:
        username_form = request.form.get('username', '').lower().strip()
        password_form = request.form.get('password', '')

        if not username_form or not password_form:
            error = "Nom d'utilisateur et mot de passe requis."
        elif username_form in USERS:
            expected_hash = USERS[username_form]
            # Vérifie le mot de passe hashé
            if check_password_hash(expected_hash, password_form):
                session.clear() # Réinitialise la session avant connexion
                session['logged_in'] = True
                session['username'] = username_form
                session.permanent = False # Session basée sur le navigateur
                flash(f"Connexion réussie ! Bienvenue {username_form.capitalize()}.", "success")
                next_url = request.args.get('next') # Redirige vers la page demandée initialement
                return redirect(next_url or url_for('index'))
            else:
                # Mot de passe incorrect
                error = "Nom d'utilisateur ou mot de passe invalide."
                session['login_attempts'] = session.get('login_attempts', 0) + 1
        else:
            # Utilisateur inconnu
            error = "Nom d'utilisateur ou mot de passe invalide."
            session['login_attempts'] = session.get('login_attempts', 0) + 1

        # Vérifie si le nombre max de tentatives est atteint
        current_attempts = session.get('login_attempts', 0)
        if error and current_attempts >= MAX_LOGIN_ATTEMPTS:
            session['lockout_until'] = time.time() + LOCKOUT_DURATION
            locked_out = True
            remaining_time = LOCKOUT_DURATION
            remaining_attempts = 0
            error = f"Trop de tentatives. Compte verrouillé pour {LOCKOUT_DURATION // 60} minutes."
            flash(error, "danger")
        elif error:
            flash(error, "danger")

    if not locked_out:
        remaining_attempts = MAX_LOGIN_ATTEMPTS - session.get('login_attempts', 0)
        remaining_attempts = max(0, remaining_attempts) # Assure que ce n'est pas négatif

    return render_template('login.html',
                           error=error,
                           locked_out=locked_out,
                           remaining_time=remaining_time,
                           remaining_attempts=remaining_attempts)

@app.route('/logout')
@login_required
def logout():
    """Déconnecte l'utilisateur."""
    session.clear()
    flash("Vous avez été déconnecté.", "info")
    return redirect(url_for('login'))

@app.route("/")
@login_required
def index():
    """Page d'accueil, redirige vers la sélection d'événement."""
    return redirect(url_for('select_event'))

@app.route("/select_event", methods=["GET", "POST"])
@login_required
def select_event():
    """Affiche la liste des événements et les participants de l'événement sélectionné."""
    conn = None
    cursor = None
    participants_processed = []
    selected_event_id_int = None
    processed_events = []
    placeholder_missing_info = "Non renseigné" # Valeur par défaut pour les infos manquantes

    try:
        conn = get_connection()
        if not conn:
            raise ConnectionError("Connexion DB impossible depuis le pool.")
        cursor = conn.cursor(dictionary=True)

        # --- Gestion de l'ID événement sélectionné ---
        selected_event_id_str = session.get("selected_event_id")
        if request.method == "POST":
            event_id = request.form.get("event_id")
            if event_id and event_id.isdigit():
                selected_event_id_int = int(event_id)
                session["selected_event_id"] = str(selected_event_id_int) # Stocke en string dans la session
            else:
                 session.pop("selected_event_id", None)
                 selected_event_id_int = None
                 if event_id: # Avertir seulement si une valeur invalide a été fournie
                     flash(f"ID événement invalide fourni : '{event_id}'.", "warning")
        elif selected_event_id_str: # Si GET et un ID est en session
             try:
                 selected_event_id_int = int(selected_event_id_str)
             except (ValueError, TypeError):
                 flash(f"ID événement invalide trouvé en session ('{selected_event_id_str}'). Sélection effacée.", "warning")
                 session.pop("selected_event_id", None)
                 selected_event_id_int = None

        # --- Récupération des Événements depuis la DB ---
        # On récupère tous les événements actifs, sans filtre de date ici
        # Le tri fait apparaître les plus récents (ou sans date) en premier
        sql_events = """
            SELECT * FROM evenements
            WHERE actif = 1
            ORDER BY date DESC, nom ASC
        """
        cursor.execute(sql_events)
        events_from_db = cursor.fetchall()
        app.logger.info(f"{len(events_from_db)} événements actifs récupérés.")

        # --- Traitement des événements pour affichage ---
        for event_db in events_from_db:
            event_processed = event_db.copy()
            event_date = event_db.get('date')
            formatted_event_date = placeholder_missing_info
            # Formate la date de l'événement pour l'affichage
            try:
                if isinstance(event_date, (date, datetime)):
                    formatted_event_date = event_date.strftime('%d/%m/%Y')
                elif isinstance(event_date, str) and event_date:
                     parsed_event_date = datetime.strptime(event_date, '%Y-%m-%d').date()
                     formatted_event_date = parsed_event_date.strftime('%d/%m/%Y')
            except (ValueError, TypeError): pass # Garde le placeholder si erreur formatage
            event_processed['date_display'] = formatted_event_date
            # Assure un placeholder si le nom est vide
            event_processed['nom'] = event_db.get('nom') or placeholder_missing_info
            processed_events.append(event_processed)

        # --- Récupération et Traitement des Participants si un événement est sélectionné ---
        if selected_event_id_int is not None:
            # Vérification si l'event sélectionné est bien dans la liste affichable
            if not any(evt['event_id'] == selected_event_id_int for evt in processed_events):
                 app.logger.warning(f"Tentative d'affichage participants pour event {selected_event_id_int} non autorisé (inactif?).")
                 flash("L'événement sélectionné n'est plus disponible.", "warning")
                 session.pop("selected_event_id", None) # Efface l'ID invalide de la session
                 selected_event_id_int = None
            else:
                app.logger.debug(f"Récupération participants pour event: {selected_event_id_int}")
                try:
                    # Récupère les participants de l'événement sélectionné
                    cursor.execute("SELECT * FROM inscriptions WHERE event_id=%s ORDER BY nom ASC, prenom ASC", (selected_event_id_int,))
                    participants_db = cursor.fetchall()
                    app.logger.info(f"{len(participants_db)} participants récupérés pour event {selected_event_id_int}.")

                    for p_db in participants_db:
                        p_processed = p_db.copy()

                        # Appliquer le placeholder à tous les champs potentiellement vides
                        p_processed["nom"] = p_db.get("nom") or placeholder_missing_info
                        p_processed["prenom"] = p_db.get("prenom") or placeholder_missing_info
                        p_processed["email"] = p_db.get("email") or placeholder_missing_info
                        p_processed["telephone"] = p_db.get("telephone") or placeholder_missing_info
                        p_processed["adresse"] = p_db.get("adresse") or placeholder_missing_info
                        p_processed["code_postal"] = p_db.get("code_postal") or placeholder_missing_info
                        p_processed["ville"] = p_db.get("ville") or placeholder_missing_info
                        p_processed["source_info"] = p_db.get("source_info") or placeholder_missing_info
                        p_processed["financement_eligible"] = p_db.get("financement_eligible") or placeholder_missing_info
                        p_processed["rqth"] = p_db.get("rqth") or placeholder_missing_info
                        p_processed["amenagements_necessaires"] = p_db.get("amenagements_necessaires") or placeholder_missing_info
                        # Placeholder pour détails seulement si aménagements = Oui et détails vide
                        needs_details = p_processed["amenagements_necessaires"] == "Oui"
                        p_processed["amenagements_details"] = p_db.get("amenagements_details") or (placeholder_missing_info if needs_details else "")
                        p_processed["nom_billet"] = p_db.get("nom_billet") or placeholder_missing_info

                        # Formatage Date naissance pour affichage
                        date_naissance_db = p_db.get("date_naissance")
                        formatted_naissance = placeholder_missing_info
                        try:
                            if isinstance(date_naissance_db, date):
                                formatted_naissance = date_naissance_db.strftime('%d/%m/%Y')
                            elif isinstance(date_naissance_db, str) and date_naissance_db:
                                formatted_naissance = datetime.strptime(date_naissance_db, '%Y-%m-%d').strftime('%d/%m/%Y')
                        except (ValueError, TypeError): pass
                        p_processed["date_naissance_display"] = formatted_naissance

                        # Formatage Date création pour affichage
                        date_creation_db = p_db.get("date_creation_inscription")
                        formatted_creation = placeholder_missing_info
                        try:
                           if isinstance(date_creation_db, datetime):
                               formatted_creation = date_creation_db.strftime('%d/%m/%Y %H:%M')
                           elif isinstance(date_creation_db, str) and date_creation_db:
                               parsed_creation = None
                               # Gère plusieurs formats potentiels de date/heure
                               for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                                   try: parsed_creation = datetime.strptime(date_creation_db, fmt); break
                                   except ValueError: continue
                               if parsed_creation: formatted_creation = parsed_creation.strftime('%d/%m/%Y %H:%M')
                        except (ValueError, TypeError): pass
                        p_processed["date_creation_display"] = formatted_creation

                        # Formatage Montant payé (transmis comme string au template)
                        montant_db = p_db.get("montant_paye")
                        formatted_montant = placeholder_missing_info
                        try:
                            if isinstance(montant_db, decimal.Decimal):
                                formatted_montant = str(montant_db) # Le formatage final (virgule) sera fait dans le template si besoin
                            elif montant_db is not None: # Tente une conversion si ce n'est pas None
                                formatted_montant = str(decimal.Decimal(montant_db))
                        except (TypeError, decimal.InvalidOperation): pass
                        p_processed["montant_paye_display"] = formatted_montant

                        # Affichage simple Oui/Non pour code promo
                        code_promo_db = p_db.get("code_promo")
                        p_processed["code_promo_display"] = "Oui" if code_promo_db else "Non"

                        participants_processed.append(p_processed)

                except Exception as e_part:
                     app.logger.error(f"Erreur récupération/traitement participants event {selected_event_id_int}: {e_part}", exc_info=True)
                     flash("Erreur lors du chargement des participants.", "danger")
                     participants_processed = [] # Vide la liste en cas d'erreur

    except mysql.connector.Error as db_err:
        app.logger.error(f"Erreur Base de Données dans select_event : {db_err}", exc_info=True)
        flash("Erreur de connexion ou de requête à la base de données.", "danger")
        # Réinitialise les listes et la sélection en cas d'erreur DB
        processed_events, participants_processed = [], []
        selected_event_id_int = None
    except Exception as e:
        app.logger.error(f"Erreur générale dans select_event : {e}", exc_info=True)
        flash("Une erreur inattendue est survenue lors du chargement de la page.", "danger")
        processed_events, participants_processed = [], []
        selected_event_id_int = None

    finally:
        # Assure la fermeture de la connexion DB
        if cursor: cursor.close()
        if conn: conn.close()
        app.logger.debug("Connexion BDD fermée/remise au pool pour select_event.")

    username = session.get('username', '')
    return render_template("select_event.html",
                           events=processed_events,
                           participants=participants_processed,
                           selected_event_id=selected_event_id_int,
                           username=username.capitalize())

@app.route("/export_participants")
@login_required
def export_participants():
    """Exporte les participants de l'événement sélectionné en fichier CSV."""
    selected_event_id_str = session.get("selected_event_id")
    if not selected_event_id_str:
        flash("Aucun événement sélectionné pour l'export.", "warning")
        return redirect(url_for('select_event'))

    conn = None
    cursor = None
    event_name = "evenement_inconnu"
    placeholder_missing_info = "Non renseigné" # Valeur par défaut pour les infos manquantes

    try:
        # Valide l'ID de l'événement de la session
        try: selected_event_id_int = int(selected_event_id_str)
        except (ValueError, TypeError):
             flash("ID événement invalide pour l'export.", "danger")
             return redirect(url_for('select_event'))

        conn = get_connection()
        if not conn:
             app.logger.error("Export impossible: Connexion DB échouée depuis le pool.")
             flash("Erreur de connexion à la base de données pour l'export.", "danger")
             return redirect(url_for('select_event'))
        cursor = conn.cursor(dictionary=True)

        # Récupère le nom de l'événement pour le nom du fichier
        try:
            cursor.execute("SELECT nom FROM evenements WHERE event_id = %s", (selected_event_id_int,))
            event_data = cursor.fetchone()
            # Utilise un nom par défaut si non trouvé ou vide
            event_name = (event_data['nom'] if event_data and event_data['nom'] else f"event_{selected_event_id_int}")
        except Exception as e_event_name:
            app.logger.warning(f"Récup nom event {selected_event_id_int} échouée pour export: {e_event_name}")
            event_name = f"event_{selected_event_id_int}" # Fallback

        # Nettoie le nom pour l'utiliser dans le nom de fichier
        safe_name = re.sub(r'[^\w\-]+', '', event_name.replace(' ', '_')) # Remplace espaces et caractères non alphanumériques
        safe_name = re.sub(r'[_]+', '_', safe_name).strip('_')[:60] # Limite la longueur et évite les underscores multiples/extrêmes
        safe_name = safe_name or "evenement" # Nom de secours si le nettoyage a tout enlevé
        download_filename = f"participants_{safe_name}.csv"

        # Récupère les participants pour l'export
        cursor.execute("SELECT * FROM inscriptions WHERE event_id=%s ORDER BY nom ASC, prenom ASC", (selected_event_id_int,))
        participants = cursor.fetchall()

        if not participants:
            flash("Aucun participant à exporter pour cet événement.", "info")
            return redirect(url_for('select_event'))

        # Utilise io.StringIO pour créer le CSV en mémoire
        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)

        # Écrit l'en-tête du CSV
        writer.writerow([
            "Nom", "Prénom", "Email", "Téléphone", "Date Naissance",
            "Adresse", "Ville", "Code Postal",
            "Date Inscription", "Heure Inscription",
            "Connu la formation", "Éligible Financement", "RQTH",
            "Besoin Aménagements", "Détails Aménagements",
            "Montant Payé",
            "Type Billet", "Code Promo Utilisé ?"
        ])

        # Écrit chaque ligne de participant dans le CSV
        for p in participants:
            # Utilisation cohérente du placeholder pour toutes les données textuelles
            nom = p.get("nom") or placeholder_missing_info
            prenom = p.get("prenom") or placeholder_missing_info
            email = p.get("email") or placeholder_missing_info
            telephone = p.get("telephone") or placeholder_missing_info
            adresse = p.get("adresse") or placeholder_missing_info
            ville = p.get("ville") or placeholder_missing_info
            code_postal = p.get("code_postal") or placeholder_missing_info
            source_info = p.get("source_info") or placeholder_missing_info
            financement = p.get("financement_eligible") or placeholder_missing_info
            rqth = p.get("rqth") or placeholder_missing_info
            amenagements = p.get("amenagements_necessaires") or placeholder_missing_info
            # Placeholder pour détails seulement si aménagements = Oui et détails vide
            needs_details = amenagements == "Oui"
            amenagements_details = p.get("amenagements_details") or (placeholder_missing_info if needs_details else "")
            nom_billet = p.get("nom_billet") or placeholder_missing_info

            # Formatage Date naissance pour l'export
            date_naissance_str = placeholder_missing_info
            date_naissance_obj = p.get("date_naissance")
            try:
                if isinstance(date_naissance_obj, date):
                    date_naissance_str = date_naissance_obj.strftime('%d/%m/%Y')
                elif isinstance(date_naissance_obj, str) and date_naissance_obj:
                    date_naissance_str = datetime.strptime(date_naissance_obj, '%Y-%m-%d').strftime('%d/%m/%Y')
            except ValueError: pass # Garde placeholder si format invalide

            # Formatage Date/Heure création pour l'export (colonnes séparées)
            date_creation_str = placeholder_missing_info
            heure_creation_str = ""
            date_creation_obj = p.get("date_creation_inscription")
            try:
                parsed_creation = None
                if isinstance(date_creation_obj, datetime):
                    parsed_creation = date_creation_obj
                elif isinstance(date_creation_obj, str) and date_creation_obj:
                     # Gère plusieurs formats potentiels de date/heure
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                        try: parsed_creation = datetime.strptime(date_creation_obj, fmt); break
                        except ValueError: continue
                if parsed_creation:
                    date_creation_str = parsed_creation.strftime('%d/%m/%Y')
                    heure_creation_str = parsed_creation.strftime('%H:%M:%S')
            except (ValueError, TypeError): pass # Garde placeholder si format invalide

            # Formatage Montant Payé pour l'export (avec virgule comme séparateur décimal)
            montant_paye_str = placeholder_missing_info
            montant_paye = p.get("montant_paye")
            # Ajout try/except pour robustesse si la donnée n'est pas un Decimal valide
            try:
                if isinstance(montant_paye, decimal.Decimal):
                    montant_paye_str = "{:.2f}".format(montant_paye).replace('.', ',')
                elif montant_paye is not None: # Tente la conversion
                    montant_paye_str = "{:.2f}".format(decimal.Decimal(montant_paye)).replace('.', ',')
            except (TypeError, decimal.InvalidOperation): pass # Reste placeholder si conversion échoue

            # Formatage Code Promo pour l'export (Oui/Non)
            code_promo_db = p.get("code_promo")
            code_promo_export = "Oui" if code_promo_db else "Non"

            # Écriture de la ligne dans le fichier CSV
            writer.writerow([
                nom, prenom, email, telephone, date_naissance_str,
                adresse, ville, code_postal,
                date_creation_str, heure_creation_str,
                source_info, financement, rqth,
                amenagements, amenagements_details,
                montant_paye_str, nom_billet,
                code_promo_export
            ])

        # Préparation de la réponse pour le téléchargement du fichier
        # Utilisation de utf-8-sig pour assurer la compatibilité Excel avec les caractères spéciaux
        csv_data_bytes = output.getvalue().encode('utf-8-sig')
        buffer = io.BytesIO(csv_data_bytes)
        buffer.seek(0) # Rembobine le buffer

        return send_file(
            buffer,
            mimetype='text/csv; charset=utf-8-sig',
            as_attachment=True, # Force le téléchargement
            download_name=download_filename # Nom du fichier téléchargé
        )

    except mysql.connector.Error as db_err:
         app.logger.error(f"Erreur DB Export participants: {db_err}", exc_info=True)
         flash("Erreur de base de données lors de la préparation de l'export.", "danger")
         return redirect(url_for('select_event'))
    except KeyError as e_key:
         # Erreur spécifique si une colonne attendue est manquante dans les données DB
         col_manquante = str(e_key).strip("'");
         app.logger.error(f"ERREUR Export: Clé manquante '{col_manquante}'.", exc_info=True)
         flash(f"Erreur export : Donnée manquante ('{col_manquante}'). Vérifiez la structure de la table 'inscriptions'.", "danger")
         return redirect(url_for('select_event'))
    except Exception as e:
         # Erreur générique
         app.logger.error(f"Erreur inattendue Export participants: {e}", exc_info=True)
         flash(f"Erreur inattendue lors de la génération de l'export : {e}", "danger")
         return redirect(url_for('select_event'))
    finally:
        # Assure la fermeture de la connexion DB
        if cursor: cursor.close()
        if conn: conn.close()
        app.logger.debug("Connexion BDD fermée/remise au pool pour export_participants.")

# --- Exécution de l'application Flask ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    # Le reloader de Flask peut causer des problèmes avec le threading (lancer 2 fois)
    # Il est désactivé par défaut, mais configurable via variable d'environnement
    use_reloader = os.environ.get("FLASK_USE_RELOADER", "false").lower() == "true"

    print(f"Démarrage Flask sur http://0.0.0.0:{port}")
    print(f"Mode Debug: {debug_mode}, Reloader: {use_reloader}")
    if use_reloader and debug_mode:
        print("ATTENTION: Reloader activé en mode debug, peut interférer avec les threads d'arrière-plan.")
    app.run(debug=debug_mode, host='0.0.0.0', port=port, use_reloader=use_reloader)