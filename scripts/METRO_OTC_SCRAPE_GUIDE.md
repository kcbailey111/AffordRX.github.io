# Metro OTC Scrape Guide (AffordRX)

This document is the pilot plan for collecting **accurate OTC drug prices** across South Carolina metros without a GoodRx API key. Use **one anchor ZIP per metro** on GoodRx, save HTML pages manually, then run the scraper.

---

## Metro anchor ZIPs and folders

GoodRx prices are keyed by **ZIP + pharmacy chain**, not individual store addresses. Set the location ZIP once per metro, then apply those chain prices to every pharmacy in that area.

| Metro | Anchor ZIP | GoodRx location | Save folder |
|-------|------------|-----------------|-------------|
| **Greenville** | `29607` | Greenville, SC | `Drugs_To_Get_Greenville/` (existing) |
| **Columbia** | `29201` | Columbia, SC | `Drugs_To_Get_Columbia/` |
| **Charleston** | `29401` | Charleston, SC | `Drugs_To_Get_Charleston/` |
| **Myrtle Beach** | `29577` | Myrtle Beach, SC | `Drugs_To_Get_MyrtleBeach/` |
| **Spartanburg** | `29301` | Spartanburg, SC | `Drugs_To_Get_Spartanburg/` |

### Chains that matter most per metro

From `pharmacies.json` (301 locations statewide):

| Metro | Top chains to capture on each GoodRx page |
|-------|-------------------------------------------|
| Columbia | Publix, CVS, Walgreens, Target, Kroger, Sam's Club, Costco, Walmart |
| Charleston | Publix, Target, Walmart, Kroger, Costco, Sam's Club, CVS |
| Myrtle Beach | Walmart, Publix, Kroger, Sam's Club, Target, Costco, CVS |
| Greenville | Publix, Walmart, Target, Walgreens, CVS, Sam's Club, Costco |
| Spartanburg | Publix, Walmart, CVS, Walgreens, Target, Kroger |

---

## 10 OTC drugs per metro (same list everywhere)

Use the **exact same SKU** in every metro so regional differences are real—not from different strengths or bottle sizes.

| # | Drug (CSV `Name`) | GoodRx search | Baseline SKU | Filename example |
|---|-------------------|---------------|--------------|------------------|
| 1 | **Ibuprofen** | `ibuprofen` → **200 mg tablet, qty 30** | 200 mg, 30 tablets | `Ibuprofen 200mg 30ct GoodRx.html` |
| 2 | **Acetaminophen** | `acetaminophen` → **500 mg tablet, qty 30** | 500 mg, 30 tablets | `Acetaminophen 500mg 30ct GoodRx.html` |
| 3 | **Loratadine** | `loratadine` → **10 mg tablet, qty 30** | 10 mg, 30 tablets | `Loratadine 10mg 30ct GoodRx.html` |
| 4 | **Cetirizine** | `cetirizine` → **10 mg tablet, qty 30** | 10 mg, 30 tablets | `Cetirizine 10mg 30ct GoodRx.html` |
| 5 | **Omeprazole** | `omeprazole` → **20 mg delayed-release capsule, qty 14** | 20 mg, 14 capsules | `Omeprazole 20mg 14ct GoodRx.html` |
| 6 | **Famotidine** | `famotidine` → **20 mg tablet, qty 30** | 20 mg, 30 tablets | `Famotidine 20mg 30ct GoodRx.html` |
| 7 | **Naproxen** | `naproxen-sodium` → **220 mg tablet, qty 30** | 220 mg, 30 tablets | `Naproxen 220mg 30ct GoodRx.html` |
| 8 | **Aspirin** | `aspirin` → **81 mg tablet, qty 30** | 81 mg, 30 tablets | `Aspirin 81mg 30ct GoodRx.html` |
| 9 | **Loperamide** | `loperamide` → **2 mg capsule, qty 24** | 2 mg, 24 capsules | `Loperamide 2mg 24ct GoodRx.html` |
| 10 | **Diphenhydramine** | `diphenhydramine` → **25 mg tablet, qty 30** | 25 mg, 30 tablets | `Diphenhydramine 25mg 30ct GoodRx.html` |

