"""Accès à la base SQLite : schéma, connexion et données par défaut."""
import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS parametres (
    cle    TEXT PRIMARY KEY,
    valeur TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nom           TEXT NOT NULL UNIQUE,
    type          TEXT NOT NULL DEFAULT 'professionnel'
                  CHECK (type IN ('professionnel', 'particulier')),
    contact       TEXT NOT NULL DEFAULT '',
    adresse       TEXT NOT NULL DEFAULT '',
    code_postal   TEXT NOT NULL DEFAULT '',
    ville         TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    telephone     TEXT NOT NULL DEFAULT '',
    notes         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS factures (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    numero         TEXT NOT NULL UNIQUE,
    client_id      INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    date_emission  TEXT NOT NULL,
    date_echeance  TEXT NOT NULL,
    montant_ttc    REAL NOT NULL CHECK (montant_ttc >= 0),
    montant_paye   REAL NOT NULL DEFAULT 0 CHECK (montant_paye >= 0),
    litige         INTEGER NOT NULL DEFAULT 0,
    notes          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS modeles (
    niveau  INTEGER PRIMARY KEY,
    libelle TEXT NOT NULL,
    objet   TEXT NOT NULL,
    corps   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relances (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    niveau       INTEGER NOT NULL,
    date_envoi   TEXT NOT NULL,
    mode_envoi   TEXT NOT NULL,
    reference    TEXT NOT NULL DEFAULT '',
    montant_du   REAL NOT NULL,
    contenu_html TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relance_factures (
    relance_id INTEGER NOT NULL REFERENCES relances(id) ON DELETE CASCADE,
    facture_id INTEGER NOT NULL REFERENCES factures(id) ON DELETE CASCADE,
    PRIMARY KEY (relance_id, facture_id)
);

CREATE INDEX IF NOT EXISTS idx_factures_client ON factures(client_id);
CREATE INDEX IF NOT EXISTS idx_relances_client ON relances(client_id);
"""

PARAMETRES_DEFAUT = {
    "societe_nom": "Ma Société SAS",
    "societe_adresse": "1 rue de l'Exemple",
    "societe_code_postal": "75001",
    "societe_ville": "Paris",
    "societe_siret": "",
    "societe_telephone": "",
    "societe_email": "",
    "societe_iban": "",
    "signataire": "Le service comptabilité",
    # Nombre de jours de retard après l'échéance avant la 1re relance
    "delai_niveau_1": "7",
    # Nombre de jours minimum entre deux relances successives
    "delai_niveau_2": "15",
    "delai_niveau_3": "15",
    # Délai laissé au client pour régler, indiqué dans le courrier
    "delai_paiement_courrier": "8",
    # Taux annuel des pénalités de retard entre professionnels (art. L441-10 C. com.)
    "taux_penalites_pro": "12.0",
    # Taux d'intérêt légal applicable aux particuliers
    "taux_interet_particulier": "8.0",
    # Indemnité forfaitaire pour frais de recouvrement (professionnels uniquement)
    "indemnite_forfaitaire": "40",
}

NIVEAUX = {
    1: "Relance amiable",
    2: "Seconde relance",
    3: "Mise en demeure",
}

MODELES_DEFAUT = {
    1: (
        "Relance amiable - facture(s) impayée(s)",
        """Madame, Monsieur,

Sauf erreur ou omission de notre part, nous constatons que le règlement des factures ci-dessous, arrivées à échéance, ne nous est pas encore parvenu :

{tableau_factures}

Il s'agit probablement d'un simple oubli. Nous vous remercions de bien vouloir procéder au règlement de la somme de {total_du} avant le {date_limite}.

Si votre paiement a été effectué entre-temps, nous vous prions de ne pas tenir compte de ce courrier.

Nous vous prions d'agréer, Madame, Monsieur, l'expression de nos salutations distinguées.""",
    ),
    2: (
        "Seconde relance - facture(s) impayée(s)",
        """Madame, Monsieur,

Malgré notre précédente relance du {date_derniere_relance}, nous restons à ce jour sans règlement de votre part pour les factures suivantes :

{tableau_factures}

Nous vous demandons de bien vouloir régulariser la somme de {total_du} au plus tard le {date_limite}.

Nous vous rappelons que tout retard de paiement entraîne de plein droit l'application de pénalités de retard{mention_indemnite}.

En cas de difficulté, nous vous invitons à prendre contact avec nous rapidement afin de trouver une solution amiable.

Nous vous prions d'agréer, Madame, Monsieur, l'expression de nos salutations distinguées.""",
    ),
    3: (
        "MISE EN DEMEURE DE PAYER",
        """Madame, Monsieur,

Malgré nos relances successives, dont la dernière en date du {date_derniere_relance}, vous ne vous êtes toujours pas acquitté(e) des sommes dues au titre des factures suivantes :

{tableau_factures}

En conséquence, par la présente, nous vous mettons en demeure de nous régler la somme totale de {total_reclame}, se décomposant comme suit :
{detail_sommes}

et ce, dans un délai de {delai_paiement} jours à compter de la réception du présent courrier, soit au plus tard le {date_limite}.

À défaut de règlement dans ce délai, nous nous verrons contraints d'engager, sans autre avis, une procédure judiciaire de recouvrement (injonction de payer), dont les frais resteront à votre charge.

La présente vaut mise en demeure au sens de l'article 1344 du Code civil et fait courir les intérêts moratoires.

Nous vous prions d'agréer, Madame, Monsieur, l'expression de nos salutations distinguées.""",
    ),
}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    for cle, valeur in PARAMETRES_DEFAUT.items():
        db.execute(
            "INSERT OR IGNORE INTO parametres (cle, valeur) VALUES (?, ?)", (cle, valeur)
        )
    for niveau, (objet, corps) in MODELES_DEFAUT.items():
        db.execute(
            "INSERT OR IGNORE INTO modeles (niveau, libelle, objet, corps) VALUES (?, ?, ?, ?)",
            (niveau, NIVEAUX[niveau], objet, corps),
        )
    db.commit()


def get_parametres():
    rows = get_db().execute("SELECT cle, valeur FROM parametres").fetchall()
    params = dict(PARAMETRES_DEFAUT)
    params.update({r["cle"]: r["valeur"] for r in rows})
    return params
