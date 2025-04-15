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
import decimal
import mysql.connector
from db_connection import get_connection # Utilise votre fichier de connexion
from dotenv import load_dotenv
from flask import (Flask, flash, redirect, render_template, request, Response,
                   send_file, session, url_for, abort) # abort ajouté
from werkzeug.security import check_password_hash

load_dotenv()

# --- État global pour la mise à jour en arrière-plan ---
update_in_progress = False
update_lock = threading.Lock()

# ===== Import des fonctions externes (Weezevent et Surveillance BDD) =====
try:
    from weezevent_events import get_events
    from weezevent_api import get_registrations
except ImportError as e:
    print(f"ERREUR: Import Weezevent échoué - {e}")
    def get_events(): print("Fonction get_events non trouvée!")
    def get_registrations(): print("Fonction get_registrations non trouvée!")

try:
    from monitoring import check_database_size # Fonction pour vérifier la taille de la BDD
except ImportError as e:
    print(f"AVERTISSEMENT: Import check_database_size échoué - {e}")
    def check_database_size():
        print("ERREUR: Fonction check_database_size non importée.")

# --- Initialisation de l'application Flask ---
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

# --- Configuration générale ---
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_DURATION = 300
CRON_SECRET_KEY = os.getenv('CRON_SECRET_KEY', 'change-this-in-production') # Pour l'endpoint de surveillance

# --- Chargement des utilisateurs depuis les variables d'environnement ---
USERS = {}
for key, value in os.environ.items():
    if key.endswith("_PASSWORD_HASH"):
        username = key[:-len("_PASSWORD_HASH")].lower()
        if value:
            USERS[username] = value
        else:
            print(f"!! ATTENTION : Valeur vide pour {key} !!")
if not USERS:
    print("\n" + "="*60); print("!! ATTENTION : Aucun utilisateur trouvé dans .env !!"); print("="*60 + "\n")

# ===== Fonction pour exécuter les mises à jour Weezevent en arrière-plan =====
def run_updates_in_background(flask_app):
    global update_in_progress, update_lock
    timestamp_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp_start}] [Thread Background] Démarrage MAJ Weezevent...")
    try:
        with flask_app.app_context():
            print(f"[{timestamp_start}] [Thread Background] Exécution get_events()...")
            get_events()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [Thread Background] Exécution get_registrations()...")
            get_registrations()
        timestamp_end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp_end}] [Thread Background] MAJ Weezevent terminées.")
    except Exception as e:
        timestamp_err = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp_err}] [Thread Background] ERREUR MAJ Weezevent: {e}")
        print(traceback.format_exc())
    finally:
        with update_lock:
            update_in_progress = False
        timestamp_final = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp_final}] [Thread Background] Fin du thread MAJ Weezevent.")

# --- Décorateur pour vérifier si l'utilisateur est connecté ---
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            flash("Connexion requise.", "warning")
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

# ===== Route pour lancer la mise à jour Weezevent =====
@app.route('/lancer-mise-a-jour', methods=['POST'])
@login_required
def launch_background_update():
    global update_in_progress, update_lock
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] Requête /lancer-mise-a-jour")
    with update_lock:
        if update_in_progress:
            print(f"[{timestamp}] MAJ déjà en cours.")
            flash("Une mise à jour des données est déjà en cours.", "warning")
            return redirect(url_for('show_maintenance'))
        else:
            print(f"[{timestamp}] Lancement thread MAJ Weezevent...")
            update_in_progress = True
            update_thread = threading.Thread(target=run_updates_in_background, args=(app,))
            update_thread.daemon = True
            update_thread.start()
            print(f"[{timestamp}] Thread démarré.")
            return redirect(url_for('show_maintenance'))

# ===== Route affichant la page d'attente pendant la mise à jour Weezevent =====
@app.route('/en-cours-de-mise-a-jour')
@login_required
def show_maintenance():
    global update_in_progress, update_lock
    with update_lock:
        in_progress = update_in_progress
    if in_progress:
        headers = {'Refresh': '6'}
        return render_template('maintenance.html'), 200, headers
    else:
        flash("La mise à jour des données est terminée.", "info")
        return redirect(url_for('select_event'))

