import io
from datetime import date

import pytest

from relances import create_app
from relances.db import get_db, get_parametres
from relances.logique import (construire_ligne, format_euro, parse_date, parse_montant,
                              remplir_modele)

AUJOURDHUI = "2026-11-25"


@pytest.fixture
def app(tmp_path):
    return create_app({"DATABASE": str(tmp_path / "test.db"), "TESTING": True,
                       "AUJOURDHUI": AUJOURDHUI})


@pytest.fixture
def client(app):
    return app.test_client()


def creer_client(client, nom="ACME", type_="professionnel"):
    r = client.post("/clients/nouveau", data={"nom": nom, "type": type_,
                                              "adresse": "1 rue A", "code_postal": "75001",
                                              "ville": "Paris"})
    assert r.status_code == 302
    return int(r.headers["Location"].rsplit("/", 1)[1])


def creer_facture(client, cid, numero, echeance, ttc, paye="0"):
    r = client.post("/factures/nouvelle", data={
        "numero": numero, "client_id": cid, "date_emission": "2026-08-01",
        "date_echeance": echeance, "montant_ttc": ttc, "montant_paye": paye})
    assert r.status_code == 302, r.data


# ----------------------------------------------------------------- utilitaires

def test_parse_montant():
    assert parse_montant("1 234,56 €") == 1234.56
    assert parse_montant("1.234,56") == 1234.56
    assert parse_montant("99.9") == 99.9
    with pytest.raises(ValueError):
        parse_montant("abc")


def test_parse_date():
    assert parse_date("25/11/2026") == date(2026, 11, 25)
    assert parse_date("2026-11-25") == date(2026, 11, 25)
    with pytest.raises(ValueError):
        parse_date("32/13/2026")


def test_format_euro():
    assert format_euro(1234.5) == "1 234,50\xa0€"


def test_remplir_modele_echappe_le_html():
    html = remplir_modele("Bonjour {client_nom}\n\n<b>x</b> {inconnue}",
                          {"client_nom": "<script>"})
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;" in html
    assert "{inconnue}" in html
    assert html.count("<p>") == 2


def test_penalites_pro_et_particulier(app):
    with app.app_context():
        params = get_parametres()
        row = {"id": 1, "numero": "F1", "date_emission": "2026-01-01",
               "date_echeance": "2026-01-01", "montant_ttc": 1000.0, "montant_paye": 0.0,
               "litige": 0}
        today = date(2027, 1, 1)  # 365 jours de retard
        pro = construire_ligne(row, "professionnel", params, today)
        assert pro.jours_retard == 365
        assert pro.penalites == 120.0  # 12 %
        assert pro.indemnite == 40.0
        part = construire_ligne(row, "particulier", params, today)
        assert part.penalites == 80.0
        assert part.indemnite == 0.0


# ----------------------------------------------------------------- parcours

def test_pages_accessibles(client):
    for url in ["/", "/clients", "/factures", "/factures?filtre=toutes", "/relances",
                "/modeles", "/parametres", "/factures/import", "/clients/nouveau",
                "/factures/nouvelle", "/factures/export.csv"]:
        assert client.get(url).status_code == 200, url


def test_cycle_complet_de_relance(client, app):
    cid = creer_client(client)
    creer_facture(client, cid, "F-001", "2026-11-01", "1 000,00")  # 24 j de retard
    creer_facture(client, cid, "F-002", "2026-12-15", "500")       # non échue

    page = client.get("/").get_data(as_text=True)
    assert "Relance amiable" in page and "1 relance(s) à envoyer" not in page

    # Préparation : aperçu avec uniquement la facture échue
    apercu = client.get(f"/clients/{cid}/relance").get_data(as_text=True)
    assert "F-001" in apercu and "Objet : Relance amiable" in apercu

    # Envoi en lot
    r = client.post("/relances/lot")
    assert r.status_code == 302 and "/relances/impression" in r.headers["Location"]
    impression = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Relance amiable" in impression and "ACME" in impression

    with app.app_context():
        rel = get_db().execute("SELECT * FROM relances").fetchall()
        assert len(rel) == 1 and rel[0]["niveau"] == 1 and rel[0]["montant_du"] == 1000
        liens = get_db().execute("SELECT facture_id FROM relance_factures").fetchall()
        assert len(liens) == 1

    # Pas de nouvelle relance tant que le délai n'est pas écoulé
    r = client.post("/relances/lot", follow_redirects=True)
    assert "Aucune relance" in r.get_data(as_text=True)

    # 15 jours plus tard → seconde relance
    app.config["AUJOURDHUI"] = "2026-12-10"
    r = client.post("/relances/lot")
    assert r.status_code == 302
    # puis mise en demeure
    app.config["AUJOURDHUI"] = "2026-12-26"
    r = client.post("/relances/lot")
    texte = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "MISE EN DEMEURE" in texte
    assert "Lettre recommandée avec accusé de réception" in texte
    assert "indemnité forfaitaire" in texte

    # puis contentieux : plus de courrier automatique
    app.config["AUJOURDHUI"] = "2027-01-10"
    tdb = client.get("/").get_data(as_text=True)
    assert "Contentieux" in tdb
    r = client.post("/relances/lot", follow_redirects=True)
    assert "Aucune relance" in r.get_data(as_text=True)


