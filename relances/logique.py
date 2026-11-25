"""Règles métier : retards, pénalités, niveau de relance suggéré, rédaction des courriers."""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from markupsafe import Markup, escape

from .db import NIVEAUX

NIVEAU_CONTENTIEUX = 4


# --------------------------------------------------------------------------- formats

def parse_date(valeur):
    """Accepte AAAA-MM-JJ, JJ/MM/AAAA ou JJ-MM-AAAA. Retourne un objet date."""
    if isinstance(valeur, date):
        return valeur
    valeur = (valeur or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(valeur, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Date invalide : « {valeur} »")


def parse_montant(valeur):
    """Accepte « 1 234,56 », « 1234.56 », « 1 234,56 € »..."""
    if isinstance(valeur, (int, float)):
        return float(valeur)
    texte = (valeur or "").strip().replace("€", "").replace(" ", "").replace("\xa0", "")
    texte = texte.replace(" ", "")
    if not texte:
        return 0.0
    if "," in texte and "." in texte:
        texte = texte.replace(".", "")  # 1.234,56
    texte = texte.replace(",", ".")
    try:
        return round(float(texte), 2)
    except ValueError:
        raise ValueError(f"Montant invalide : « {valeur} »") from None


def format_euro(montant):
    texte = f"{montant:,.2f}"
    return texte.replace(",", " ").replace(".", ",") + "\xa0€"


def format_date(valeur):
    if not valeur:
        return ""
    return parse_date(valeur).strftime("%d/%m/%Y")


MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def format_date_longue(valeur):
    d = parse_date(valeur)
    jour = "1er" if d.day == 1 else str(d.day)
    return f"{jour} {MOIS[d.month - 1]} {d.year}"


# --------------------------------------------------------------------------- calculs

@dataclass
class LigneFacture:
    id: int
    numero: str
    date_emission: date
    date_echeance: date
    montant_ttc: float
    montant_paye: float
    litige: bool
    niveau_atteint: int = 0
    jours_retard: int = 0
    penalites: float = 0.0
    indemnite: float = 0.0

    @property
    def reste_du(self):
        return round(max(0.0, self.montant_ttc - self.montant_paye), 2)

    @property
    def payee(self):
        return self.reste_du <= 0.005


def construire_ligne(row, client_type, params, aujourd_hui, niveau_atteint=0):
    ligne = LigneFacture(
        id=row["id"],
        numero=row["numero"],
        date_emission=parse_date(row["date_emission"]),
        date_echeance=parse_date(row["date_echeance"]),
        montant_ttc=row["montant_ttc"],
        montant_paye=row["montant_paye"],
        litige=bool(row["litige"]),
        niveau_atteint=niveau_atteint or 0,
    )
    ligne.jours_retard = max(0, (aujourd_hui - ligne.date_echeance).days)
    if not ligne.payee and ligne.jours_retard > 0:
        if client_type == "particulier":
            taux = float(params["taux_interet_particulier"])
        else:
            taux = float(params["taux_penalites_pro"])
            ligne.indemnite = float(params["indemnite_forfaitaire"])
        ligne.penalites = round(ligne.reste_du * taux / 100 * ligne.jours_retard / 365, 2)
    return ligne


@dataclass
class Situation:
    """Situation d'un client vis-à-vis de ses factures échues impayées."""
    factures: list = field(default_factory=list)
    niveau_actuel: int = 0
    date_derniere_relance: date | None = None
    niveau_suggere: int | None = None
    date_prochaine_action: date | None = None

    @property
    def total_du(self):
        return round(sum(f.reste_du for f in self.factures), 2)

    @property
    def retard_max(self):
        return max((f.jours_retard for f in self.factures), default=0)

    @property
    def a_relancer(self):
        return self.niveau_suggere is not None

    @property
    def prochaine_action(self):
        niveau = self.niveau_suggere
        if niveau is None:
            niveau = min(self.niveau_actuel + 1, NIVEAU_CONTENTIEUX)
        if niveau == NIVEAU_CONTENTIEUX:
            return "Contentieux / injonction de payer"
        return NIVEAUX[niveau]


def calculer_situation(factures_echues, derniere_relance, params, aujourd_hui):
    """
    factures_echues : liste de LigneFacture échues, impayées et hors litige.
    derniere_relance : (niveau, date) de la relance la plus récente portant sur
                       ces factures, ou None.
    """
    sit = Situation(factures=factures_echues)
    if not factures_echues:
        return sit
    sit.niveau_actuel = max(f.niveau_atteint for f in factures_echues)
    if derniere_relance:
        sit.date_derniere_relance = parse_date(derniere_relance[1])

    if sit.niveau_actuel == 0:
        echeance_la_plus_ancienne = min(f.date_echeance for f in factures_echues)
        sit.date_prochaine_action = echeance_la_plus_ancienne + timedelta(
            days=int(params["delai_niveau_1"]))
        prochain = 1
    else:
        prochain = min(sit.niveau_actuel + 1, NIVEAU_CONTENTIEUX)
        cle = f"delai_niveau_{prochain}" if prochain <= 3 else "delai_paiement_courrier"
        base = sit.date_derniere_relance or aujourd_hui
        sit.date_prochaine_action = base + timedelta(days=int(params[cle]))

    if aujourd_hui >= sit.date_prochaine_action:
        sit.niveau_suggere = prochain
    return sit


# --------------------------------------------------------------------------- courriers

def tableau_factures_html(lignes, avec_penalites):
    entetes = ["Facture", "Date", "Échéance", "Montant TTC", "Déjà réglé", "Reste dû",
               "Retard"]
    if avec_penalites:
        entetes += ["Pénalités", "Indemnité"]
    html = ['<table class="tableau-factures"><thead><tr>']
    html += [f"<th>{escape(e)}</th>" for e in entetes]
    html.append("</tr></thead><tbody>")
    for f in lignes:
        cellules = [
            escape(f.numero), format_date(f.date_emission), format_date(f.date_echeance),
            format_euro(f.montant_ttc), format_euro(f.montant_paye), format_euro(f.reste_du),
            f"{f.jours_retard} j",
        ]
        if avec_penalites:
            cellules += [format_euro(f.penalites), format_euro(f.indemnite)]
        html.append("<tr>" + "".join(f"<td>{c}</td>" for c in cellules) + "</tr>")
    total = sum(f.reste_du for f in lignes)
    html.append(f'</tbody><tfoot><tr><td colspan="5">Total</td><td>{format_euro(total)}</td>'
                f'<td></td>')
    if avec_penalites:
        html.append(f"<td>{format_euro(sum(f.penalites for f in lignes))}</td>"
                    f"<td>{format_euro(sum(f.indemnite for f in lignes))}</td>")
    html.append("</tr></tfoot></table>")
    return Markup("".join(html))


def variables_courrier(client, lignes, niveau, date_courrier, params, date_derniere_relance):
    date_courrier = parse_date(date_courrier)
    delai = int(params["delai_paiement_courrier"])
    total_du = sum(f.reste_du for f in lignes)
    total_penalites = sum(f.penalites for f in lignes)
    total_indemnites = sum(f.indemnite for f in lignes)
    pro = client["type"] != "particulier"
    mention = ""
    if pro:
        mention = (f" ainsi que d'une indemnité forfaitaire pour frais de recouvrement de "
                   f"{format_euro(float(params['indemnite_forfaitaire']))} par facture "
                   f"(article L441-10 du Code de commerce)")
    detail = [f"- principal restant dû : {format_euro(total_du)}",
              f"- {'pénalités' if pro else 'intérêts'} de retard arrêtés au "
              f"{format_date_longue(date_courrier)} : {format_euro(total_penalites)}"]
    if pro:
        detail.append(f"- indemnité forfaitaire pour frais de recouvrement : "
                      f"{format_euro(total_indemnites)}")
    return {
        "client_nom": client["nom"],
        "client_contact": client["contact"],
        "societe_nom": params["societe_nom"],
        "nb_factures": str(len(lignes)),
        "total_du": format_euro(total_du),
        "total_penalites": format_euro(total_penalites),
        "total_indemnites": format_euro(total_indemnites),
        "total_reclame": format_euro(total_du + total_penalites + total_indemnites),
        "detail_sommes": "\n".join(detail),
        "date_courrier": format_date_longue(date_courrier),
        "date_limite": format_date_longue(date_courrier + timedelta(days=delai)),
        "delai_paiement": str(delai),
        "date_derniere_relance": (format_date_longue(date_derniere_relance)
                                  if date_derniere_relance else "—"),
        "mention_indemnite": mention,
        "tableau_factures": tableau_factures_html(lignes, avec_penalites=niveau >= 3),
    }


PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def remplir_modele(texte, variables):
    """
    Remplace les {variables} d'un modèle saisi par l'utilisateur.
    Le texte est échappé (pas de HTML possible) ; seules les variables
    de type Markup (le tableau) sont insérées telles quelles.
    Les paragraphes sont séparés par une ligne vide.
    """
    def remplacer(m):
        nom = m.group(1)
        if nom not in variables:
            return m.group(0)
        return str(escape(variables[nom]))

    echappe = str(escape(texte.replace("\r\n", "\n")))
    rempli = PLACEHOLDER.sub(remplacer, echappe)
    blocs = [b.strip() for b in re.split(r"\n\s*\n", rempli) if b.strip()]
    html = []
    for bloc in blocs:
        if bloc.startswith("<table"):
            html.append(bloc)
        else:
            html.append("<p>" + bloc.replace("\n", "<br>") + "</p>")
    return Markup("\n".join(html))


VARIABLES_DISPONIBLES = {
    "client_nom": "Nom du client",
    "client_contact": "Interlocuteur chez le client",
    "societe_nom": "Nom de votre société",
    "nb_factures": "Nombre de factures concernées",
    "tableau_factures": "Tableau détaillé des factures (à placer seul dans un paragraphe)",
    "total_du": "Total du principal restant dû",
    "total_penalites": "Total des pénalités / intérêts de retard",
    "total_indemnites": "Total des indemnités forfaitaires (40 € / facture, professionnels)",
    "total_reclame": "Principal + pénalités + indemnités",
    "detail_sommes": "Décomposition des sommes réclamées (liste)",
    "date_courrier": "Date du courrier",
    "date_limite": "Date limite de paiement (date du courrier + délai)",
    "delai_paiement": "Délai de paiement accordé (en jours)",
    "date_derniere_relance": "Date de la précédente relance",
    "mention_indemnite": "Mention légale sur l'indemnité forfaitaire (vide pour un particulier)",
}