# ===== Route pour la connexion utilisateur =====
@app.route('/login', methods=['GET', 'POST'])
def login():
    if 'logged_in' in session:
        return redirect(url_for('index'))

    error = None
    locked_out = False
    remaining_time = 0
    remaining_attempts = MAX_LOGIN_ATTEMPTS - session.get('login_attempts', 0)

    lockout_until = session.get('lockout_until', 0)
    current_time = time.time()
    if current_time < lockout_until:
        locked_out = True
        remaining_time = int(lockout_until - current_time)
        remaining_attempts = 0
        if request.method == 'POST':
             return render_template('login.html', error=None, locked_out=True, remaining_time=remaining_time, remaining_attempts=0)
    elif 'lockout_until' in session:
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
            if check_password_hash(expected_hash, password_form):
                session.clear()
                session['logged_in'] = True
                session['username'] = username_form
                session.permanent = False
                flash(f"Connexion réussie ! Bienvenue {username_form.capitalize()}.", "success")
                next_url = request.args.get('next')
                return redirect(next_url or url_for('index'))
            else:
                error = "Nom d'utilisateur ou mot de passe invalide."
                session['login_attempts'] = session.get('login_attempts', 0) + 1
        else:
            error = "Nom d'utilisateur ou mot de passe invalide."
            session['login_attempts'] = session.get('login_attempts', 0) + 1

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
        remaining_attempts = max(0, remaining_attempts)

    return render_template('login.html',
                           error=error,
                           locked_out=locked_out,
                           remaining_time=remaining_time,
                           remaining_attempts=remaining_attempts)

# ===== Route pour la déconnexion =====
@app.route('/logout')
@login_required
def logout():
    session.clear()
    flash("Vous avez été déconnecté.", "info")
    return redirect(url_for('login'))

# ===== Route principale (accueil) =====
@app.route("/")
@login_required
def index():
    return redirect(url_for('select_event'))

