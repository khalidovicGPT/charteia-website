# Site Web Association Charte IA

Landing page minimaliste et bilingue pour charteia.org

## Structure

```
charteia-website/
├── fr/
│   └── index.html      # Version française (principale)
├── en/
│   └── index.html      # Version anglaise
└── vercel.json         # Configuration Vercel
```

## Caractéristiques

- ✅ Design minimal, fond blanc, typographie académique
- ✅ Bilingue FR/EN avec toggle
- ✅ SEO optimisé (hreflang, canonical, meta descriptions)
- ✅ Responsive mobile
- ✅ Formulaire de contact Formspree
- ✅ Liens DOI Zenodo
- ✅ Aucune image (contenu = design)

## Hébergement

**Plateforme** : Vercel
**Domaine** : charteia.org

## Déploiement

### 1. Configuration DNS (Hostinger)

Dans le panel Hostinger, ajouter ces enregistrements DNS :

| Type | Nom | Valeur |
|------|-----|--------|
| A | @ | 76.76.21.21 (IP Vercel) |
| CNAME | www | cname.vercel-dns.com |

### 2. Connexion Vercel

1. Aller sur https://vercel.com
2. Importer le projet GitHub (ou upload manuel)
3. Configurer le domaine custom : charteia.org
4. Vercel génère automatiquement le certificat SSL

### 3. Configuration Formspree (Formulaire)

1. Créer un compte sur https://formspree.io
2. Créer un nouveau formulaire avec l'email : contact@charteia.org
3. Récupérer le endpoint (ex: https://formspree.io/f/xnqkvnpr)
4. Remplacer dans les fichiers HTML :
   - `/fr/index.html` : action du formulaire
   - `/en/index.html` : action du formulaire

**Note** : Le formulaire actuel pointe vers `https://formspree.io/f/contact@charteia.org` — il faut remplacer par l'endpoint réel après création.

## URLs

- **FR** : https://charteia.org/fr/
- **EN** : https://charteia.org/en/
- **Racine** : Redirection vers /fr/ (FR par défaut)

## SEO

- Balises hreflang pour FR et EN
- URLs canoniques
- Meta descriptions optimisées
- Structure sémantique HTML5
- Liens DOI vers Zenodo

## Maintenance

Pour modifier le contenu, éditer directement les fichiers HTML dans `/fr/` et `/en/`.

---

*Créé par OptimusClaw — 10 avril 2026*
