# SY Communications · Quote survey app: setup

A branded 2-minute survey that customers open from the survey email sent by Prospect Engine and Lead Revival.

When a customer presses **Send my answers**, the app:

1. writes the answers onto their Zoho lead, with a note and the tag "Survey completed";
2. emails them a branded copy from Zoho with a PDF of their answers attached (logged on the lead);
3. emails you an alert (optional);
4. saves the response to the shared GitHub data repo, so Prospect Engine, Lead Revival and Lead Overview can count it.

The customer then sees a thank-you page with their answers, a PDF download and "what happens next".

## 1. New GitHub repo

Create a new private repo, for example `sy-quote-survey`, and upload everything in this folder. That includes the hidden `.streamlit` and `.github` folders:

```
app.py
inter_font.py
requirements.txt
.streamlit/config.toml
.github/workflows/keep-awake.yml
```

## 2. Deploy on Streamlit Community Cloud

1. Go to share.streamlit.io → **Create app** → pick the repo, branch `main`, file `app.py`.
2. Under **Advanced settings**, choose a nice address, e.g. `sycomms-quote` (giving `https://sycomms-quote.streamlit.app`).
3. Make the app **public** (Settings → Sharing → "This app is public"), so customers can open it without a login.

## 3. Secrets (App settings → Secrets)

```
# Zoho: its own Self Client refresh token (use the key helper in step 4)
ZOHO_CLIENT_ID = "1000.xxxx"
ZOHO_CLIENT_SECRET = "xxxx"
ZOHO_REFRESH_TOKEN = ""

# Signs the links. Make up a long random phrase and put the SAME line in Prospect Engine and Lead Revival too.
SURVEY_SIGNING_KEY = "a-long-random-phrase-only-you-know"

# Copies go from this address (it must be a verified From address in Zoho)
SURVEY_FROM = "hello@sycomms.co.uk"
# Optional: where the "survey completed" alert goes
SURVEY_ALERT_TO = "you@sycomms.co.uk"

# Same data repo and token the other apps use
GITHUB_TOKEN = "github_pat_xxxx"
GITHUB_REPO = "sammyatt2010-hub/prospect-engine-data"

# For the admin page and test links
ADMIN_PASSWORD = "choose-one"
APP_URL = "https://sycomms-quote.streamlit.app"

# Optional
# DEMO_BOOKING_URL = "https://..."    adds a "Book a time" button on the thank-you page
# ZOHO_ORG = "org20123456"            makes the alert's Zoho link open the right organisation
# SENDER_NAME = "Sam ..."             name on the customer's copy
```

## 4. Connect Zoho (key helper)

1. Open `https://sycomms-quote.streamlit.app/?admin=1` and enter the admin password.
2. In the **Zoho key helper** box, copy the scope shown.
3. In api-console.zoho.eu, open your Self Client → **Generate Code**, paste the scope, choose 10 minutes and click Create.
4. Paste the code into the helper straight away and click **Get my refresh token**.
5. Copy the `ZOHO_REFRESH_TOKEN = "..."` line into Secrets, save, and reboot the app.

The **Status** box on the admin page should now show green ticks for:

- Zoho
- the survey fields on Leads (it finds the fields your webform set-up made, by name)
- Lead Source "Survey form"
- the From address
- the stats repo
- signed links

## 5. Point Prospect Engine and Lead Revival at it

In **both** apps' Secrets:

```
SURVEY_FORM_URL = "https://sycomms-quote.streamlit.app"
SURVEY_SIGNING_KEY = "the same phrase as above"
```

Save and reboot both. Survey emails now carry a personal link that fills in the customer's details. Their answers land on that exact lead, with no matching by email needed.

## 6. Lead Overview

Nothing new is needed if it already uses the same `GITHUB_REPO`. A "Quote surveys completed" source appears in the sidebar. When there's activity, Surveys sent / Surveys completed totals appear under the main totals. If the survey app saves to a different repo, add `SV_GITHUB_REPO` (and `SV_GITHUB_TOKEN`).

## 7. Keep it awake (no "wake this app up" screen)

Free Streamlit apps go to sleep when nobody visits them for a while. The included GitHub Action visits the app every 6 hours and presses the wake button if needed.

1. In the survey repo on GitHub: **Settings → Secrets and variables → Actions → Variables → New repository variable**.
2. Name it `SURVEY_APP_URL`, with the value `https://sycomms-quote.streamlit.app`.
3. **Actions** tab → "Keep survey app awake" → **Run workflow** once to check it works.

## 8. Test

1. On the admin page, fill in **Test link** with your own test lead's Zoho ID and email, and open the link.
2. Submit the form. Check that the lead has the answers, the note and the "Survey completed" tag, and that the copy arrived with its PDF.
3. In Prospect Engine → 📋 Survey follow-ups, the lead shows ✅ Completed.

## The webform

You can keep the CRM webform as a backup. Once this app is live, the apps' emails no longer link to the webform. The 🧹 Tidy up button still clears any spare leads the webform made. Leads this app creates (for links someone forwarded) are made with workflows switched off, so your webform function never acts on them.