### Per-metro checklist (50 page saves total)

Repeat for **each** metro—change only the ZIP and folder:

```
□ Set GoodRx location to [ZIP]
□ Save 10 HTML files into Drugs_To_Get_[Metro]/
□ Run scraper → [metro]_data.csv
```

| Metro | ZIP | Drugs 1–10 (all required) |
|-------|-----|---------------------------|
| **Columbia** | 29201 | Ibuprofen, Acetaminophen, Loratadine, Cetirizine, Omeprazole, Famotidine, Naproxen, Aspirin, Loperamide, Diphenhydramine |
| **Charleston** | 29401 | Same 10 |
| **Myrtle Beach** | 29577 | Same 10 |
| **Greenville** | 29607 | Same 10 (refresh existing data) |
| **Spartanburg** | 29301 | Same 10 |

---

## How to save each GoodRx page (accuracy tips)

1. Set **location ZIP** first (top of GoodRx).
2. Open the drug, then select **exact form, strength, and quantity** on the page.
3. Confirm the URL includes form/qty when possible (e.g. `.../ibuprofen?label_override=ibuprofen&form=tablet&dosage=200mg&quantity=30`).
4. Scroll until the **full pharmacy price list** is visible.
5. Save as **“Webpage, Complete”** (not “HTML only”) so `data-qa` spans are included.
6. Use the filename pattern above so drug name + SKU are obvious.

---

## Why the scraper may need changes

Current parser in `scripts/scraper.ipynb`:

- Finds `div` with class `pt-2`, uses **only the first** match.
- Reads `span[data-qa='seller-name']` and `span[data-qa='seller-price']` inside `li` elements.
- Extracts drug name from filename using **only the first word**.

### Problems that hurt accuracy today

| Issue | What goes wrong | Example |
|-------|-----------------|---------|
| **`div.pt-2` + first only** | Grabs wrong block if GoodRx adds promos above the list | Empty or partial CSV |
| **Drug name from filename** | Only first word used | `Plan B` → `Plan`; `Omega-3-Acid` breaks |
| **No ZIP / metro in output** | Can't tell which region a price belongs to | Columbia stores get wrong dataset |
| **No dosage/qty in CSV** | Can't verify SKU match | Omeprazole 14 vs 30 ct mixed up |
| **Pharmacy name junk** | Extra text in names | `PublixPay online` won't match `Publix` in `script.js` |
| **No chain normalization** | Fragile matching | `Target (CVS)` vs `Target` |
| **Price string only** | Promo artifacts | $2.00 Publix rows |
| **Single selector strategy** | GoodRx layout changes → silent failures | Whole drug missing from CSV |

---

## Scraper changes to make (recommended)

### 1. Parse by `data-qa` first; don't rely only on `div.pt-2`

```python
# BETTER: find ALL pharmacy rows anywhere on the page
for li in soup.find_all('li'):
    name_el = li.find('span', attrs={'data-qa': 'seller-name'})
    price_el = li.find('span', attrs={'data-qa': 'seller-price'})
    if name_el and price_el:
        ...
```

Add fallbacks if zero rows: `data-qa='pharmacy-name'`, `data-testid`, or JSON embedded in `<script>` tags.

### 2. Normalize pharmacy names before CSV

| Raw from GoodRx | Normalized for AffordRX |
|-----------------|-------------------------|
| `PublixPay online` | `Publix` |
| `Harris TeeterPay online` | `Harris Teeter` |
| `Walmart Neighborhood Market` | `Walmart` (or keep separate) |
| `Community, a Walgreens Pharmacy` | `Walgreens` |
| `Target (CVS)` | `Target (CVS)` |

`script.js` matches with `csvPharmacy.includes(targetPharmacy.split(' ')[0])`—normalization makes that reliable.

### 3. Richer CSV columns

