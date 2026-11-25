# Relances impayés

Logiciel web local pour suivre les factures impayées et produire les courriers de relance :
**relance amiable → seconde relance → mise en demeure → contentieux**.

## Fonctionnalités

- **Tableau de bord** : encours échu, factures en retard, clients à relancer aujourd'hui,
  prochaine étape et date prévue pour chaque client.
- **Clients** (professionnels ou particuliers) et **factures** : saisie, modification,
  enregistrement des paiements partiels ou du solde, marquage « en litige » (exclu des relances).
- **Import CSV** des factures depuis votre logiciel de facturation ou Excel ; **export CSV** des impayés.
- **Courriers de relance** à 3 niveaux, regroupant toutes les factures échues d'un client :
  - choix des factures, du niveau, de la date, du mode d'envoi (simple, recommandé AR, e-mail…) et du n° de recommandé ;
  - aperçu au format A4, adresse placée pour enveloppe à fenêtre ;
  - impression ou export PDF via le navigateur ;
  - **génération en lot** de toutes les relances du jour en un clic.
- **Calcul automatique des pénalités** à la date du courrier :
  - professionnels : pénalités au taux paramétré + indemnité forfaitaire de 40 € par facture (art. L441-10 C. com.) ;
  - particuliers : intérêts au taux légal, sans indemnité forfaitaire.
- **Historique** : chaque courrier envoyé est conservé à l'identique (réimpression possible).
- **Modèles de courrier modifiables** avec variables (`{total_du}`, `{date_limite}`, `{tableau_factures}`…).
- **Paramètres** : en-tête de votre société, IBAN, signature, délais entre relances, taux.

## Installation

Prérequis : Python 3.10 ou plus récent.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate
pip install -r requirements.txt
```

## Lancement

```bash
python run.py            # puis ouvrir http://127.0.0.1:5000
python run.py --demo     # idem, avec quelques clients et factures d'exemple
```

Les données sont stockées dans `instance/relances.db` (fichier SQLite à sauvegarder régulièrement).
Variable d'environnement `RELANCES_DB` pour choisir un autre emplacement.

## Utilisation conseillée

1. **Paramètres** : renseignez votre société, IBAN, signature et vérifiez les taux de pénalités
   (ils évoluent chaque semestre).
2. Saisissez ou **importez** vos factures.
3. Chaque jour (ou chaque semaine), ouvrez le **tableau de bord** puis cliquez sur
   « Générer les relances du jour », imprimez, postez.
4. Enregistrez les paiements reçus : les factures soldées sortent automatiquement du circuit de relance.
5. Après une mise en demeure restée sans réponse, le dossier passe en **Contentieux**
   (injonction de payer, commissaire de justice).

### Calendrier par défaut

| Étape | Déclenchement |
|---|---|
| Relance amiable | 7 jours après l'échéance |
| Seconde relance | 15 jours après la relance amiable |
| Mise en demeure (recommandé AR) | 15 jours après la seconde relance |
| Contentieux | 8 jours après la mise en demeure |

Tous ces délais sont modifiables dans **Paramètres**.

### Format d'import CSV

```
numero;client;date_emission;date_echeance;montant_ttc;montant_paye
F2026-001;Boulangerie Martin;01/09/2026;01/10/2026;1 250,00;0
```

## Tests

```bash
pytest
```

> Les modèles de courrier fournis sont des exemples : faites-les relire au besoin par votre
> conseil juridique, notamment la mise en demeure.
