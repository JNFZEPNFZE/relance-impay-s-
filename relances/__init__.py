"""Application de gestion des relances de factures impayées."""
import csv
import io
import os
from datetime import date

from flask import (Flask, Response, abort, flash, redirect, render_template, request,
                   url_for)

from . import db as dbmod
from .db import NIVEAUX, get_db, get_parametres
from .logique import (NIVEAU_CONTENTIEUX, VARIABLES_DISPONIBLES, calculer_situation,
                      construire_ligne, format_date, format_date_longue, format_euro,
                      parse_date, parse_montant, remplir_modele, variables_courrier)

MODES_ENVOI = ["Courrier simple", "Lettre recommandée avec AR", "E-mail", "Remise en main propre"]

CHAMPS_CLIENT = ["nom", "type", "contact", "adresse", "code_postal", "ville", "email",
                 "telephone", "notes"]


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("RELANCES_SECRET_KEY", "dev-relances-impayes"),
        DATABASE=os.environ.get("RELANCES_DB",
                                os.path.join(app.instance_path, "relances.db")),
        AUJOURDHUI=None,  # permet de figer la date (tests)
    )
    if config:
        app.config.update(config)
    os.makedirs(os.path.dirname(os.path.abspath(app.config["DATABASE"])), exist_ok=True)

    app.teardown_appcontext(dbmod.close_db)
    with app.app_context():
        dbmod.init_db()

    app.jinja_env.filters["euro"] = format_euro
    app.jinja_env.filters["date_fr"] = format_date
    app.jinja_env.filters["date_longue"] = format_date_longue

    @app.context_processor
    def inject():
        return {"NIVEAUX": NIVEAUX, "NIVEAU_CONTENTIEUX": NIVEAU_CONTENTIEUX,
                "aujourd_hui": aujourd_hui()}

    enregistrer_routes(app)
    return app


def aujourd_hui():
    from flask import current_app
    forcee = current_app.config.get("AUJOURDHUI")
    return parse_date(forcee) if forcee else date.today()


# --------------------------------------------------------------------------- accès données

def charger_client(client_id):
    client = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None:
        abort(404)
    return client


def situation_client(client, params=None, today=None):
    """Retourne (situation, toutes_les_lignes_du_client)."""
    db = get_db()
    params = params or get_parametres()
    today = today or aujourd_hui()
    niveaux = dict(db.execute(
        """SELECT rf.facture_id, MAX(r.niveau) FROM relance_factures rf
           JOIN relances r ON r.id = rf.relance_id
           WHERE r.client_id = ? GROUP BY rf.facture_id""", (client["id"],)).fetchall())
    rows = db.execute("SELECT * FROM factures WHERE client_id = ? ORDER BY date_echeance",
                      (client["id"],)).fetchall()
    lignes = [construire_ligne(r, client["type"], params, today, niveaux.get(r["id"], 0))
              for r in rows]
    echues = [l for l in lignes if not l.payee and l.jours_retard > 0 and not l.litige]
    derniere = None
    if echues:
        marques = ",".join("?" * len(echues))
        derniere = db.execute(
            f"""SELECT r.niveau, r.date_envoi FROM relances r
                JOIN relance_factures rf ON rf.relance_id = r.id
                WHERE rf.facture_id IN ({marques})
                ORDER BY r.date_envoi DESC, r.id DESC LIMIT 1""",
            [l.id for l in echues]).fetchone()
    return calculer_situation(echues, derniere, params, today), lignes


def rediger_courrier(client, lignes, niveau, date_courrier, mode, reference, params,
                     date_derniere_relance):
    modele = get_db().execute("SELECT * FROM modeles WHERE niveau = ?", (niveau,)).fetchone()
    variables = variables_courrier(client, lignes, niveau, date_courrier, params,
                                   date_derniere_relance)
    return render_template(
        "_lettre.html", client=client, params=params, niveau=niveau, mode=mode,
        reference=reference, date_courrier=parse_date(date_courrier),
        objet=remplir_modele(modele["objet"], variables).striptags(),
        corps=remplir_modele(modele["corps"], variables))


