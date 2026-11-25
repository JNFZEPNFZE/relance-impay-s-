"""Lance l'application : python run.py  (puis ouvrir http://127.0.0.1:5000)

Options :
  --demo        charge des clients et factures d'exemple (si la base est vide)
  --port 8080   change le port d'écoute
"""
import argparse
from datetime import date, timedelta

from relances import create_app
from relances.db import get_db


def charger_demo():
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM factures").fetchone()[0]:
        print("La base contient déjà des factures : données de démonstration non chargées.")
        return
    t = date.today()
    clients = [
        ("Boulangerie Martin", "professionnel", "M. Martin", "12 rue du Four", "69002", "Lyon"),
        ("Garage Durand SARL", "professionnel", "Service comptabilité", "5 avenue de la Gare", "33000", "Bordeaux"),
        ("Mme Sophie Bernard", "particulier", "", "8 impasse des Lilas", "44000", "Nantes"),
    ]
    ids = []
    for nom, typ, contact, adr, cp, ville in clients:
        ids.append(db.execute(
            "INSERT INTO clients (nom, type, contact, adresse, code_postal, ville) VALUES (?,?,?,?,?,?)",
            (nom, typ, contact, adr, cp, ville)).lastrowid)
    factures = [
        ("F-DEMO-001", ids[0], 75, 45, 1250.00, 0),
        ("F-DEMO-002", ids[0], 40, 10, 380.40, 0),
        ("F-DEMO-003", ids[1], 120, 90, 4820.00, 1000),
        ("F-DEMO-004", ids[2], 50, 20, 690.00, 0),
        ("F-DEMO-005", ids[2], 20, -10, 150.00, 0),
    ]
    for num, cid, emis, ech, ttc, paye in factures:
        db.execute(
            "INSERT INTO factures (numero, client_id, date_emission, date_echeance, montant_ttc, montant_paye)"
            " VALUES (?,?,?,?,?,?)",
            (num, cid, (t - timedelta(days=emis)).isoformat(), (t - timedelta(days=ech)).isoformat(), ttc, paye))
    # Le garage a déjà reçu une 1re relance il y a 20 jours
    rid = db.execute(
        "INSERT INTO relances (client_id, niveau, date_envoi, mode_envoi, montant_du, contenu_html)"
        " VALUES (?, 1, ?, 'Courrier simple', 3820, '<p>(courrier de démonstration)</p>')",
        (ids[1], (t - timedelta(days=20)).isoformat())).lastrowid
    db.execute("INSERT INTO relance_factures VALUES (?, (SELECT id FROM factures WHERE numero='F-DEMO-003'))", (rid,))
    db.commit()
    print("Données de démonstration chargées.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    app = create_app()
    if args.demo:
        with app.app_context():
            charger_demo()
    print(f"Base de données : {app.config['DATABASE']}")
    app.run(host=args.host, port=args.port, debug=False)