def test_paiement_solde_supprime_la_relance(client, app):
    cid = creer_client(client)
    creer_facture(client, cid, "F-010", "2026-10-01", "300")
    with app.app_context():
        fid = get_db().execute("SELECT id FROM factures").fetchone()[0]
    client.post(f"/factures/{fid}/paiement", data={"montant": "100"})
    with app.app_context():
        assert get_db().execute("SELECT montant_paye FROM factures").fetchone()[0] == 100
    client.post(f"/factures/{fid}/paiement", data={"solde": "1"})
    with app.app_context():
        assert get_db().execute("SELECT montant_paye FROM factures").fetchone()[0] == 300
    r = client.post("/relances/lot", follow_redirects=True)
    assert "Aucune relance" in r.get_data(as_text=True)


def test_facture_en_litige_exclue(client):
    cid = creer_client(client)
    client.post("/factures/nouvelle", data={
        "numero": "F-L", "client_id": cid, "date_emission": "2026-08-01",
        "date_echeance": "2026-09-01", "montant_ttc": "100", "litige": "1"})
    r = client.post("/relances/lot", follow_redirects=True)
    assert "Aucune relance" in r.get_data(as_text=True)


def test_relance_manuelle_avec_selection(client, app):
    cid = creer_client(client, "Mme Durand", "particulier")
    creer_facture(client, cid, "F-100", "2026-10-01", "200")
    creer_facture(client, cid, "F-101", "2026-10-05", "300")
    with app.app_context():
        f100 = get_db().execute("SELECT id FROM factures WHERE numero='F-100'").fetchone()[0]
    r = client.post(f"/clients/{cid}/relance", data={
        "factures": [f100], "niveau": "3", "date": "2026-11-25",
        "mode": "Lettre recommandée avec AR", "reference": "1A 234 567 8901 2"})
    assert r.status_code == 302
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "F-100" in page and "F-101" not in page
    assert "1A 234 567 8901 2" in page
    # particulier : pas d'indemnité forfaitaire dans la décomposition
    assert "indemnité forfaitaire pour frais de recouvrement" not in page
    assert "intérêts de retard" in page


def test_import_csv(client, app):
    contenu = ("numero;client;date_emission;date_echeance;montant_ttc;montant_paye\n"
               "F-I1;Client Import;01/09/2026;01/10/2026;1 250,00;0\n"
               "F-I2;Client Import;15/09/2026;15/10/2026;480,50;100\n"
               "F-I3;Autre;mauvaise date;15/10/2026;10;0\n")
    r = client.post("/factures/import", data={
        "fichier": (io.BytesIO(contenu.encode("utf-8")), "factures.csv")},
        content_type="multipart/form-data")
    texte = r.get_data(as_text=True)
    assert "<strong>2</strong> facture(s) importée(s)" in texte
    assert "Ligne 4" in texte
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
    # Ré-import : doublons ignorés
    r = client.post("/factures/import", data={
        "fichier": (io.BytesIO(contenu.encode("utf-8")), "factures.csv")},
        content_type="multipart/form-data")
    assert "2 facture(s) ignorée(s)" in r.get_data(as_text=True)


def test_modeles_et_parametres_modifiables(client):
    client.post("/parametres", data={"societe_nom": "Société Test", "delai_niveau_1": "0",
                                     "delai_niveau_2": "15", "delai_niveau_3": "15",
                                     "delai_paiement_courrier": "8",
                                     "taux_penalites_pro": "12", "taux_interet_particulier": "8",
                                     "indemnite_forfaitaire": "40"})
    data = {f"objet_{n}": f"Objet {n}" for n in (1, 2, 3)}
    data.update({f"corps_{n}": f"Bonjour {{client_nom}}, vous devez {{total_du}}." for n in (1, 2, 3)})
    client.post("/modeles", data=data)
    cid = creer_client(client)
    creer_facture(client, cid, "F-M", "2026-11-24", "50")
    page = client.get(f"/clients/{cid}/relance").get_data(as_text=True)
    assert "Bonjour ACME, vous devez 50,00" in page
    assert "Société Test" in page


def test_unicite_numero_facture(client):
    cid = creer_client(client)
    creer_facture(client, cid, "F-X", "2026-11-01", "10")
    r = client.post("/factures/nouvelle", data={
        "numero": "F-X", "client_id": cid, "date_emission": "2026-08-01",
        "date_echeance": "2026-09-01", "montant_ttc": "10"})
    assert r.status_code == 200
    assert "existe déjà" in r.get_data(as_text=True)