def enregistrer_relance(client, lignes, niveau, date_courrier, mode, reference, params,
                        date_derniere_relance):
    html = rediger_courrier(client, lignes, niveau, date_courrier, mode, reference, params,
                            date_derniere_relance)
    db = get_db()
    cur = db.execute(
        """INSERT INTO relances (client_id, niveau, date_envoi, mode_envoi, reference,
                                 montant_du, contenu_html)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (client["id"], niveau, parse_date(date_courrier).isoformat(), mode, reference,
         round(sum(l.reste_du for l in lignes), 2), html))
    db.executemany("INSERT INTO relance_factures (relance_id, facture_id) VALUES (?, ?)",
                   [(cur.lastrowid, l.id) for l in lignes])
    return cur.lastrowid


def mode_par_defaut(niveau):
    return "Lettre recommandée avec AR" if niveau >= 3 else "Courrier simple"


# --------------------------------------------------------------------------- routes

def enregistrer_routes(app):

    # ------------------------------------------------------------------ tableau de bord
    @app.route("/")
    def tableau_de_bord():
        params = get_parametres()
        clients = get_db().execute("SELECT * FROM clients ORDER BY nom").fetchall()
        lignes_tdb = []
        for c in clients:
            sit, _ = situation_client(c, params)
            if sit.factures:
                lignes_tdb.append((c, sit))
        lignes_tdb.sort(key=lambda x: (not x[1].a_relancer, -x[1].retard_max))
        stats = {
            "encours_echu": sum(s.total_du for _, s in lignes_tdb),
            "nb_factures": sum(len(s.factures) for _, s in lignes_tdb),
            "nb_clients": len(lignes_tdb),
            "a_relancer": sum(1 for _, s in lignes_tdb
                              if s.a_relancer and s.niveau_suggere < NIVEAU_CONTENTIEUX),
            "contentieux": sum(1 for _, s in lignes_tdb
                               if s.niveau_suggere == NIVEAU_CONTENTIEUX),
        }
        return render_template("tableau_de_bord.html", lignes=lignes_tdb, stats=stats)

    # ------------------------------------------------------------------ clients
    @app.route("/clients")
    def clients():
        rows = get_db().execute(
            """SELECT c.*, COUNT(f.id) AS nb_factures,
                      COALESCE(SUM(MAX(f.montant_ttc - f.montant_paye, 0)), 0) AS encours
               FROM clients c LEFT JOIN factures f ON f.client_id = c.id
               GROUP BY c.id ORDER BY c.nom""").fetchall()
        return render_template("clients.html", clients=rows)

    def lire_form_client():
        data = {k: request.form.get(k, "").strip() for k in CHAMPS_CLIENT}
        if data["type"] not in ("professionnel", "particulier"):
            data["type"] = "professionnel"
        return data

    @app.route("/clients/nouveau", methods=["GET", "POST"])
    def client_nouveau():
        if request.method == "POST":
            data = lire_form_client()
            if not data["nom"]:
                flash("Le nom du client est obligatoire.", "erreur")
            else:
                db = get_db()
                try:
                    cur = db.execute(
                        f"INSERT INTO clients ({', '.join(CHAMPS_CLIENT)}) "
                        f"VALUES ({', '.join('?' * len(CHAMPS_CLIENT))})",
                        [data[k] for k in CHAMPS_CLIENT])
                    db.commit()
                    flash("Client créé.", "succes")
                    return redirect(url_for("client_fiche", client_id=cur.lastrowid))
                except Exception:
                    flash("Un client porte déjà ce nom.", "erreur")
            return render_template("client_form.html", client=data)
        return render_template("client_form.html", client={"type": "professionnel"})

    @app.route("/clients/<int:client_id>")
    def client_fiche(client_id):
        client = charger_client(client_id)
        sit, lignes = situation_client(client)
        relances = get_db().execute(
            "SELECT * FROM relances WHERE client_id = ? ORDER BY date_envoi DESC, id DESC",
            (client_id,)).fetchall()
        return render_template("client_fiche.html", client=client, sit=sit, lignes=lignes,
                               relances=relances)

    @app.route("/clients/<int:client_id>/modifier", methods=["GET", "POST"])
    def client_modifier(client_id):
        client = charger_client(client_id)
        if request.method == "POST":
            data = lire_form_client()
            if not data["nom"]:
                flash("Le nom du client est obligatoire.", "erreur")
                return render_template("client_form.html", client=data)
            db = get_db()
            try:
                db.execute(
                    f"UPDATE clients SET {', '.join(k + ' = ?' for k in CHAMPS_CLIENT)} "
                    f"WHERE id = ?", [data[k] for k in CHAMPS_CLIENT] + [client_id])
                db.commit()
            except Exception:
                flash("Un client porte déjà ce nom.", "erreur")
                return render_template("client_form.html", client=data)
            flash("Client mis à jour.", "succes")
            return redirect(url_for("client_fiche", client_id=client_id))
        return render_template("client_form.html", client=client)

    @app.post("/clients/<int:client_id>/supprimer")
    def client_supprimer(client_id):
        charger_client(client_id)
        db = get_db()
        db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        db.commit()
        flash("Client supprimé (avec ses factures et relances).", "succes")
        return redirect(url_for("clients"))

    # ------------------------------------------------------------------ factures
    @app.route("/factures")
    def factures():
        filtre = request.args.get("filtre", "impayees")
        params = get_parametres()
        today = aujourd_hui()
        db = get_db()
        rows = db.execute(
            """SELECT f.*, c.nom AS client_nom, c.type AS client_type FROM factures f
               JOIN clients c ON c.id = f.client_id ORDER BY f.date_echeance""").fetchall()
        niveaux = dict(db.execute(
            """SELECT rf.facture_id, MAX(r.niveau) FROM relance_factures rf
               JOIN relances r ON r.id = rf.relance_id GROUP BY rf.facture_id""").fetchall())
        liste = []
        for r in rows:
            l = construire_ligne(r, r["client_type"], params, today, niveaux.get(r["id"], 0))
            if filtre == "impayees" and l.payee:
                continue
            if filtre == "retard" and (l.payee or l.jours_retard == 0):
                continue
            if filtre == "payees" and not l.payee:
                continue
            liste.append((r, l))
        return render_template("factures.html", factures=liste, filtre=filtre)

    def lire_form_facture():
        erreurs = []
        data = {
            "numero": request.form.get("numero", "").strip(),
            "client_id": request.form.get("client_id", type=int),
            "notes": request.form.get("notes", "").strip(),
            "litige": 1 if request.form.get("litige") else 0,
        }
        for champ in ("date_emission", "date_echeance"):
            try:
                data[champ] = parse_date(request.form.get(champ, "")).isoformat()
            except ValueError as e:
                erreurs.append(str(e))
                data[champ] = request.form.get(champ, "")
        for champ in ("montant_ttc", "montant_paye"):
            try:
                data[champ] = parse_montant(request.form.get(champ, "0"))
            except ValueError as e:
                erreurs.append(str(e))
                data[champ] = 0
        if not data["numero"]:
            erreurs.append("Le numéro de facture est obligatoire.")
        if not data["client_id"]:
            erreurs.append("Le client est obligatoire.")
        if data.get("montant_ttc", 0) < 0 or data.get("montant_paye", 0) < 0:
            erreurs.append("Les montants doivent être positifs.")
        return data, erreurs

    CHAMPS_FACTURE = ["numero", "client_id", "date_emission", "date_echeance",
                      "montant_ttc", "montant_paye", "litige", "notes"]

    def form_facture(facture, facture_id=None):
        clients_ = get_db().execute("SELECT id, nom FROM clients ORDER BY nom").fetchall()
        return render_template("facture_form.html", facture=facture, clients=clients_,
                               facture_id=facture_id)

    @app.route("/factures/nouvelle", methods=["GET", "POST"])
    def facture_nouvelle():
        if request.method == "POST":
            data, erreurs = lire_form_facture()
            if not erreurs:
                db = get_db()
                try:
                    db.execute(
                        f"INSERT INTO factures ({', '.join(CHAMPS_FACTURE)}) "
                        f"VALUES ({', '.join('?' * len(CHAMPS_FACTURE))})",
                        [data[k] for k in CHAMPS_FACTURE])
                    db.commit()
                    flash(f"Facture {data['numero']} enregistrée.", "succes")
                    return redirect(url_for("client_fiche", client_id=data["client_id"]))
                except Exception:
                    erreurs.append("Ce numéro de facture existe déjà.")
            for e in erreurs:
                flash(e, "erreur")
            return form_facture(data)
        return form_facture({"client_id": request.args.get("client_id", type=int),
                             "date_emission": aujourd_hui().isoformat(),
                             "montant_paye": 0})

    @app.route("/factures/<int:facture_id>/modifier", methods=["GET", "POST"])
    def facture_modifier(facture_id):
        db = get_db()
        facture = db.execute("SELECT * FROM factures WHERE id = ?", (facture_id,)).fetchone()
        if facture is None:
            abort(404)
        if request.method == "POST":
            data, erreurs = lire_form_facture()
            if not erreurs:
                try:
                    db.execute(
                        f"UPDATE factures SET {', '.join(k + ' = ?' for k in CHAMPS_FACTURE)}"
                        f" WHERE id = ?", [data[k] for k in CHAMPS_FACTURE] + [facture_id])
                    db.commit()
                    flash("Facture mise à jour.", "succes")
                    return redirect(url_for("client_fiche", client_id=data["client_id"]))
                except Exception:
                    erreurs.append("Ce numéro de facture existe déjà.")
            for e in erreurs:
                flash(e, "erreur")
            return form_facture(data, facture_id)
        return form_facture(facture, facture_id)

    @app.post("/factures/<int:facture_id>/paiement")
    def facture_paiement(facture_id):
        db = get_db()
        f = db.execute("SELECT * FROM factures WHERE id = ?", (facture_id,)).fetchone()
        if f is None:
            abort(404)
        if request.form.get("solde"):
            montant = f["montant_ttc"] - f["montant_paye"]
        else:
            try:
                montant = parse_montant(request.form.get("montant", ""))
            except ValueError as e:
                flash(str(e), "erreur")
                return redirect(request.referrer or url_for("factures"))
        if montant <= 0:
            flash("Le montant du paiement doit être positif.", "erreur")
        else:
            nouveau = round(min(f["montant_ttc"], f["montant_paye"] + montant), 2)
            db.execute("UPDATE factures SET montant_paye = ? WHERE id = ?",
                       (nouveau, facture_id))
            db.commit()
            flash(f"Paiement de {format_euro(montant)} enregistré sur la facture "
                  f"{f['numero']}.", "succes")
        return redirect(request.referrer or url_for("factures"))

    @app.post("/factures/<int:facture_id>/supprimer")
    def facture_supprimer(facture_id):
        db = get_db()
        f = db.execute("SELECT * FROM factures WHERE id = ?", (facture_id,)).fetchone()
        if f is None:
            abort(404)
        db.execute("DELETE FROM factures WHERE id = ?", (facture_id,))
        db.commit()
        flash(f"Facture {f['numero']} supprimée.", "succes")
        return redirect(url_for("client_fiche", client_id=f["client_id"]))

    @app.route("/factures/import", methods=["GET", "POST"])
    def factures_import():
        if request.method == "GET":
            return render_template("import.html")
        fichier = request.files.get("fichier")
        if not fichier or not fichier.filename:
            flash("Choisissez un fichier CSV.", "erreur")
            return render_template("import.html")
        brut = fichier.read()
        try:
            texte = brut.decode("utf-8-sig")
        except UnicodeDecodeError:
            texte = brut.decode("cp1252")
        resultat = importer_csv(texte)
        return render_template("import.html", resultat=resultat)

    @app.route("/factures/export.csv")
    def factures_export():
        params = get_parametres()
        today = aujourd_hui()
        rows = get_db().execute(
            """SELECT f.*, c.nom AS client_nom, c.type AS client_type FROM factures f
               JOIN clients c ON c.id = f.client_id ORDER BY c.nom, f.date_echeance""").fetchall()
        sortie = io.StringIO()
        w = csv.writer(sortie, delimiter=";")
        w.writerow(["client", "numero", "date_emission", "date_echeance", "montant_ttc",
                    "montant_paye", "reste_du", "jours_retard", "penalites", "litige"])
        for r in rows:
            l = construire_ligne(r, r["client_type"], params, today)
            if l.payee:
                continue
            w.writerow([r["client_nom"], l.numero, format_date(l.date_emission),
                        format_date(l.date_echeance), f"{l.montant_ttc:.2f}".replace(".", ","),
                        f"{l.montant_paye:.2f}".replace(".", ","),
                        f"{l.reste_du:.2f}".replace(".", ","), l.jours_retard,
                        f"{l.penalites:.2f}".replace(".", ","), "oui" if l.litige else "non"])
        return Response("﻿" + sortie.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition":
                                 f"attachment; filename=impayes-{today.isoformat()}.csv"})

    # ------------------------------------------------------------------ relances
    @app.route("/clients/<int:client_id>/relance", methods=["GET", "POST"])
    def relance_preparer(client_id):
        client = charger_client(client_id)
        params = get_parametres()
        sit, lignes = situation_client(client, params)
        impayees = [l for l in lignes if not l.payee]
        if not impayees:
            flash("Ce client n'a aucune facture impayée.", "info")
            return redirect(url_for("client_fiche", client_id=client_id))

        source = request.form if request.method == "POST" else request.args
        niveau_defaut = sit.niveau_suggere or min(sit.niveau_actuel + 1, 3)
        niveau = min(max(source.get("niveau", niveau_defaut, type=int), 1), 3)
        if "factures" in source:
            ids = {int(i) for i in source.getlist("factures")}
        else:
            ids = {l.id for l in sit.factures}
        selection = [l for l in impayees if l.id in ids]
        try:
            date_courrier = parse_date(source.get("date") or aujourd_hui().isoformat())
        except ValueError:
            date_courrier = aujourd_hui()
        mode = source.get("mode") or mode_par_defaut(niveau)
        reference = source.get("reference", "").strip()

        if request.method == "POST":
            if not selection:
                flash("Sélectionnez au moins une facture.", "erreur")
            else:
                rid = enregistrer_relance(client, selection, niveau, date_courrier, mode,
                                          reference, params, sit.date_derniere_relance)
                get_db().commit()
                flash(f"{NIVEAUX[niveau]} enregistrée pour {client['nom']}.", "succes")
                return redirect(url_for("relance_voir", relance_id=rid))

        apercu = None
        if selection:
            apercu = rediger_courrier(client, selection, niveau, date_courrier, mode,
                                      reference, params, sit.date_derniere_relance)
        return render_template("relance_preparer.html", client=client, sit=sit,
                               impayees=impayees, ids=ids, niveau=niveau,
                               date_courrier=date_courrier, mode=mode, reference=reference,
                               modes=MODES_ENVOI, apercu=apercu)

    @app.post("/relances/lot")
    def relances_lot():
        params = get_parametres()
        today = aujourd_hui()
        db = get_db()
        crees = []
        for c in db.execute("SELECT * FROM clients ORDER BY nom").fetchall():
            sit, _ = situation_client(c, params, today)
            if sit.a_relancer and sit.niveau_suggere < NIVEAU_CONTENTIEUX:
                crees.append(enregistrer_relance(
                    c, sit.factures, sit.niveau_suggere, today,
                    mode_par_defaut(sit.niveau_suggere), "", params,
                    sit.date_derniere_relance))
        db.commit()
        if not crees:
            flash("Aucune relance à générer aujourd'hui.", "info")
            return redirect(url_for("tableau_de_bord"))
        flash(f"{len(crees)} courrier(s) de relance généré(s).", "succes")
        return redirect(url_for("relances_impression", ids=",".join(map(str, crees))))

    @app.route("/relances")
    def relances():
        rows = get_db().execute(
            """SELECT r.*, c.nom AS client_nom,
                      (SELECT GROUP_CONCAT(f.numero, ', ') FROM relance_factures rf
                       JOIN factures f ON f.id = rf.facture_id
                       WHERE rf.relance_id = r.id) AS numeros
               FROM relances r JOIN clients c ON c.id = r.client_id
               ORDER BY r.date_envoi DESC, r.id DESC""").fetchall()
        return render_template("relances.html", relances=rows)

    @app.route("/relances/<int:relance_id>")
    def relance_voir(relance_id):
        r = get_db().execute(
            """SELECT r.*, c.nom AS client_nom FROM relances r
               JOIN clients c ON c.id = r.client_id WHERE r.id = ?""",
            (relance_id,)).fetchone()
        if r is None:
            abort(404)
        return render_template("impression.html", relances=[r], retour=url_for(
            "client_fiche", client_id=r["client_id"]))

    @app.route("/relances/impression")
    def relances_impression():
        try:
            ids = [int(i) for i in request.args.get("ids", "").split(",") if i]
        except ValueError:
            abort(400)
        if not ids:
            abort(404)
        marques = ",".join("?" * len(ids))
        rows = get_db().execute(
            f"""SELECT r.*, c.nom AS client_nom FROM relances r
                JOIN clients c ON c.id = r.client_id WHERE r.id IN ({marques})
                ORDER BY c.nom""", ids).fetchall()
        return render_template("impression.html", relances=rows,
                               retour=url_for("relances"))

    @app.post("/relances/<int:relance_id>/supprimer")
    def relance_supprimer(relance_id):
        db = get_db()
        r = db.execute("SELECT * FROM relances WHERE id = ?", (relance_id,)).fetchone()
        if r is None:
            abort(404)
        db.execute("DELETE FROM relances WHERE id = ?", (relance_id,))
        db.commit()
        flash("Relance supprimée de l'historique.", "succes")
        return redirect(request.referrer or url_for("relances"))

    # ------------------------------------------------------------------ paramétrage
    @app.route("/modeles", methods=["GET", "POST"])
    def modeles():
        db = get_db()
        if request.method == "POST":
            for niveau in NIVEAUX:
                objet = request.form.get(f"objet_{niveau}", "").strip()
                corps = request.form.get(f"corps_{niveau}", "").strip()
                if objet and corps:
                    db.execute("UPDATE modeles SET objet = ?, corps = ? WHERE niveau = ?",
                               (objet, corps, niveau))
            db.commit()
            flash("Modèles de courrier enregistrés.", "succes")
            return redirect(url_for("modeles"))
        if request.args.get("reinitialiser"):
            niveau = request.args.get("reinitialiser", type=int)
            if niveau in dbmod.MODELES_DEFAUT:
                objet, corps = dbmod.MODELES_DEFAUT[niveau]
                db.execute("UPDATE modeles SET objet = ?, corps = ? WHERE niveau = ?",
                           (objet, corps, niveau))
                db.commit()
                flash(f"Modèle « {NIVEAUX[niveau]} » réinitialisé.", "succes")
            return redirect(url_for("modeles"))
        rows = db.execute("SELECT * FROM modeles ORDER BY niveau").fetchall()
        return render_template("modeles.html", modeles=rows, variables=VARIABLES_DISPONIBLES)

    @app.route("/parametres", methods=["GET", "POST"])
    def parametres():
        if request.method == "POST":
            db = get_db()
            erreurs = []
            for cle in dbmod.PARAMETRES_DEFAUT:
                valeur = request.form.get(cle, "").strip()
                if cle.startswith(("delai_", "taux_", "indemnite")):
                    try:
                        nombre = parse_montant(valeur)
                        if nombre < 0:
                            raise ValueError
                        valeur = str(int(nombre)) if cle.startswith("delai_") else str(nombre)
                    except ValueError:
                        erreurs.append(f"Valeur numérique invalide pour « {cle} ».")
                        continue
                db.execute("INSERT OR REPLACE INTO parametres (cle, valeur) VALUES (?, ?)",
                           (cle, valeur))
            db.commit()
            for e in erreurs:
                flash(e, "erreur")
            if not erreurs:
                flash("Paramètres enregistrés.", "succes")
            return redirect(url_for("parametres"))
        return render_template("parametres.html", p=get_parametres())


# --------------------------------------------------------------------------- import CSV

COLONNES_ALIAS = {
    "numero": {"numero", "numéro", "n°", "facture", "num_facture", "numero_facture", "no"},
    "client": {"client", "nom_client", "raison_sociale", "nom"},
    "date_emission": {"date_emission", "date émission", "date", "date_facture",
                      "date d'émission"},
    "date_echeance": {"date_echeance", "échéance", "echeance", "date échéance",
                      "date_échéance"},
    "montant_ttc": {"montant_ttc", "montant", "ttc", "total_ttc", "montant ttc"},
    "montant_paye": {"montant_paye", "payé", "paye", "regle", "réglé", "montant payé"},
}


def importer_csv(texte):
    """Importe des factures. Crée les clients absents. Ignore les numéros déjà présents."""
    try:
        dialecte = csv.Sniffer().sniff(texte.split("\n", 1)[0], delimiters=";,\t")
    except csv.Error:
        class dialecte(csv.excel):
            delimiter = ";"
    lecteur = csv.reader(io.StringIO(texte), dialecte)
    try:
        entetes = [h.strip().lower() for h in next(lecteur)]
    except StopIteration:
        return {"importees": 0, "ignorees": 0, "clients_crees": 0,
                "erreurs": ["Fichier vide."]}
    index = {}
    for cle, alias in COLONNES_ALIAS.items():
        for i, h in enumerate(entetes):
            if h in alias:
                index[cle] = i
                break
    manquantes = [c for c in ("numero", "client", "date_echeance", "montant_ttc")
                  if c not in index]
    if manquantes:
        return {"importees": 0, "ignorees": 0, "clients_crees": 0,
                "erreurs": [f"Colonne(s) manquante(s) : {', '.join(manquantes)}"]}

    db = get_db()
    res = {"importees": 0, "ignorees": 0, "clients_crees": 0, "erreurs": []}
    for n, ligne in enumerate(lecteur, start=2):
        if not any(c.strip() for c in ligne):
            continue

        def val(cle):
            i = index.get(cle)
            return ligne[i].strip() if i is not None and i < len(ligne) else ""
        try:
            numero = val("numero")
            nom_client = val("client")
            if not numero or not nom_client:
                raise ValueError("numéro ou client vide")
            echeance = parse_date(val("date_echeance"))
            emission = parse_date(val("date_emission")) if val("date_emission") else echeance
            ttc = parse_montant(val("montant_ttc"))
            paye = parse_montant(val("montant_paye")) if val("montant_paye") else 0.0
        except ValueError as e:
            res["erreurs"].append(f"Ligne {n} : {e}")
            continue
        if db.execute("SELECT 1 FROM factures WHERE numero = ?", (numero,)).fetchone():
            res["ignorees"] += 1
            continue
        client = db.execute("SELECT id FROM clients WHERE nom = ?", (nom_client,)).fetchone()
        if client is None:
            client_id = db.execute("INSERT INTO clients (nom) VALUES (?)",
                                   (nom_client,)).lastrowid
            res["clients_crees"] += 1
        else:
            client_id = client["id"]
        db.execute(
            """INSERT INTO factures (numero, client_id, date_emission, date_echeance,
                                     montant_ttc, montant_paye) VALUES (?, ?, ?, ?, ?, ?)""",
            (numero, client_id, emission.isoformat(), echeance.isoformat(), ttc,
             min(paye, ttc)))
        res["importees"] += 1
    db.commit()
    return res