# ===== Route pour afficher/sélectionner les événements et participants =====
@app.route("/select_event", methods=["GET", "POST"])
@login_required
def select_event():
    conn = None
    cursor = None
    participants_processed = []
    selected_event_id_int = None
    processed_events = []
    placeholder_missing_info = "Non renseigné"

    try:
        conn = get_connection()
        if not conn:
            raise ConnectionError("Connexion DB impossible depuis le pool.")
        cursor = conn.cursor(dictionary=True)

        # Gestion de l'ID événement sélectionné (POST ou session)
        selected_event_id_str = session.get("selected_event_id")
        if request.method == "POST":
            event_id = request.form.get("event_id")
            if event_id and event_id.isdigit():
                selected_event_id_int = int(event_id)
                session["selected_event_id"] = str(selected_event_id_int)
            else:
                 session.pop("selected_event_id", None)
                 selected_event_id_int = None
                 if event_id: flash(f"ID événement invalide fourni : '{event_id}'.", "warning")
        elif selected_event_id_str:
             try:
                 selected_event_id_int = int(selected_event_id_str)
             except (ValueError, TypeError):
                 flash(f"ID événement invalide en session ('{selected_event_id_str}'). Sélection effacée.", "warning")
                 session.pop("selected_event_id", None)
                 selected_event_id_int = None

        # Récupération des événements actifs depuis la DB
        sql_events = "SELECT * FROM evenements WHERE actif = 1 ORDER BY date DESC, nom ASC"
        cursor.execute(sql_events)
        events_from_db = cursor.fetchall()
        # app.logger.info(f"{len(events_from_db)} événements actifs récupérés.") # Log retiré pour moins de commentaires

        # Traitement des événements pour l'affichage
        for event_db in events_from_db:
            event_processed = event_db.copy()
            event_date = event_db.get('date')
            formatted_event_date = placeholder_missing_info
            try:
                if isinstance(event_date, (date, datetime)): formatted_event_date = event_date.strftime('%d/%m/%Y')
                elif isinstance(event_date, str) and event_date: formatted_event_date = datetime.strptime(event_date, '%Y-%m-%d').strftime('%d/%m/%Y')
            except (ValueError, TypeError): pass
            event_processed['date_display'] = formatted_event_date
            event_processed['nom'] = event_db.get('nom') or placeholder_missing_info
            processed_events.append(event_processed)

        # Récupération et traitement des participants si un événement est sélectionné
        if selected_event_id_int is not None:
            if not any(evt['event_id'] == selected_event_id_int for evt in processed_events):
                 # app.logger.warning(f"Tentative affichage event {selected_event_id_int} non autorisé.") # Log retiré
                 flash("L'événement sélectionné n'est plus disponible.", "warning")
                 session.pop("selected_event_id", None)
                 selected_event_id_int = None
            else:
                # app.logger.debug(f"Récupération participants event: {selected_event_id_int}") # Log retiré
                try:
                    cursor.execute("SELECT * FROM inscriptions WHERE event_id=%s ORDER BY nom ASC, prenom ASC", (selected_event_id_int,))
                    participants_db = cursor.fetchall()
                    # app.logger.info(f"{len(participants_db)} participants récupérés pour event {selected_event_id_int}.") # Log retiré

                    for p_db in participants_db:
                        p_processed = p_db.copy()
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
                        needs_details = p_processed["amenagements_necessaires"] == "Oui"
                        p_processed["amenagements_details"] = p_db.get("amenagements_details") or (placeholder_missing_info if needs_details else "")
                        p_processed["nom_billet"] = p_db.get("nom_billet") or placeholder_missing_info

                        date_naissance_db = p_db.get("date_naissance")
                        formatted_naissance = placeholder_missing_info
                        try:
                            if isinstance(date_naissance_db, date): formatted_naissance = date_naissance_db.strftime('%d/%m/%Y')
                            elif isinstance(date_naissance_db, str) and date_naissance_db: formatted_naissance = datetime.strptime(date_naissance_db, '%Y-%m-%d').strftime('%d/%m/%Y')
                        except (ValueError, TypeError): pass
                        p_processed["date_naissance_display"] = formatted_naissance

                        date_creation_db = p_db.get("date_creation_inscription")
                        formatted_creation = placeholder_missing_info
                        try:
                           if isinstance(date_creation_db, datetime): formatted_creation = date_creation_db.strftime('%d/%m/%Y %H:%M')
                           elif isinstance(date_creation_db, str) and date_creation_db:
                               parsed_creation = None
                               for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                                   try: parsed_creation = datetime.strptime(date_creation_db, fmt); break
                                   except ValueError: continue
                               if parsed_creation: formatted_creation = parsed_creation.strftime('%d/%m/%Y %H:%M')
                        except (ValueError, TypeError): pass
                        p_processed["date_creation_display"] = formatted_creation

                        montant_db = p_db.get("montant_paye")
                        formatted_montant = placeholder_missing_info
                        try:
                            if isinstance(montant_db, decimal.Decimal): formatted_montant = str(montant_db)
                            elif montant_db is not None: formatted_montant = str(decimal.Decimal(montant_db))
                        except (TypeError, decimal.InvalidOperation): pass
                        p_processed["montant_paye_display"] = formatted_montant

                        code_promo_db = p_db.get("code_promo")
                        p_processed["code_promo_display"] = "Oui" if code_promo_db else "Non"
                        participants_processed.append(p_processed)

                except Exception as e_part:
                     app.logger.error(f"Erreur traitement participants event {selected_event_id_int}: {e_part}", exc_info=True)
                     flash("Erreur lors du chargement des participants.", "danger")
                     participants_processed = []

    except mysql.connector.Error as db_err:
        app.logger.error(f"Erreur DB dans select_event : {db_err}", exc_info=True)
        flash("Erreur de connexion ou de requête à la base de données.", "danger")
        processed_events, participants_processed = [], []
        selected_event_id_int = None
    except Exception as e:
        app.logger.error(f"Erreur générale dans select_event : {e}", exc_info=True)
        flash("Une erreur inattendue est survenue lors du chargement de la page.", "danger")
        processed_events, participants_processed = [], []
        selected_event_id_int = None

    finally:
        if cursor: cursor.close()
        if conn: conn.close()
        # app.logger.debug("Connexion BDD fermée/remise au pool pour select_event.") # Log retiré

    username = session.get('username', '')
    return render_template("select_event.html",
                           events=processed_events,
                           participants=participants_processed,
                           selected_event_id=selected_event_id_int,
                           username=username.capitalize())