```csv
Name,Pharmacy,Price,Metro,Zip,Dosage,Quantity,ScrapedAt,Source,SourceFile
Ibuprofen,Walmart,$5.66,columbia,29201,200mg,30,2026-06-08,goodrx,Ibuprofen 200mg 30ct GoodRx.html
```

### 4. Parse drug metadata from filename or manifest

**Filename pattern:** `{Name} {dosage} {qty}ct GoodRx.html`

**Or** `scrape_manifest.json` per folder:

```json
{
  "file": "Omeprazole 20mg 14ct GoodRx.html",
  "name": "Omeprazole",
  "dosage": "20mg",
  "quantity": 14,
  "metro": "columbia",
  "zip": "29201"
}
```

### 5. Extract ZIP from saved HTML (sanity check)

Search saved HTML for the location/ZIP GoodRx displays. Flag if ZIP ≠ expected anchor ZIP.

### 6. Deduplicate: one row per chain

If GoodRx lists the same chain twice, keep **one row per normalized chain**—usually the **lowest** price.

### 7. Validate row count before merging

Expect **6–10 pharmacies** per drug in SC metros. If you get 0–2 rows, mark the file failed—don't merge into production CSV.

### 8. Optional: SingleCare same ZIP

Scrape SingleCare for the same 10 drugs × same ZIP. Use `min(goodrx_price, singlecare_price)` per chain.

---

## Suggested folder layout

```
AffordRX.github.io/
├── Drugs_To_Get_Columbia/
│   ├── Ibuprofen 200mg 30ct GoodRx.html
│   ├── ...
│   └── scrape_manifest.json
├── Drugs_To_Get_Charleston/
├── Drugs_To_Get_MyrtleBeach/
├── Drugs_To_Get_Spartanburg/
├── Drugs_To_Get_Greenville/
├── columbia_data.csv
├── charleston_data.csv
├── myrtlebeach_data.csv
├── spartanburg_data.csv
├── greenville_data.csv
└── all_data.csv                    (statewide fallback)
```

---

## Pilot order (recommended)

1. **Columbia only** — 10 drugs, ZIP `29201`.
2. Compare output to `all_data.csv` chain-by-chain.
3. If omeprazole / famotidine / ibuprofen differ a lot → regional files are worth it.
4. If loratadine / cetirizine are within ~5% → those may share statewide data later.

**Known regional gaps (Greenville vs national):**

- **Omeprazole** — large differences (Walmart, Walgreens, Publix).
- **Ibuprofen** — Walgreens and Costco differ materially.
- **Loratadine** — mostly identical across regions.

---

## Quick reference card

```
METRO          ZIP     FOLDER
Columbia       29201   Drugs_To_Get_Columbia
Charleston     29401   Drugs_To_Get_Charleston
Myrtle Beach   29577   Drugs_To_Get_MyrtleBeach
Greenville     29607   Drugs_To_Get_Greenville
Spartanburg    29301   Drugs_To_Get_Spartanburg

DRUGS (all metros):
  Ibuprofen, Acetaminophen, Loratadine, Cetirizine, Omeprazole,
  Famotidine, Naproxen, Aspirin, Loperamide, Diphenhydramine

WORKFLOW:
  1. Set ZIP on GoodRx
  2. Pick exact strength + quantity
  3. Save complete webpage to metro folder
  4. Run scraper → [metro]_data.csv
  5. Run pricing_pipeline.py to flag outliers
```

---

## Related files

| File | Purpose |
|------|---------|
| `scripts/scraper.ipynb` | Current GoodRx HTML → CSV parser |
| `scripts/pricing_pipeline.py` | NADAC validation / outlier flags |
| `scripts/drug_ndc_baseline.json` | Baseline SKU for NADAC lookups |
| `script.js` | Loads CSVs; Greenville ZIP override today |
| `pharmacies.json` | 301 SC pharmacy locations |

---

## Next steps (when ready)

1. Rewrite `get_name_price()` with normalization, fallbacks, and manifest support.
2. Add `scrape_manifest.json` templates per metro folder.
3. Update `script.js` to load metro CSV by pharmacy ZIP (Columbia, Charleston, etc.).