# ===== Route pour exporter les participants en CSV =====
@app.route("/export_participants")
@login_required
def export_participants():
    selected_event_id_str = session.get("selected_event_id")
    if not selected_event_id_str:
        flash("Aucun événement sélectionné pour l'export.", "warning")
        return redirect(url_for('select_event'))

    conn = None
    cursor = None
    event_name = "evenement_inconnu"
    placeholder_missing_info = "Non renseigné"

    try:
        try: selected_event_id_int = int(selected_event_id_str)
        except (ValueError, TypeError):
             flash("ID événement invalide pour l'export.", "danger")
             return redirect(url_for('select_event'))

        conn = get_connection()
        if not conn:
             app.logger.error("Export impossible: Connexion DB échouée.")
             flash("Erreur de connexion à la base de données pour l'export.", "danger")
             return redirect(url_for('select_event'))
        cursor = conn.cursor(dictionary=True)

        try:
            cursor.execute("SELECT nom FROM evenements WHERE event_id = %s", (selected_event_id_int,))
            event_data = cursor.fetchone()
            event_name = (event_data['nom'] if event_data and event_data['nom'] else f"event_{selected_event_id_int}")
        except Exception as e_event_name:
            # app.logger.warning(f"Récup nom event {selected_event_id_int} échouée: {e_event_name}") # Log retiré
            event_name = f"event_{selected_event_id_int}"

        safe_name = re.sub(r'[^\w\-]+', '', event_name.replace(' ', '_'))
        safe_name = re.sub(r'[_]+', '_', safe_name).strip('_')[:60]
        safe_name = safe_name or "evenement"
        download_filename = f"participants_{safe_name}.csv"

        cursor.execute("SELECT * FROM inscriptions WHERE event_id=%s ORDER BY nom ASC, prenom ASC", (selected_event_id_int,))
        participants = cursor.fetchall()

        if not participants:
            flash("Aucun participant à exporter pour cet événement.", "info")
            return redirect(url_for('select_event'))

        output = io.StringIO()
        writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)

        writer.writerow([
            "Nom", "Prénom", "Email", "Téléphone", "Date Naissance",
            "Adresse", "Ville", "Code Postal",
            "Date Inscription", "Heure Inscription",
            "Connu la formation", "Éligible Financement", "RQTH",
            "Besoin Aménagements", "Détails Aménagements",
            "Montant Payé",
            "Type Billet", "Code Promo Utilisé ?"
        ])

        for p in participants:
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
            needs_details = amenagements == "Oui"
            amenagements_details = p.get("amenagements_details") or (placeholder_missing_info if needs_details else "")
            nom_billet = p.get("nom_billet") or placeholder_missing_info

            date_naissance_str = placeholder_missing_info
            date_naissance_obj = p.get("date_naissance")
            try:
                if isinstance(date_naissance_obj, date): date_naissance_str = date_naissance_obj.strftime('%d/%m/%Y')
                elif isinstance(date_naissance_obj, str) and date_naissance_obj: date_naissance_str = datetime.strptime(date_naissance_obj, '%Y-%m-%d').strftime('%d/%m/%Y')
            except ValueError: pass

            date_creation_str = placeholder_missing_info
            heure_creation_str = ""
            date_creation_obj = p.get("date_creation_inscription")
            try:
                parsed_creation = None
                if isinstance(date_creation_obj, datetime): parsed_creation = date_creation_obj
                elif isinstance(date_creation_obj, str) and date_creation_obj:
                    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                        try: parsed_creation = datetime.strptime(date_creation_obj, fmt); break
                        except ValueError: continue
                if parsed_creation:
                    date_creation_str = parsed_creation.strftime('%d/%m/%Y')
                    heure_creation_str = parsed_creation.strftime('%H:%M:%S')
            except (ValueError, TypeError): pass

            montant_paye_str = placeholder_missing_info
            montant_paye = p.get("montant_paye")
            try:
                if isinstance(montant_paye, decimal.Decimal): montant_paye_str = "{:.2f}".format(montant_paye).replace('.', ',')
                elif montant_paye is not None: montant_paye_str = "{:.2f}".format(decimal.Decimal(montant_paye)).replace('.', ',')
            except (TypeError, decimal.InvalidOperation): pass

            code_promo_db = p.get("code_promo")
            code_promo_export = "Oui" if code_promo_db else "Non"

            writer.writerow([
                nom, prenom, email, telephone, date_naissance_str,
                adresse, ville, code_postal,
                date_creation_str, heure_creation_str,
                source_info, financement, rqth,
                amenagements, amenagements_details,
                montant_paye_str, nom_billet,
                code_promo_export
            ])

        csv_data_bytes = output.getvalue().encode('utf-8-sig')
        buffer = io.BytesIO(csv_data_bytes)
        buffer.seek(0)

        return send_file(
            buffer,
            mimetype='text/csv; charset=utf-8-sig',
            as_attachment=True,
            download_name=download_filename
        )

    except mysql.connector.Error as db_err:
         app.logger.error(f"Erreur DB Export: {db_err}", exc_info=True)
         flash("Erreur de base de données lors de la préparation de l'export.", "danger")
         return redirect(url_for('select_event'))
    except KeyError as e_key:
         col_manquante = str(e_key).strip("'");
         app.logger.error(f"ERREUR Export: Clé manquante '{col_manquante}'.", exc_info=True)
         flash(f"Erreur export : Donnée manquante ('{col_manquante}'). Vérifiez la structure BDD.", "danger")
         return redirect(url_for('select_event'))
    except Exception as e:
         app.logger.error(f"Erreur inattendue Export: {e}", exc_info=True)
         flash(f"Erreur inattendue lors de la génération de l'export : {e}", "danger")
         return redirect(url_for('select_event'))
    finally:
        if cursor: cursor.close()
        if conn: conn.close()
        # app.logger.debug("Connexion BDD fermée/remise au pool pour export.") # Log retiré


# ===== Endpoint pour déclencher la vérification de la taille de la BDD (appel externe) =====
@app.route('/trigger-db-check/<secret_key>', methods=['POST'])
def trigger_db_check_endpoint(secret_key):
    """Endpoint sécurisé pour déclencher la vérification BDD par un service externe."""
    if secret_key != CRON_SECRET_KEY:
        print(f"ALERTE SÉCURITÉ: Tentative accès non autorisé au trigger DB check.")
        abort(403) # Forbidden

    print("INFO: Requête reçue /trigger-db-check. Lancement vérification BDD...")
    try:
        check_database_size() # Appel de la fonction importée
        print("INFO: Appel à check_database_size terminé.")
        return "Vérification de la base de données déclenchée avec succès.", 200
    except Exception as e:
        print(f"ERREUR: Échec exécution check_database_size via endpoint: {e}")
        print(traceback.format_exc())
        return "Erreur interne lors du déclenchement de la vérification.", 500


# --- Démarrage de l'application Flask ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    use_reloader = os.environ.get("FLASK_USE_RELOADER", "false").lower() == "true"

    print(f"Démarrage Flask sur http://0.0.0.0:{port}")
    print(f"Mode Debug: {debug_mode}, Reloader: {use_reloader}")
    if use_reloader and debug_mode:
        print("ATTENTION: Reloader activé, peut interférer avec les threads.")

    # Configuration basique du logging Flask
    if not debug_mode:
        import logging
        log_level = logging.INFO
        logging.basicConfig(level=log_level, format='%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]')
        app.logger.setLevel(log_level)
        app.logger.info("Logging configuré pour production.")
    else:
        app.logger.setLevel(logging.DEBUG)
        app.logger.info("Logging configuré pour debug.")

    app.run(debug=debug_mode, host='0.0.0.0', port=port, use_reloader=use_reloader)